"""Explicit, user-triggered download of the local speech recognition model."""

import logging
import os
import threading
from pathlib import Path


logger = logging.getLogger(__name__)
BASE_MODEL_DIR = Path(os.environ.get("MEETING_ASSISTANT_DATA_DIR") or Path(__file__).resolve().parent) / "whisper_models" / "base"
REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")


class SpeechModelDownload:
    def __init__(self, model_dir: Path = BASE_MODEL_DIR):
        self.model_dir = model_dir
        self._lock = threading.Lock()
        self._downloading = False
        self._error = ""

    def _installed(self) -> bool:
        return all((self.model_dir / name).is_file() for name in REQUIRED_FILES)

    def status(self) -> dict:
        with self._lock:
            if self._downloading:
                return {"state": "downloading", "message": "正在下载语音识别模型，请保持联网并等待；完成后会自动显示“已就绪”。"}
            if self._installed():
                return {"state": "ready", "message": "语音识别模型已就绪，可离线识别。"}
            if self._error:
                return {"state": "failed", "message": f"下载失败：{self._error}。检查网络后可点击重试。"}
            return {"state": "missing", "message": "尚未下载语音识别模型。首次使用请点击下方按钮，下载完成后再开始监听。"}

    def require_ready(self) -> None:
        state = self.status()["state"]
        if state != "ready":
            raise RuntimeError("语音识别模型尚未就绪；请到主界面“语音识别设置”点击“下载语音识别模型”。")

    def start_download(self) -> dict:
        with self._lock:
            if self._installed():
                return {"state": "ready", "message": "语音识别模型已就绪，可离线识别。"}
            if not self._downloading:
                self._downloading = True
                self._error = ""
                threading.Thread(target=self._download, name="speech-model-download", daemon=True).start()
        return self.status()

    def _download(self) -> None:
        try:
            from faster_whisper.utils import download_model

            self.model_dir.mkdir(parents=True, exist_ok=True)
            download_model("base", output_dir=str(self.model_dir))
            if not self._installed():
                raise RuntimeError("模型文件不完整")
            logger.info("Whisper base model downloaded to %s", self.model_dir)
        except Exception as exc:
            logger.exception("Whisper model download failed")
            with self._lock:
                self._error = str(exc) or type(exc).__name__
        finally:
            with self._lock:
                self._downloading = False


speech_model = SpeechModelDownload()
