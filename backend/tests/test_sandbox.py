import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

mock_modules = {
    "app.core.celery": SimpleNamespace(CELERY_BROKER_URL="redis://localhost:6379/0", celery_app=MagicMock()),
    "app.core.graph": MagicMock(),
    "neo4j": MagicMock()
}
for mod_name, mock_mod in mock_modules.items():
    if mod_name not in sys.modules:
        sys.modules[mod_name] = mock_mod

from fastapi.testclient import TestClient
from app.main import app
from app.core.sandbox import (
    discover_project_launch_plan,
    find_available_port,
    start_sandbox_session,
    stop_sandbox_session,
    get_sandbox_session_status,
    SandboxSession,
    _ACTIVE_SANDBOXES
)


class TestSandboxEngine(unittest.TestCase):
    def setUp(self):
        os.environ["ALLOW_UNAUTHENTICATED"] = "true"
        self.client = TestClient(app)

    def test_find_available_port(self):
        port = find_available_port(start_port=9200, max_port=9250)
        self.assertIsInstance(port, int)
        self.assertGreaterEqual(port, 1024)

    def test_discover_project_launch_plan_node(self):
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            pkg_json = tmp_path / "package.json"
            pkg_json.write_text('{"name": "test-app", "scripts": {"dev": "vite", "build": "vite build"}, "dependencies": {"vite": "^5.0.0"}}', encoding="utf-8")
            readme = tmp_path / "README.md"
            readme.write_text("# Test App\nRun with `npm run dev` to start Vite.", encoding="utf-8")

            with patch("app.core.sandbox.ALLOWED_REPO_DIR", tmp_path.parent):
                plan = discover_project_launch_plan(tmp_path.name)
                self.assertIn("Node.js", plan["project_type"])
                self.assertIn("npm run dev", plan["command"])
                self.assertEqual(plan["default_port"], 5173)

    def test_discover_project_launch_plan_python_fastapi(self):
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            main_py = tmp_path / "main.py"
            main_py.write_text('from fastapi import FastAPI\napp = FastAPI()\n', encoding="utf-8")
            req_txt = tmp_path / "requirements.txt"
            req_txt.write_text('fastapi\nuvicorn\n', encoding="utf-8")

            with patch("app.core.sandbox.ALLOWED_REPO_DIR", tmp_path.parent):
                plan = discover_project_launch_plan(tmp_path.name)
                self.assertIn("Python (FastAPI)", plan["project_type"])
                self.assertIn("uvicorn", plan["command"])
                self.assertIn("main:app", plan["command"])

    def test_sandbox_lifecycle_execution(self):
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with patch("app.core.sandbox.ALLOWED_REPO_DIR", tmp_path.parent):
                port = 9312
                session_dict = start_sandbox_session(
                    repo_name=tmp_path.name,
                    custom_command=f"python -m http.server {port}",
                    custom_port=port
                )
                self.assertIn(session_dict["status"], ["starting", "running"])
                self.assertEqual(session_dict["port"], port)

                status_dict = get_sandbox_session_status(tmp_path.name)
                self.assertIn(status_dict["status"], ["starting", "running"])

                stopped_dict = stop_sandbox_session(tmp_path.name)
                self.assertEqual(stopped_dict["status"], "stopped")

    @patch("app.core.security.ALLOW_UNAUTHENTICATED", True)
    def test_sandbox_api_endpoints(self):
        resp_plan = self.client.get("/api/v1/sandbox/plan")
        self.assertEqual(resp_plan.status_code, 200)
        data = resp_plan.json()
        self.assertIn("command", data)
        self.assertIn("assigned_port", data)

        resp_status = self.client.get("/api/v1/sandbox/status")
        self.assertEqual(resp_status.status_code, 200)

        resp_stop = self.client.post("/api/v1/sandbox/stop", json={"repo_name": "test_repo"})
        self.assertEqual(resp_stop.status_code, 200)

    @patch("app.core.security.ALLOW_UNAUTHENTICATED", True)
    def test_orca_code_review_and_download(self):
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "main.py").write_text("print('hello world')", encoding="utf-8")
            with patch("app.core.sandbox.ALLOWED_REPO_DIR", tmp_path.parent):
                resp_review = self.client.post("/api/v1/sandbox/orca-review", json={"repo_name": tmp_path.name})
                self.assertEqual(resp_review.status_code, 200)
                data = resp_review.json()
                self.assertEqual(data["status"], "success")
                self.assertIn("merge_gate", data)
                self.assertIn("counts", data)
                self.assertIn("markdown_report", data)
                self.assertIn("Orca Code Review Report", data["markdown_report"])

                resp_dl = self.client.get(f"/api/v1/sandbox/orca-review/download?repo_name={tmp_path.name}")
                self.assertEqual(resp_dl.status_code, 200)
                self.assertIn("Orca Code Review Report", resp_dl.text)

    @patch("app.core.security.ALLOW_UNAUTHENTICATED", True)
    def test_diagnose_sandbox_error_and_download(self):
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with patch("app.core.sandbox.ALLOWED_REPO_DIR", tmp_path.parent):
                resp_diag = self.client.post("/api/v1/sandbox/diagnose-error", json={"repo_name": tmp_path.name})
                self.assertEqual(resp_diag.status_code, 200)
                data = resp_diag.json()
                self.assertEqual(data["status"], "success")
                self.assertIn("markdown_report", data)
                self.assertIn("Sandbox Code Error & Crash Report", data["markdown_report"])

                resp_dl = self.client.get(f"/api/v1/sandbox/error-report/download?repo_name={tmp_path.name}")
                self.assertEqual(resp_dl.status_code, 200)
                self.assertIn("Sandbox Code Error & Crash Report", resp_dl.text)


if __name__ == "__main__":
    unittest.main()
