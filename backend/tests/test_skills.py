import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

mock_modules = {
    "app.core.celery": SimpleNamespace(CELERY_BROKER_URL="redis://localhost:6379/0", celery_app=MagicMock()),
    "app.core.graph": MagicMock(),
    "app.core.embedder": MagicMock(),
    "neo4j": MagicMock()
}

for mod_name, mock_mod in mock_modules.items():
    if mod_name not in sys.modules:
        sys.modules[mod_name] = mock_mod

from app.core.skills import (
    get_available_skills_metadata,
    dispatch_skill_or_command,
    skill_read_file,
    skill_search_code,
    skill_inspect_routes,
    skill_find_symbol,
    skill_arch_diagram,
    SKILLS_REGISTRY
)


class TestSkillsModule(unittest.TestCase):

    def test_skills_registry_and_metadata(self):
        metadata = get_available_skills_metadata()
        self.assertGreaterEqual(len(metadata), 8)
        skill_ids = [s["id"] for s in metadata]
        self.assertIn("read_file", skill_ids)
        self.assertIn("inspect_routes", skill_ids)
        self.assertIn("search_code", skill_ids)
        self.assertIn("audit_red", skill_ids)
        self.assertIn("audit_orange", skill_ids)
        self.assertIn("audit_yellow", skill_ids)
        self.assertIn("executive_report", skill_ids)

    def test_skill_read_file(self):
        # Read backend/app/main.py
        res = skill_read_file(repo_name=None, file_path="backend/app/main.py", start_line=1, end_line=20)
        self.assertTrue(res["success"])
        self.assertEqual(res["start_line"], 1)
        self.assertIn("Source File:", res["markdown"])

        # Read non-existent file
        res_fail = skill_read_file(repo_name=None, file_path="non_existent_file_xyz.abc")
        self.assertFalse(res_fail["success"])
        self.assertIn("File not found", res_fail["markdown"])

    def test_skill_search_code(self):
        res = skill_search_code(repo_name=None, query="verify_api_key")
        self.assertTrue(res["success"])
        self.assertGreater(res["matches_count"], 0)
        self.assertIn("Search Results for", res["markdown"])

    def test_skill_inspect_routes(self):
        res = skill_inspect_routes(repo_name=None)
        self.assertTrue(res["success"])
        self.assertGreater(res["endpoints_count"], 0)
        self.assertIn("Discovered API Route Endpoints", res["markdown"])

    def test_skill_find_symbol(self):
        res = skill_find_symbol(repo_name=None, symbol_name="verify_api_key")
        self.assertTrue(res["success"])
        self.assertIn("Declaration", res["markdown"])

    def test_skill_arch_diagram(self):
        res = skill_arch_diagram(repo_name=None)
        self.assertTrue(res["success"])
        self.assertIn("flowchart TD", res["mermaid"])
        self.assertIn("Architectural Graph", res["markdown"])

    def test_dispatch_slash_commands(self):
        # /read command
        res_read = dispatch_skill_or_command("/read backend/app/main.py:1:10")
        self.assertIsNotNone(res_read)
        self.assertTrue(res_read["success"])

        # /routes command
        res_routes = dispatch_skill_or_command("/routes")
        self.assertIsNotNone(res_routes)
        self.assertTrue(res_routes["success"])

        # Natural language command
        res_nl = dispatch_skill_or_command("inspect routes")
        self.assertIsNotNone(res_nl)
        self.assertTrue(res_nl["success"])

        # Non-matching natural prompt
        res_chat = dispatch_skill_or_command("What does this project do?")
        self.assertIsNone(res_chat)

    def test_skills_api_endpoints(self):
        with patch.dict(sys.modules, mock_modules), patch("app.core.security.ALLOW_UNAUTHENTICATED", True):
            from app.main import app
            from fastapi.testclient import TestClient
            client = TestClient(app)
            import tempfile, shutil
            from pathlib import Path
            from tests.auth_helpers import login_client
            login_client(client, repos=["skills_fixture_repo"])
            repos_root = Path(tempfile.mkdtemp(prefix="preflight-repos-"))
            fixture = repos_root / "skills_fixture_repo"
            fixture.mkdir()
            (fixture / "api.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/api/v1/items')\ndef items():\n    return []\n",
                encoding="utf-8",
            )
            repos_patch = patch("app.core.preflight.ALLOWED_REPO_DIR", repos_root)
            repos_patch.start()
            try:
                # GET /api/v1/skills
                res = client.get("/api/v1/skills")
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertIn("skills", data)
                self.assertGreaterEqual(len(data["skills"]), 8)

                # POST /api/v1/skills/execute
                res_exec = client.post("/api/v1/skills/execute", json={
                    "skill_name": "inspect_routes", "repo_name": "skills_fixture_repo"
                })
                self.assertEqual(res_exec.status_code, 200)
                data_exec = res_exec.json()
                self.assertTrue(data_exec["success"])

                # Chat with /routes command
                res_chat = client.post("/api/v1/chat", json={
                    "message": "/routes", "repo_name": "skills_fixture_repo"
                })
                self.assertEqual(res_chat.status_code, 200)
                data_chat = res_chat.json()
                self.assertEqual(data_chat["mode"], "skill")
                self.assertIn("Discovered API Route Endpoints", data_chat["response"])
            finally:
                repos_patch.stop()
                shutil.rmtree(repos_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
