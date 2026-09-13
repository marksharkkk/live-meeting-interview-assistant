import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
os.environ.setdefault("MEETING_ASSISTANT_TOKEN", "test-token")
os.environ["KNOWLEDGE_BASE_DIR"] = tempfile.mkdtemp(prefix="meeting-assistant-api-kb-")

import main as backend_main  # noqa: E402

app = backend_main.app


class ApiSecurityTests(unittest.TestCase):
    def test_ai_artifacts_stay_with_source_session_after_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            store = backend_main.MeetingStore(directory)
            first = store.start('First', False)['id']
            store.append('First original')
            store.finish()
            second = store.start('Second', False)['id']

            async def answer(**kwargs):
                self.assertIn('Reply in English.', kwargs['system_prompt'])
                yield 'Answer to first question'

            async def translate(text):
                return '译文'

            with patch.object(backend_main, 'meeting_store', store), patch.object(
                backend_main.llm_service, 'generate_answer_stream', answer
            ), patch.object(backend_main.llm_service, 'translate_text', translate):
                payload = {'meeting_id': first, 'source_at': '2026-09-13T01:00:00Z'}
                result = self.client.post('/api/answer/stream', headers=self.headers, json={
                    **payload, 'question': 'First question', 'use_knowledge_base': False, 'answer_language': 'en'})
                self.assertIn('"type": "done"', result.text)
                self.assertEqual(self.client.post('/api/translate', headers=self.headers, json={
                    **payload, 'text': 'First original'}).status_code, 200)
                reloaded = backend_main.MeetingStore(directory)
                saved = reloaded.read(first)
                self.assertEqual(saved['answers'][0]['question'], 'First question')
                self.assertEqual(saved['translations'][0]['original'], 'First original')
                self.assertEqual(saved['answers'][0]['source_at'], payload['source_at'])
                self.assertEqual(reloaded.read(second)['answers'], [])
                self.assertEqual(saved['transcript'].count('First original'), 1)

    def test_live_summary_records_source_cutoff_while_new_words_arrive(self):
        with tempfile.TemporaryDirectory() as directory:
            store = backend_main.MeetingStore(directory)
            ident = store.start('Live', False)['id']
            first = store.append('Initial words')

            async def summarize(text):
                self.assertIn('Initial words', text)
                self.assertNotIn('Later words', text)
                store.append('Later words')
                return 'Initial summary'

            with patch.object(backend_main, 'meeting_store', store), patch.object(
                backend_main.llm_service, 'summarize_meeting', summarize
            ):
                result = self.client.post(f'/api/meetings/{ident}/summary?live=true', headers=self.headers).json()
                self.assertEqual(result['source_at'], first['created_at'])
                self.assertIn('Later words', store.read(ident)['transcript'])
                self.assertEqual(store.read(ident)['live-summary_source']['source_at'], first['created_at'])

    def test_interview_without_meeting_receives_live_and_final_text(self):
        class Recognizer:
            def start_listening(self, callback, error_callback, boundary_callback):
                self.callback = callback
                callback('A spoken interview question')
                boundary_callback('A spoken interview question', True)

            def stop_listening(self):
                self.callback('including the final words')

        with tempfile.TemporaryDirectory() as directory:
            store = backend_main.MeetingStore(directory)
            with patch.object(backend_main, 'meeting_store', store), patch.object(
                backend_main, 'voice_recognizer', Recognizer()
            ):
                with self.client.websocket_connect('/ws/voice', subprotocols=['meeting-assistant', 'test-token']) as ws:
                    self.assertEqual(ws.receive_json()['message'], 'Loading speech model')
                    self.assertEqual(ws.receive_json()['message'], 'Listening')
                    self.assertEqual(ws.receive_json()['text'], 'A spoken interview question')
                    self.assertTrue(ws.receive_json()['end_of_utterance'])
                    ws.send_json({'type': 'stop'})
                    self.assertEqual(ws.receive_json()['text'], 'including the final words')
                    self.assertEqual(ws.receive_json()['type'], 'stopped')
                self.assertIsNone(store.active)

    def test_meeting_archive_and_full_summary_api(self):
        with tempfile.TemporaryDirectory() as directory:
            store = backend_main.MeetingStore(directory)

            async def summarize(text):
                self.assertIn('first point', text)
                self.assertIn('last point', text)
                return 'Meeting summary'

            with patch.object(backend_main, 'meeting_store', store), patch.object(
                backend_main.llm_service, 'summarize_meeting', summarize
            ):
                response = self.client.post('/api/meetings', headers=self.headers,
                                            json={'title': 'Test', 'record_audio': False})
                self.assertEqual(response.status_code, 200)
                meeting_id = response.json()['id']
                store.append('first point')
                store.append('last point')
                path = f'/api/meetings/{meeting_id}'
                self.assertEqual(self.client.post(path + '/summary', headers=self.headers).status_code, 409)
                self.assertEqual(self.client.post('/api/meetings/finish', headers=self.headers).status_code, 200)
                self.assertEqual(self.client.post(path + '/summary', headers=self.headers).json()['summary'], 'Meeting summary')
                download = self.client.get(path + '/files/transcript.txt', headers=self.headers)
                self.assertIn('last point', download.text)
                self.assertEqual(self.client.get(path + '/files/meeting.json', headers=self.headers).status_code, 404)

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.headers = {"X-Meeting-Assistant-Token": "test-token"}

    def test_local_api_requires_token(self):
        self.assertEqual(self.client.get("/api/health").status_code, 401)
        response = self.client.get("/api/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_speech_model_has_visible_status_and_explicit_download_route(self):
        class Model:
            def status(self):
                return {"state": "missing", "message": "请点击下载"}

            def start_download(self):
                return {"state": "downloading", "message": "正在下载"}

        with patch.object(backend_main, "speech_model", Model()):
            self.assertEqual(self.client.get("/api/speech-model").status_code, 401)
            self.assertEqual(self.client.get("/api/speech-model", headers=self.headers).json()["state"], "missing")
            self.assertEqual(self.client.post("/api/speech-model/download", headers=self.headers).json()["state"], "downloading")

    def test_settings_do_not_return_api_key(self):
        response = self.client.get("/api/settings", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("openai_api_key", response.json())

    def test_upload_rejects_path_traversal_filename(self):
        response = self.client.post(
            "/api/knowledge/files",
            headers=self.headers,
            files={"files": ("../../main.py", b"not a document", "text/plain")},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["files"][0]["status"], "failed")

    def test_translate_subtitle_uses_translation_service(self):
        original_translate = backend_main.llm_service.translate_text

        async def fake_translate(text):
            self.assertEqual(text, "你好，欢迎参加面试。")
            return "Hello, welcome to the interview."

        backend_main.llm_service.translate_text = fake_translate
        try:
            response = self.client.post(
                "/api/translate",
                headers=self.headers,
                json={"text": "你好，欢迎参加面试。"},
            )
        finally:
            backend_main.llm_service.translate_text = original_translate

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["translation"], "Hello, welcome to the interview.")

    def test_interim_translation_preview_is_not_saved_as_final_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            store = backend_main.MeetingStore(directory)
            meeting_id = store.start('Preview', False)['id']

            async def fake_translate(text):
                return f'译文：{text}'

            with patch.object(backend_main, 'meeting_store', store), patch.object(
                backend_main.llm_service, 'translate_text', fake_translate
            ):
                preview = self.client.post('/api/translate', headers=self.headers, json={
                    'text': '这是一句尚未说完的内容', 'meeting_id': meeting_id,
                    'persist': False,
                })
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(store.read(meeting_id)['translations'], [])

                final = self.client.post('/api/translate', headers=self.headers, json={
                    'text': '这是一句完整的内容。', 'meeting_id': meeting_id,
                })
                self.assertEqual(final.status_code, 200)
                self.assertEqual(len(store.read(meeting_id)['translations']), 1)


if __name__ == "__main__":
    unittest.main()
