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
from tests.auth_helpers import login_client, grant_repo
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
        self.user_id, _ = login_client(self.client, repos=["test_repo"])

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
                self.assertEqual(plan["install_command"], "npm install")
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
        resp_plan = self.client.get("/api/v1/sandbox/plan?repo_name=test_repo")
        self.assertEqual(resp_plan.status_code, 200)
        data = resp_plan.json()
        self.assertIn("command", data)
        self.assertIn("assigned_port", data)

        resp_status = self.client.get("/api/v1/sandbox/status?repo_name=test_repo")
        self.assertEqual(resp_status.status_code, 200)

        resp_stop = self.client.post("/api/v1/sandbox/stop", json={"repo_name": "test_repo"})
        self.assertEqual(resp_stop.status_code, 200)

        resp_exec = self.client.post("/api/v1/sandbox/exec", json={"repo_name": "test_repo", "command": "python -c \"print('sandbox exec test ok')\""})
        self.assertEqual(resp_exec.status_code, 200)
        data_exec = resp_exec.json()
        self.assertEqual(data_exec["status"], "success")
        self.assertIn("sandbox exec test ok", data_exec["output"])

    @patch("app.core.sandbox.call_llm")
    @patch("app.core.security.ALLOW_UNAUTHENTICATED", True)
    def test_preflight_code_review_and_download(self, mock_llm):
        mock_llm.return_value = (
            "# Preflight Code Review Report\n"
            "**Repository:** test\n"
            "**Merge Gate:** APPROVED\n"
            "**P0 Issues (Blocker):** 0\n"
            "**P1 Issues (Critical):** 0\n"
            "**P2 Issues (Major):** 0\n"
            "**P3 Issues (Minor / Nit):** 0\n\n"
            "## 1. Executive Summary\nClean codebase."
        )
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "main.py").write_text("print('hello world')", encoding="utf-8")
            grant_repo(self.user_id, tmp_path.name)
            with patch("app.core.sandbox.ALLOWED_REPO_DIR", tmp_path.parent):
                # Test primary preflight review endpoint
                resp_review = self.client.post("/api/v1/sandbox/preflight-review", json={"repo_name": tmp_path.name})
                self.assertEqual(resp_review.status_code, 200)
                data = resp_review.json()
                self.assertEqual(data["status"], "success")
                self.assertIn("merge_gate", data)
                self.assertIn("counts", data)
                self.assertIn("markdown_report", data)
                self.assertIn("Preflight Code Review Report", data["markdown_report"])

                resp_dl = self.client.get(f"/api/v1/sandbox/preflight-review/download?repo_name={tmp_path.name}")
                self.assertEqual(resp_dl.status_code, 200)
                self.assertIn("Preflight Code Review Report", resp_dl.text)

                # Test backward-compatible orca alias endpoint
                resp_orca = self.client.post("/api/v1/sandbox/orca-review", json={"repo_name": tmp_path.name})
                self.assertEqual(resp_orca.status_code, 200)
                self.assertEqual(resp_orca.json()["status"], "success")

                resp_orca_dl = self.client.get(f"/api/v1/sandbox/orca-review/download?repo_name={tmp_path.name}")
                self.assertEqual(resp_orca_dl.status_code, 200)

    @patch("app.core.sandbox.call_llm")
    @patch("app.core.security.ALLOW_UNAUTHENTICATED", True)
    def test_diagnose_sandbox_error_and_download(self, mock_llm):
        mock_llm.return_value = (
            "# Sandbox Code Error & Crash Report\n"
            "**Repository:** test\n"
            "**Status:** Execution Failure Detected\n\n"
            "## 1. Crash Summary\nStartup failure."
        )
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            grant_repo(self.user_id, tmp_path.name)
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

    def test_collect_repo_code_context_bounds(self):
        from app.core.sandbox import collect_repo_code_context
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            # Create large files of different types
            (tmp_path / "Program.cs").write_text("Console.WriteLine(\"test\");\n" * 200, encoding="utf-8")
            (tmp_path / "app.py").write_text("print(\"hello\")\n" * 200, encoding="utf-8")
            (tmp_path / "styles.css").write_text("body { color: red; }\n" * 500, encoding="utf-8")
            (tmp_path / "package-lock.json").write_text("{\n  \"lock\": true\n}\n" * 500, encoding="utf-8")
            (tmp_path / "bundle.min.js").write_text("var a=1;\n" * 500, encoding="utf-8")

            res = collect_repo_code_context(tmp_path, max_files=5, max_bytes_per_file=1500, max_total_bytes=4000)
            self.assertIn("Program.cs", res)
            self.assertIn("app.py", res)
            self.assertNotIn("styles.css", res)
            self.assertNotIn("package-lock.json", res)
            self.assertNotIn("bundle.min.js", res)
            self.assertLessEqual(len(res.encode("utf-8")), 6000)

    @patch("app.core.sandbox.call_llm")
    @patch.dict(os.environ, {"TESTING": "0", "REAL_LLM": "1"})
    def test_preflight_code_review_llm_timeout_fallback(self, mock_llm):
        from app.core.sandbox import run_preflight_code_review
        mock_llm.side_effect = Exception("HTTPConnectionPool(host='host.docker.internal', port=20128): Read timed out. (read timeout=120)")
        
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "server.py").write_text(
                "import os\n"
                "api_key = 'sk-12345678901234567890123456789012'\n"
                "try:\n"
                "    pass\n"
                "except Exception:\n"
                "    pass\n",
                encoding="utf-8"
            )
            with patch("app.core.sandbox.ALLOWED_REPO_DIR", tmp_path.parent):
                res = run_preflight_code_review(repo_name=tmp_path.name)
                self.assertEqual(res["status"], "success")
                self.assertEqual(res["merge_gate"], "BLOCKED")
                self.assertGreater(res["counts"]["P0"] + res["counts"]["P1"], 0)
                self.assertIn("Preflight Code Review Report", res["markdown_report"])
                self.assertIn("Heuristic static analysis", res["markdown_report"])

    @patch("app.core.sandbox.call_llm")
    @patch.dict(os.environ, {"TESTING": "0", "REAL_LLM": "1"})
    def test_diagnose_sandbox_error_llm_timeout_fallback(self, mock_llm):
        from app.core.sandbox import diagnose_sandbox_error
        mock_llm.side_effect = Exception("Read timed out")

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with patch("app.core.sandbox.ALLOWED_REPO_DIR", tmp_path.parent):
                res = diagnose_sandbox_error(repo_name=tmp_path.name, logs=["No logs recorded in sandbox."])
                self.assertEqual(res["status"], "success")
                self.assertIn("Sandbox Code Error & Crash Report", res["markdown_report"])
                self.assertIn("Silent Startup Termination", res["markdown_report"])


if __name__ == "__main__":
    unittest.main()
