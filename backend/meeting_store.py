"""Durable meeting originals, independent of editable subtitle UI."""
import json
import os
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path


class MeetingStore:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get('MEETINGS_DIR', Path(__file__).parent / 'meetings'))
        self.lock = threading.RLock()
        self.active = None
        self.writer = None
        self.track = 0
        self.rate = None

    def directory(self, meeting_id):
        if len(meeting_id) != 32 or any(c not in '0123456789abcdef' for c in meeting_id):
            raise ValueError('Invalid meeting ID')
        return self.root / meeting_id

    def _save(self):
        path = self.directory(self.active['id']) / 'meeting.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.active, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)

    def start(self, title, record_audio):
        with self.lock:
            if self.active:
                raise ValueError('请先结束当前会议')
            self.active = {'id': uuid.uuid4().hex, 'title': title or '未命名会议',
                           'started_at': datetime.now(timezone.utc).isoformat(),
                           'ended_at': None, 'record_audio': record_audio, 'tracks': []}
            self.directory(self.active['id']).mkdir(parents=True)
            self.track = 0
            self._save()
            return dict(self.active)

    def audio(self, pcm, rate):
        # Called before VAD: retain quiet audio as well as speech.
        with self.lock:
            if not self.active or not self.active['record_audio']:
                return
            if self.writer is None or rate != self.rate:
                self.close_audio()
                self.track += 1
                name = f'audio-{self.track:03d}.wav'
                self.writer = wave.open(str(self.directory(self.active['id']) / name), 'wb')
                self.writer.setnchannels(1)
                self.writer.setsampwidth(2)
                self.writer.setframerate(rate)
                self.rate = rate
                self.active['tracks'].append({'file': name, 'started_at': time.time(), 'sample_rate': rate})
                self._save()
            self.writer.writeframes(pcm)

    def close_audio(self):
        with self.lock:
            if self.writer:
                self.writer.close()
                self.writer = None

    def append(self, text):
        with self.lock:
            if not self.active:
                return
            row = {'id': uuid.uuid4().hex, 'created_at': datetime.now(timezone.utc).isoformat(), 'text': text}
            directory = self.directory(self.active['id'])
            with (directory / 'transcript.jsonl').open('a', encoding='utf-8') as file:
                file.write(json.dumps(row, ensure_ascii=False) + '\n')
            with (directory / 'transcript.txt').open('a', encoding='utf-8') as file:
                file.write(f"[{row['created_at']}] {text}\n")
            return row

    def append_artifact(self, meeting_id, kind, payload):
        """Bind delayed AI results to their source session, even after finish."""
        if kind not in {'translations', 'answers'}:
            raise ValueError('Unknown artifact type')
        with self.lock:
            directory = self.directory(meeting_id)
            if not (directory / 'meeting.json').is_file():
                raise FileNotFoundError(meeting_id)
            row = {**payload, 'created_at': datetime.now(timezone.utc).isoformat()}
            with (directory / f'{kind}.jsonl').open('a', encoding='utf-8') as file:
                file.write(json.dumps(row, ensure_ascii=False) + '\n')

    @staticmethod
    def _rows(directory, name):
        path = directory / f'{name}.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def finish(self):
        with self.lock:
            if not self.active:
                raise ValueError('没有正在记录的会议')
            self.close_audio()
            self.active['ended_at'] = datetime.now(timezone.utc).isoformat()
            self._save()
            result = dict(self.active)
            self.active = None
            return result

    def read(self, meeting_id):
        with self.lock:
            directory = self.directory(meeting_id)
            result = json.loads((directory / 'meeting.json').read_text(encoding='utf-8'))
            transcript = directory / 'transcript.txt'
            result['transcript'] = transcript.read_text(encoding='utf-8') if transcript.exists() else ''
            result['entries'] = self._rows(directory, 'transcript')
            result['translations'] = self._rows(directory, 'translations')
            result['answers'] = self._rows(directory, 'answers')
            for key in ('summary', 'live-summary'):
                path = directory / f'{key}.txt'
                result[key] = path.read_text(encoding='utf-8') if path.exists() else ''
                meta = directory / f'{key}.json'
                result[f'{key}_source'] = json.loads(meta.read_text(encoding='utf-8')) if meta.exists() else None
            return result

    def list(self):
        with self.lock:
            return sorted([json.loads(p.read_text(encoding='utf-8'))
                           for p in self.root.glob('*/meeting.json')],
                          key=lambda row: row['started_at'], reverse=True)
