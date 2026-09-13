import asyncio
import json
import queue
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from knowledge_base import KnowledgeBase
from local_translation import detect_translation_direction
from llm_service import LLMService
from security import safe_upload_path, validate_upload_filename
from voice_recognition import VoiceRecognizer, _AudioSegmenter
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
    def test_auto_language_is_redetected_on_each_audio_chunk(self):
        from types import SimpleNamespace
        recognizer = VoiceRecognizer.__new__(VoiceRecognizer)
        recognizer.language = 'auto'
        calls = []

        def transcribe(audio, **options):
            calls.append(options)
            return [SimpleNamespace(text='你好' if len(calls) == 1 else 'Hello')], None

        recognizer._whisper_model = SimpleNamespace(transcribe=transcribe)
        self.assertEqual(recognizer._transcribe_audio(np.zeros(16000)), '你好')
        self.assertEqual(recognizer._transcribe_audio(np.zeros(16000)), 'Hello')
        self.assertTrue(all(call['language'] is None for call in calls))
        recognizer.language = 'en-US'
        recognizer._transcribe_audio(np.zeros(16000))
        self.assertEqual(calls[-1]['language'], 'en')

    def test_audio_segmenter_flushes_continuous_audio(self):
        audio_queue = queue.Queue()
        recognizer = type(
            "FakeRecognizer",
            (),
            {
                "sample_rate": 16000,
                "_queue_audio": lambda self, session_id, audio, segment, end=False: audio_queue.put(
                    (session_id, audio, segment, end)
                ),
            },
        )()
        segmenter = _AudioSegmenter(recognizer, session_id=1, sample_rate=48000)
        chunk = np.full(480, 1000, dtype=np.int16)
        for _ in range(601):
            segmenter.push(chunk)
        self.assertEqual(audio_queue.qsize(), 1)
        record = audio_queue.get_nowait()
        self.assertEqual(record[2], 1)
        self.assertFalse(record[3])

    def test_audio_segmenter_tolerates_in_sentence_pause_and_marks_real_boundary(self):
        captured = []
        recognizer = type(
            "FakeRecognizer",
            (),
            {
                "sample_rate": 1000,
                "_queue_audio": lambda self, session_id, audio, segment, end=False: captured.append(
                    (session_id, audio, segment, end)
                ),
            },
        )()
        segmenter = _AudioSegmenter(recognizer, session_id=1, sample_rate=1000)
        speech = np.full(100, 1000, dtype=np.int16)
        silence = np.zeros(100, dtype=np.int16)

        for _ in range(10):
            segmenter.push(speech)
        for _ in range(6):
            segmenter.push(silence)
        self.assertEqual(captured, [])
        self.assertTrue(segmenter.is_recording)

        # Speech resumes before the 0.9s hangover expires, so the same
        # utterance continues instead of being split at the short pause.
        segmenter.push(speech)
        for _ in range(10):
            segmenter.push(silence)
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0][3])

    def test_automatic_capture_uses_system_loopback(self):
        recognizer = VoiceRecognizer.__new__(VoiceRecognizer)
        recognizer.audio = type("FakeAudio", (), {"get_device_count": lambda self: 0})()
        recognizer.input_device_index = None
        recognizer._state_lock = __import__("threading").RLock()
        index, device = recognizer._find_input_device()
        self.assertEqual(index, -2)
        self.assertEqual(device["kind"], "system_loopback")

    def test_legacy_output_endpoint_is_migrated_to_system_loopback(self):
        recognizer = VoiceRecognizer.__new__(VoiceRecognizer)
        recognizer.audio = type("FakeAudio", (), {
            "get_device_count": lambda self: 1,
            "get_device_info_by_index": lambda self, index: {
                "index": index,
                "name": "电脑扬声器 (Realtek HD Audio output)",
                "maxInputChannels": 2,
            },
        })()
        recognizer.input_device_index = 0
        recognizer._state_lock = __import__("threading").RLock()
        index, device = recognizer._find_input_device()
        self.assertEqual(index, -2)
        self.assertEqual(device["kind"], "system_loopback")

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


class LocalTranslationTests(unittest.TestCase):
    def test_live_answer_disables_thinking_only_for_supported_official_models(self):
        with patch.object(config.settings, 'openai_api_base', 'https://api.deepseek.com'), patch.object(config.settings, 'openai_model', 'deepseek-flash'):
            self.assertEqual(LLMService._answer_options(), {'extra_body': {'thinking': {'type': 'disabled'}}})
            with patch.object(config.settings, 'openai_api_base', 'https://example.com/v1'):
                self.assertEqual(LLMService._answer_options(), {})
            with patch.object(config.settings, 'openai_model', 'deepseek-reasoner'):
                self.assertEqual(LLMService._answer_options(), {})

    def test_translation_direction_follows_dominant_script(self):
        self.assertEqual(detect_translation_direction("你好，欢迎参加会议。"), ("zh", "en"))
        self.assertEqual(detect_translation_direction("Welcome to the meeting."), ("en", "zh"))

    def test_mixed_subtitle_with_more_chinese_uses_chinese_source(self):
        self.assertEqual(detect_translation_direction("请介绍一下你的 API"), ("zh", "en"))


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

    def test_main_window_llm_settings_update_runtime_and_persisted_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            original_env_file = config.ENV_FILE
            original_base = config.settings.openai_api_base
            original_model = config.settings.openai_model
            try:
                config.ENV_FILE = env_file
                config.persist_settings({
                    "openai_api_base": "https://example.test/v1",
                    "openai_model": "example-model",
                })
                self.assertEqual(config.settings.openai_api_base, "https://example.test/v1")
                self.assertEqual(config.settings.openai_model, "example-model")
                saved = env_file.read_text(encoding="utf-8")
                self.assertIn("OPENAI_API_BASE='https://example.test/v1'", saved)
                self.assertIn("OPENAI_MODEL='example-model'", saved)
            finally:
                config.ENV_FILE = original_env_file
                config.settings.openai_api_base = original_base
                config.settings.openai_model = original_model


if __name__ == "__main__":
    unittest.main()
