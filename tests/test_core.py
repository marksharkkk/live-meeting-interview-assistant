import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from knowledge_base import KnowledgeBase
from security import safe_upload_path, validate_upload_filename
from voice_recognition import VoiceRecognizer
import config


class UploadSecurityTests(unittest.TestCase):
    def test_accepts_supported_basename(self):
        self.assertEqual(validate_upload_filename("meeting notes.docx"), "meeting notes.docx")

    def test_rejects_traversal_and_unsupported_types(self):
        for filename in ("../main.py", "..\\main.py", "folder/file.pdf", "script.py", ""):
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    validate_upload_filename(filename)

    def test_resolved_path_stays_in_upload_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = safe_upload_path(root, "notes.txt")
            self.assertEqual(target.parent, root.resolve())


class KnowledgeBaseTests(unittest.TestCase):
    def test_text_is_chunked_and_json_cache_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            kb = KnowledgeBase(directory)
            asyncio.run(kb.add_text("项目技术方案包括安全认证和本地语音识别。" * 120))
            self.assertGreater(kb.status()["document_count"], 1)

            cache = Path(directory) / "kb_cache.json"
            payload = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertGreater(len(payload["documents"]), 1)

            reloaded = KnowledgeBase(directory)
            result = asyncio.run(reloaded.query("项目的安全认证方案是什么"))
            self.assertIn("安全认证", result)

    def test_readding_file_replaces_existing_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            kb = KnowledgeBase(directory)
            source = kb.uploads_dir / "notes.txt"
            source.write_text("first version " * 200, encoding="utf-8")
            asyncio.run(kb.add_documents([str(source)]))
            first_count = kb.status()["document_count"]

            source.write_text("second version " * 200, encoding="utf-8")
            asyncio.run(kb.add_documents([str(source)]))
            self.assertEqual(kb.status()["document_count"], first_count)

    def test_cache_survives_project_directory_move(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "project-a"
            moved = Path(directory) / "project-b"
            kb = KnowledgeBase(original)
            source = kb.uploads_dir / "notes.txt"
            source.write_text("portable project knowledge " * 100, encoding="utf-8")
            asyncio.run(kb.add_documents([str(source)]))
            self.assertTrue(kb.status()["files"][0]["loaded"])

            shutil.copytree(original, moved)
            moved_kb = KnowledgeBase(moved)
            self.assertTrue(moved_kb.status()["files"][0]["loaded"])
            self.assertIn("portable project knowledge", asyncio.run(moved_kb.query("portable project")))


class VoiceRecognizerTests(unittest.TestCase):
    def test_selected_input_device_is_preferred(self):
        recognizer = VoiceRecognizer.__new__(VoiceRecognizer)
        recognizer.audio = type("FakeAudio", (), {
            "get_device_count": lambda self: 2,
            "get_device_info_by_index": lambda self, index: {
                "index": index, "name": f"Mic {index}", "maxInputChannels": 1,
            },
        })()
        recognizer.input_device_index = 1
        recognizer._state_lock = __import__("threading").RLock()
        index, device = recognizer._find_input_device()
        self.assertEqual(index, 1)
        self.assertEqual(device["name"], "Mic 1")


class SettingsTests(unittest.TestCase):
    def test_persisted_settings_are_written_to_selected_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            original_env_file = config.ENV_FILE
            original_model = config.settings.openai_model
            try:
                config.ENV_FILE = env_file
                config.persist_settings({"openai_model": "test-model"})
                self.assertIn("OPENAI_MODEL='test-model'", env_file.read_text(encoding="utf-8"))
                self.assertEqual(config.settings.openai_model, "test-model")
            finally:
                config.ENV_FILE = original_env_file
                config.settings.openai_model = original_model


if __name__ == "__main__":
    unittest.main()
