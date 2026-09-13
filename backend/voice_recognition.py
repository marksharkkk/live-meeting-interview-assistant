import logging
import os
import queue
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import scipy.signal
from pyaudio import PyAudio, paInt16

try:
    from .wasapi_loopback import WasapiLoopback
except ImportError:
    from wasapi_loopback import WasapiLoopback


logger = logging.getLogger(__name__)
BASE_MODEL_DIR = Path(os.environ.get("MEETING_ASSISTANT_DATA_DIR") or Path(__file__).resolve().parent) / "whisper_models" / "base"
SYSTEM_LOOPBACK_INDEX = -2


class _AudioSegmenter:
    """Turn native-rate PCM chunks into the existing Whisper queue segments."""

    SPEECH_THRESHOLD = 300.0
    # A normal speaker can pause for more than half a second inside one
    # sentence.  The old 0.5s endpoint therefore finalized subtitles in the
    # middle of a thought.  Keep a little more VAD hangover so a pause is
    # treated as an utterance boundary only when it is likely intentional.
    SILENCE_TIMEOUT_SECONDS = 0.9
    # Whisper needs enough context around word boundaries.  The old 3s hard
    # cut frequently split a word and made the next independent chunk start
    # with the remainder of the sentence.  Six seconds still gives natural
    # pauses near-live subtitles while avoiding most mid-word cuts.
    MAX_AUDIO_SECONDS = 6.0

    def __init__(self, recognizer: "VoiceRecognizer", session_id: int, sample_rate: int):
        self.recognizer = recognizer
        self.session_id = session_id
        self.sample_rate = sample_rate
        self.silence_seconds = 0.0
        self.audio_seconds = 0.0
        self.audio_buffer: list[np.ndarray] = []
        self.segment_count = 0
        self.is_recording = False

    def push(self, audio_data: np.ndarray) -> None:
        if audio_data.size == 0:
            return
        audio_data = np.asarray(audio_data, dtype=np.int16).reshape(-1)
        audio_callback = getattr(self.recognizer, 'audio_callback', None)
        if audio_callback:
            audio_callback(audio_data.tobytes(), self.sample_rate)
        audio_level = float(np.sqrt(np.mean(np.square(audio_data.astype(np.float32)))))
        frame_seconds = len(audio_data) / max(1, self.sample_rate)

        if audio_level > self.SPEECH_THRESHOLD:
            self.silence_seconds = 0.0
            self.audio_buffer.append(audio_data)
            self.audio_seconds += frame_seconds
            self.is_recording = True
        elif self.is_recording:
            self.silence_seconds += frame_seconds
            self.audio_buffer.append(audio_data)
            self.audio_seconds += frame_seconds
        else:
            return

        if (
            self.is_recording
            and self.audio_seconds >= 0.15
            and (
                self.silence_seconds >= self.SILENCE_TIMEOUT_SECONDS
                or self.audio_seconds >= self.MAX_AUDIO_SECONDS
            )
        ):
            self.flush()

    def flush(self) -> None:
        if not self.is_recording or self.audio_seconds < 0.15:
            return
        audio_np = np.concatenate(self.audio_buffer).astype(np.float32) / 32768.0
        if self.sample_rate != self.recognizer.sample_rate:
            sample_count = int(len(audio_np) * self.recognizer.sample_rate / self.sample_rate)
            audio_np = scipy.signal.resample(audio_np, sample_count)
        self.segment_count += 1
        # A silence-triggered flush is an utterance boundary.  A duration
        # flush is only a transport chunk and must be joined by the UI before
        # it is translated.
        end_of_utterance = self.silence_seconds >= self.SILENCE_TIMEOUT_SECONDS
        try:
            self.recognizer._queue_audio(
                self.session_id, audio_np, self.segment_count, end_of_utterance
            )
        except TypeError:
            # Keep the small fake recognizers used by older integrations
            # compatible with the new optional boundary metadata.
            self.recognizer._queue_audio(self.session_id, audio_np, self.segment_count)
        logger.info(
            "Queued audio segment %s: %.2fs at %sHz",
            self.segment_count,
            self.audio_seconds,
            self.sample_rate,
        )
        self.silence_seconds = 0.0
        self.audio_seconds = 0.0
        self.audio_buffer = []
        self.is_recording = False


class VoiceRecognizer:
    def __init__(self, language: str = "auto", sample_rate: int = 16000):
        self.language = language
        self.sample_rate = sample_rate
        self.audio = PyAudio()
        self.is_listening = False
        self.callback: Optional[Callable[[str], None]] = None
        self.boundary_callback: Optional[Callable[[str, bool], None]] = None
        self.error_callback: Optional[Callable[[str], None]] = None
        self.audio_callback = None
        self._capture_thread: Optional[threading.Thread] = None
        self._recognition_thread: Optional[threading.Thread] = None
        self._active_stop_event: Optional[threading.Event] = None
        self._shutdown_event = threading.Event()
        # Recognition runs on CPU and can briefly fall behind while Whisper
        # is decoding.  A small queue caused whole audio segments to be
        # discarded under load.  Keep the queue bounded, but give the worker
        # several minutes of headroom before declaring a loss.
        self._recognition_queue: queue.Queue = queue.Queue(maxsize=90)
        self._whisper_model = None
        self._session_id = 0
        self._state_lock = threading.RLock()
        self.input_device_index: int | None = None

    def set_language(self, language: str) -> None:
        with self._state_lock:
            self.language = language

    def set_input_device(self, index: int | None) -> None:
        with self._state_lock:
            self.input_device_index = None if index is None or int(index) == -1 else int(index)

    def list_input_devices(self) -> list[dict]:
        # -2 is a stable application-level ID, not a PyAudio index. It is the
        # NexQ-style WASAPI loopback source for the current headphones/speaker.
        devices = [{
            "index": SYSTEM_LOOPBACK_INDEX,
            "name": "系统音频回环（当前默认耳机/扬声器，推荐）",
            "kind": "system_loopback",
        }]
        devices.extend([
            {"index": index, "name": str(device.get("name", f"设备 {index}"))}
            for index, device in self._input_devices()
        ])
        return devices

    def _whisper_language(self) -> str | None:
        value = (self.language or "").strip().lower()
        if not value or value == "auto":
            return None
        return value.split("-", 1)[0]

    def _load_whisper_model(self) -> None:
        if self._whisper_model is not None:
            return
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model

        if not (BASE_MODEL_DIR / "model.bin").is_file():
            BASE_MODEL_DIR.mkdir(parents=True, exist_ok=True)
            logger.info("Downloading Whisper base model into the project")
            download_model("base", output_dir=str(BASE_MODEL_DIR))

        logger.info("Loading project-local Whisper base model")
        self._whisper_model = WhisperModel(
            str(BASE_MODEL_DIR),
            device="cpu",
            compute_type="int8",
        )
        logger.info("Whisper base model loaded")

    def _input_devices(self) -> list[tuple[int, dict]]:
        devices = []
        for index in range(self.audio.get_device_count()):
            try:
                device = self.audio.get_device_info_by_index(index)
                if int(device.get("maxInputChannels", 0)) > 0:
                    devices.append((index, device))
            except Exception as exc:
                logger.debug("Could not inspect audio device %s: %s", index, exc)
        return devices

    def _find_input_device(self) -> tuple[int | None, dict | None]:
        devices = self._input_devices()
        with self._state_lock:
            selected_index = self.input_device_index
        if selected_index is None:
            logger.info("Using NexQ-style WASAPI system loopback for the default output")
            return SYSTEM_LOOPBACK_INDEX, {
                "name": "系统音频回环（当前默认耳机/扬声器）",
                "kind": "system_loopback",
            }
        if selected_index == SYSTEM_LOOPBACK_INDEX:
            return SYSTEM_LOOPBACK_INDEX, {
                "name": "系统音频回环（当前默认耳机/扬声器）",
                "kind": "system_loopback",
            }
        if selected_index is not None:
            for index, device in devices:
                if index == selected_index:
                    # Existing installations often saved a Stereo Mix index.
                    # Migrate that selection to the reliable output loopback.
                    name = str(device.get("name", "")).casefold()
                    if any(
                        marker in name
                        for marker in (
                            "stereo",
                            "mix",
                            "立体声",
                            "扬声器",
                            "speaker",
                            "output",
                            "耳机",
                            "headphone",
                        )
                    ):
                        logger.info(
                            "Replacing legacy Stereo Mix selection with WASAPI system loopback: %s",
                            device.get("name"),
                        )
                        return SYSTEM_LOOPBACK_INDEX, {
                            "name": "系统音频回环（当前默认耳机/扬声器）",
                            "kind": "system_loopback",
                        }
                    logger.info("Using selected input device: %s", device.get("name"))
                    return index, device
            logger.warning("Selected input device %s is unavailable; falling back", selected_index)
        try:
            default_device = self.audio.get_default_input_device_info()
            default_index = int(default_device["index"])
            if int(default_device.get("maxInputChannels", 0)) > 0:
                logger.info("Using default input device: %s", default_device.get("name"))
                return default_index, default_device
        except Exception:
            pass

        if devices:
            index, device = devices[0]
            logger.info("Using first available input device: %s", device.get("name"))
            return index, device
        return None, None

    def _ensure_recognition_worker(self) -> None:
        if self._recognition_thread and self._recognition_thread.is_alive():
            return
        self._recognition_thread = threading.Thread(
            target=self._recognition_worker,
            name="meeting-assistant-recognition",
            daemon=True,
        )
        self._recognition_thread.start()

    def start_listening(
        self,
        callback: Callable[[str], None],
        error_callback: Optional[Callable[[str], None]] = None,
        boundary_callback: Optional[Callable[[str, bool], None]] = None,
    ) -> None:
        self._load_whisper_model()
        with self._state_lock:
            if self.is_listening:
                raise RuntimeError("Voice recognition is already running")
            if self._capture_thread and self._capture_thread.is_alive():
                raise RuntimeError("Previous voice capture session is still stopping")
            self._session_id += 1
            session_id = self._session_id
            self.callback = callback
            self.error_callback = error_callback
            self.boundary_callback = boundary_callback
            self.is_listening = True
            stop_event = threading.Event()
            self._active_stop_event = stop_event
            self._ensure_recognition_worker()
            self._capture_thread = threading.Thread(
                target=self._listen_loop,
                args=(session_id, stop_event),
                name="meeting-assistant-capture",
                daemon=True,
            )
            self._capture_thread.start()
        logger.info("Voice recognizer started")

    def _drain_recognition_queue(self) -> None:
        while True:
            try:
                self._recognition_queue.get_nowait()
                self._recognition_queue.task_done()
            except queue.Empty:
                return

    def stop_listening(self) -> None:
        with self._state_lock:
            was_listening = self.is_listening
            self.is_listening = False
            if self._active_stop_event:
                self._active_stop_event.set()
            capture_thread = self._capture_thread
        if capture_thread and capture_thread is not threading.current_thread():
            capture_thread.join(timeout=5)
            if capture_thread.is_alive():
                logger.warning("Voice capture thread did not stop within timeout")
        self._recognition_queue.join()
        with self._state_lock:
            self._session_id += 1
        if was_listening:
            logger.info("Voice recognizer stopped")

    def _recognition_worker(self) -> None:
        while not self._shutdown_event.is_set():
            item_received = False
            try:
                item = self._recognition_queue.get(timeout=1)
                item_received = True
                if item is None:
                    return
                if len(item) == 4:
                    session_id, audio_data, segment_number, end_of_utterance = item
                else:
                    session_id, audio_data, segment_number = item
                    end_of_utterance = False
                text = self._transcribe_audio(audio_data)
                with self._state_lock:
                    callback = self.callback if session_id == self._session_id else None
                    boundary_callback = (
                        getattr(self, 'boundary_callback', None)
                        if session_id == self._session_id else None
                    )
                # Do not discard one-character Chinese words or short English
                # answers.  The previous len(text) > 1 guard silently removed
                # valid recognition results.
                if callback and text:
                    logger.info("Recognized segment %s: %s", segment_number, text)
                    callback(text)
                elif not text:
                    logger.info("Whisper returned no text for segment %s", segment_number)
                if boundary_callback:
                    boundary_callback(text, end_of_utterance)
            except queue.Empty:
                continue
            except Exception as exc:
                logger.error("Recognition worker error: %s", exc, exc_info=True)
                with self._state_lock:
                    callback = self.error_callback if self.is_listening else None
                if callback:
                    callback(f"语音识别失败：{exc}")
            finally:
                if item_received:
                    self._recognition_queue.task_done()

    def _transcribe_audio(self, audio_data: np.ndarray) -> str:
        segments, _info = self._whisper_model.transcribe(
            audio_data,
            beam_size=1,
            language=self._whisper_language(),
            condition_on_previous_text=False,
            without_timestamps=True,
            # The capture layer already uses NexQ-style energy VAD. Running a
            # second VAD here can discard short or low-volume remote speech.
            vad_filter=False,
        )
        return "".join(segment.text for segment in segments).strip()

    def _queue_audio(
        self,
        session_id: int,
        audio_np: np.ndarray,
        segment_number: int,
        end_of_utterance: bool = False,
    ) -> None:
        try:
            self._recognition_queue.put_nowait(
                (session_id, audio_np, segment_number, end_of_utterance)
            )
        except queue.Full:
            logger.warning("Recognition queue is full; dropping an audio segment")
            if self.error_callback:
                self.error_callback('识别速度跟不上录音，部分转录缺失；请保留原音频。')

    def _report_capture_error(self, session_id: int, message: str) -> None:
        logger.error(message)
        with self._state_lock:
            callback = self.error_callback if session_id == self._session_id else None
            if session_id == self._session_id:
                self.is_listening = False
                self._active_stop_event = None
        if callback:
            callback(message)

    def _listen_loop(self, session_id: int, stop_event: threading.Event) -> None:
        device_index, device_info = self._find_input_device()
        if device_info is None:
            self._report_capture_error(session_id, "No audio input device is available")
            return

        if device_index == SYSTEM_LOOPBACK_INDEX:
            self._listen_loopback(session_id, stop_event)
            return

        actual_rate = int(device_info.get("defaultSampleRate", self.sample_rate))
        chunk_size = 1024
        channels = max(1, min(int(device_info.get("maxInputChannels", 1)), 2))
        stream = None
        try:
            stream = self.audio.open(
                format=paInt16,
                channels=channels,
                rate=actual_rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=chunk_size,
            )
        except Exception as exc:
            self._report_capture_error(session_id, f"Could not open the audio input: {exc}")
            return

        segmenter = _AudioSegmenter(self, session_id, actual_rate)

        try:
            while not stop_event.is_set():
                data = stream.read(chunk_size, exception_on_overflow=False)
                audio_data = np.frombuffer(data, dtype=np.int16)
                if channels > 1:
                    audio_data = audio_data[: len(audio_data) - (len(audio_data) % channels)]
                    audio_data = audio_data.reshape(-1, channels).mean(axis=1).astype(np.int16)
                segmenter.push(audio_data)
        except Exception as exc:
            if not stop_event.is_set():
                self._report_capture_error(session_id, f"Audio capture failed: {exc}")
        finally:
            if stream:
                stream.stop_stream()
                stream.close()
            segmenter.flush()
            self._mark_capture_stopped(session_id)

    def _listen_loopback(self, session_id: int, stop_event: threading.Event) -> None:
        segmenter: _AudioSegmenter | None = None

        def on_audio(samples: np.ndarray, actual_rate: int) -> None:
            nonlocal segmenter
            if segmenter is None:
                segmenter = _AudioSegmenter(self, session_id, actual_rate)
            pcm = np.clip(samples, -1.0, 1.0) * 32767.0
            segmenter.push(pcm.astype(np.int16))

        try:
            WasapiLoopback().capture(stop_event, on_audio)
        except Exception as exc:
            if not stop_event.is_set():
                self._report_capture_error(session_id, f"WASAPI 系统音频回环失败：{exc}")
        finally:
            if segmenter:
                segmenter.flush()
            self._mark_capture_stopped(session_id)

    def _mark_capture_stopped(self, session_id: int) -> None:
        with self._state_lock:
            if session_id == self._session_id:
                self.is_listening = False
                self._active_stop_event = None

    def cleanup(self) -> None:
        self.stop_listening()
        self._shutdown_event.set()
        try:
            self._recognition_queue.put_nowait(None)
        except queue.Full:
            self._drain_recognition_queue()
            self._recognition_queue.put_nowait(None)
        if self._recognition_thread:
            self._recognition_thread.join(timeout=5)
        self.audio.terminate()
