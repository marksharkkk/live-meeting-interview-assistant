import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.speech_model import REQUIRED_FILES, SpeechModelDownload


class SpeechModelDownloadTests(unittest.TestCase):
    def test_missing_model_requires_explicit_click_then_becomes_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            model = SpeechModelDownload(Path(directory) / "base")
            self.assertEqual(model.status()["state"], "missing")
            with self.assertRaisesRegex(RuntimeError, "下载语音识别模型"):
                model.require_ready()

            def fake_download(name, output_dir):
                self.assertEqual(name, "base")
                for filename in REQUIRED_FILES:
                    (Path(output_dir) / filename).write_bytes(b"test")

            with patch("faster_whisper.utils.download_model", side_effect=fake_download):
                self.assertIn(model.start_download()["state"], {"downloading", "ready"})
                for _ in range(100):
                    if model.status()["state"] == "ready":
                        break
                    time.sleep(0.01)
                self.assertEqual(model.status()["state"], "ready")
                model.require_ready()

    def test_failed_download_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            model = SpeechModelDownload(Path(directory) / "base")
            with patch("faster_whisper.utils.download_model", side_effect=OSError("offline")):
                model.start_download()
                for _ in range(100):
                    if model.status()["state"] == "failed":
                        break
                    time.sleep(0.01)
                self.assertEqual(model.status()["state"], "failed")
                self.assertIn("重试", model.status()["message"])
