import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
os.environ.setdefault("MEETING_ASSISTANT_TOKEN", "test-token")
os.environ["KNOWLEDGE_BASE_DIR"] = tempfile.mkdtemp(prefix="meeting-assistant-api-kb-")

from main import app  # noqa: E402


class ApiSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.headers = {"X-Meeting-Assistant-Token": "test-token"}

    def test_local_api_requires_token(self):
        self.assertEqual(self.client.get("/api/health").status_code, 401)
        response = self.client.get("/api/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

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


if __name__ == "__main__":
    unittest.main()
