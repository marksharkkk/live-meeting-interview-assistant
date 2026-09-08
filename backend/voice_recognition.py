import logging
import queue
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import scipy.signal
from pyaudio import PyAudio, paInt16


logger = logging.getLogger(__name__)
BASE_MODEL_DIR = Path(__file__).resolve().parent / "whisper_models" / "base"


class VoiceRecognizer:
    def __init__(self, language: str = "zh-CN", sample_rate: int = 16000):
        self.language = language
        self.sample_rate = sample_rate
        self.audio = PyAudio()
        self.is_listening = False
        self.callback: Optional[Callable[[str], None]] = None
        self.error_callback: Optional[Callable[[str], None]] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._recognition_thread: Optional[threading.Thread] = None
        self._active_stop_event: Optional[threading.Event] = None
        self._shutdown_event = threading.Event()
        self._recognition_queue: queue.Queue = queue.Queue(maxsize=30)
        self._whisper_model = None
        self._session_id = 0
        self._state_lock = threading.RLock()
        self.input_device_index: int | None = None

    def set_language(self, language: str) -> None:
        with self._state_lock:
            self.language = language

    def set_input_device(self, index: int | None) -> None:
        with self._state_lock:
            self.input_device_index = None if index is None or int(index) < 0 else int(index)

    def list_input_devices(self) -> list[dict]:
        return [
            {"index": index, "name": str(device.get("name", f"设备 {index}"))}
            for index, device in self._input_devices()
        ]

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
        if selected_index is not None:
            for index, device in devices:
                if index == selected_index:
                    logger.info("Using selected input device: %s", device.get("name"))
                    return index, device
            logger.warning("Selected input device %s is unavailable; falling back", selected_index)
        for index, device in devices:
            name = str(device.get("name", "")).casefold()
            if any(marker in name for marker in ("stereo", "mix", "立体声")):
                logger.info("Using stereo-mix device: %s", device.get("name"))
                return index, device

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
            except queue.Empty:
                return

    def stop_listening(self) -> None:
        with self._state_lock:
            was_listening = self.is_listening
            self.is_listening = False
            self._session_id += 1
            if self._active_stop_event:
                self._active_stop_event.set()
            capture_thread = self._capture_thread
        if capture_thread and capture_thread is not threading.current_thread():
            capture_thread.join(timeout=5)
            if capture_thread.is_alive():
                logger.warning("Voice capture thread did not stop within timeout")
        self._drain_recognition_queue()
        if was_listening:
            logger.info("Voice recognizer stopped")

    def _recognition_worker(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                item = self._recognition_queue.get(timeout=1)
                if item is None:
                    return
                session_id, audio_data, segment_number = item
                text = self._transcribe_audio(audio_data)
                with self._state_lock:
                    callback = self.callback if self.is_listening and session_id == self._session_id else None
                if callback and text and len(text) > 1:
                    logger.info("Recognized segment %s: %s", segment_number, text)
                    callback(text)
            except queue.Empty:
                continue
            except Exception as exc:
                logger.error("Recognition worker error: %s", exc, exc_info=True)

    def _transcribe_audio(self, audio_data: np.ndarray) -> str:
        segments, _info = self._whisper_model.transcribe(
            audio_data,
            beam_size=1,
            language=self._whisper_language(),
            vad_filter=True,
        )
        return "".join(segment.text for segment in segments).strip()

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

        actual_rate = int(device_info.get("defaultSampleRate", self.sample_rate))
        chunk_size = 1024
        stream = None
        try:
            stream = self.audio.open(
                format=paInt16,
                channels=1,
                rate=actual_rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=chunk_size,
            )
        except Exception as exc:
            self._report_capture_error(session_id, f"Could not open the audio input: {exc}")
            return

        min_silence_seconds = 0.45
        min_audio_seconds = 0.15
        max_audio_seconds = 20.0
        noise_floor = 20.0
        silence_frames = 0
        audio_buffer = []
        audio_frame_count = 0
        is_recording = False
        segment_count = 0

        try:
            while not stop_event.is_set():
                data = stream.read(chunk_size, exception_on_overflow=False)
                audio_data = np.frombuffer(data, dtype=np.int16)
                audio_level = float(np.mean(np.abs(audio_data)))
                frame_seconds = chunk_size / actual_rate
                if not is_recording:
                    noise_floor = 0.95 * noise_floor + 0.05 * audio_level
                silence_threshold = max(60.0, noise_floor * 2.2)

                if audio_level > silence_threshold:
                    silence_frames = 0
                    audio_buffer.append(audio_data)
                    audio_frame_count += 1
                    is_recording = True
                elif is_recording:
                    silence_frames += 1
                    audio_buffer.append(audio_data)
                else:
                    continue

                should_transcribe = (
                    is_recording
                    and audio_frame_count * frame_seconds >= min_audio_seconds
                    and (
                        silence_frames * frame_seconds >= min_silence_seconds
                        or audio_frame_count * frame_seconds >= max_audio_seconds
                    )
                )
                if not should_transcribe:
                    continue

                audio_np = np.concatenate(audio_buffer).astype(np.float32) / 32768.0
                if actual_rate != self.sample_rate:
                    sample_count = int(len(audio_np) * self.sample_rate / actual_rate)
                    audio_np = scipy.signal.resample(audio_np, sample_count)

                segment_count += 1
                try:
                    self._recognition_queue.put_nowait((session_id, audio_np, segment_count))
                except queue.Full:
                    logger.warning("Recognition queue is full; dropping an audio segment")

                silence_frames = 0
                audio_buffer = []
                audio_frame_count = 0
                is_recording = False
        except Exception as exc:
            if not stop_event.is_set():
                self._report_capture_error(session_id, f"Audio capture failed: {exc}")
        finally:
            stream.stop_stream()
            stream.close()
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
