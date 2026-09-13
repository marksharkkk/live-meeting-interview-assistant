import asyncio
import json
import queue
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from meeting_store import MeetingStore
from llm_service import LLMService
from voice_recognition import VoiceRecognizer


class MeetingTests(unittest.TestCase):
    def test_audio_and_originals_survive_finish_and_reload(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            meeting = store.start('Example', True)
            store.audio(b'\x00\x00' * 100, 16000)
            store.append('Original text')
            store.close_audio()  # Pause/resume creates separate original audio tracks.
            store.audio(b'\x01\x00' * 200, 16000)
            store.append('Last words')
            store.finish()
            loaded = MeetingStore(root).read(meeting['id'])
            self.assertIn('Last words', loaded['transcript'])
            self.assertEqual(len(loaded['tracks']), 2)
            directory = store.directory(meeting['id'])
            with wave.open(str(directory / 'audio-001.wav')) as wav:
                self.assertEqual(wav.getnframes(), 100)
                self.assertEqual(wav.getframerate(), 16000)
            rows = [json.loads(line) for line in (directory / 'transcript.jsonl').read_text().splitlines()]
            self.assertEqual([row['text'] for row in rows], ['Original text', 'Last words'])

    def test_recording_opt_out_and_path_validation(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            row = store.start('', False)
            store.audio(b'\x00\x00', 16000)
            store.finish()
            self.assertFalse(list(store.directory(row['id']).glob('*.wav')))
            with self.assertRaises(ValueError):
                store.read('../secret')

    def test_long_summary_processes_entire_original(self):
        service = LLMService.__new__(LLMService)
        calls = []

        async def summarize(text, **kwargs):
            calls.append(text)
            return 'condensed notes'

        service.generate_answer = summarize
        original = 'A' * 10000 + 'B' * 10000 + 'TAIL'
        asyncio.run(service.summarize_meeting(original))
        self.assertEqual(''.join(calls[:3]), original)
        self.assertIn('condensed notes', calls[-1])

    def test_stop_drains_last_recognition(self):
        recognizer = VoiceRecognizer.__new__(VoiceRecognizer)
        recognizer._state_lock = threading.RLock()
        recognizer._shutdown_event = threading.Event()
        recognizer._active_stop_event = threading.Event()
        recognizer._session_id = 1
        recognizer.is_listening = True
        recognizer._capture_thread = None
        recognizer._recognition_queue = queue.Queue()
        recognizer._recognition_queue.put((1, None, 1))
        received = []
        recognizer.callback = received.append
        recognizer.error_callback = None
        recognizer._transcribe_audio = lambda _: 'last words'
        worker = threading.Thread(target=recognizer._recognition_worker)
        worker.start()
        recognizer.stop_listening()
        recognizer._shutdown_event.set()
        worker.join(2)
        self.assertEqual(received, ['last words'])
