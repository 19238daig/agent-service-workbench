from legacy_identity import install_offline_identity
"""Check frontend hosting without exposing project files or breaking API routes."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import smart_cs.api.main as main


class FrontendApiTests(unittest.TestCase):
    def setUp(self):
        install_offline_identity(self, main.app)
        database_patch = patch.object(main, 'configured_database', return_value=None)
        database_patch.start()
        self.addCleanup(database_patch.stop)

    def test_chat_page_and_api_routes_remain_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder)
            (dist / "index.html").write_text('<html lang="zh-CN"><div id="root"></div></html>', encoding="utf-8")
            with patch.object(main, "FRONTEND_DIST", dist), patch.object(main, "initialize_graph"), patch.object(main, "init_tracer"):
                with TestClient(main.app) as client:
                    page = client.get("/")
                    self.assertEqual(page.status_code, 200)
                    self.assertIn('id="root"', page.text)
                    self.assertEqual(page.headers["cache-control"], "no-cache")
                    self.assertEqual(client.get("/docs").status_code, 200)
                    self.assertEqual(client.get("/api/tools").status_code, 200)
                    self.assertEqual(client.get("/.env").status_code, 404)
                    self.assertEqual(client.get("/src/smart_cs/settings.py").status_code, 404)
                    self.assertEqual(client.get("/api/nonexistent").status_code, 404)

    def test_unbuilt_frontend_returns_a_clear_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(main, "FRONTEND_DIST", Path(folder)), patch.object(main, "initialize_graph"), patch.object(main, "init_tracer"):
                with TestClient(main.app) as client:
                    self.assertEqual(client.get("/").status_code, 503)
                    self.assertEqual(client.get("/health").status_code, 200)

    def test_chat_rejects_empty_or_oversize_messages(self):
        for value in ["", "   ", "x" * 8001]:
            with self.subTest(value_length=len(value)), self.assertRaises(ValidationError):
                main.ChatRequest(message=value)
        self.assertEqual(main.ChatRequest(message="  退款问题  ").message, "退款问题")


if __name__ == "__main__":
    unittest.main()
