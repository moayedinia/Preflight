import os
import sys
import unittest
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
from app.core.llm import fetch_vultr_inference_models, VULTR_INFERENCE_BASE_URL, VULTR_DEFAULT_MODELS
from app.core.sandbox import get_sandbox_capabilities, CloudSandboxDriver
from app.core.supabase_client import (
    get_supabase_config,
    set_supabase_config,
    test_supabase_connection,
    sync_report_to_supabase
)


class TestVultrStandards(unittest.TestCase):
    def setUp(self):
        self.auth_patch = patch("app.core.security.ALLOW_UNAUTHENTICATED", True)
        self.auth_patch.start()
        os.environ["TESTING"] = "1"
        self.client = TestClient(app)

    def tearDown(self):
        self.auth_patch.stop()

    def test_vultr_inference_models_fallback_and_constants(self):
        self.assertEqual(VULTR_INFERENCE_BASE_URL, "https://api.vultrinference.com/v1")
        models = fetch_vultr_inference_models()
        self.assertIsInstance(models, list)
        self.assertGreaterEqual(len(models), 5)
        model_ids = [m["id"] for m in models]
        self.assertIn("deepseek-v4.1-flash-1017", model_ids)

    def test_vultr_models_endpoint(self):
        resp = self.client.get("/api/v1/config/vultr-models")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["base_url"], "https://api.vultrinference.com/v1")
        self.assertGreaterEqual(data["count"], 5)
        self.assertIn("deepseek-v4.1-flash-1017", [m["id"] for m in data["models"]])

    def test_sandbox_capabilities_vultr_hardened(self):
        resp = self.client.get("/api/v1/sandbox/capabilities")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("Vultr", data["security_profile"])
        self.assertEqual(data["memory_quota"], "1g")
        self.assertEqual(data["pids_limit"], 100)
        self.assertEqual(data["cap_drop"], ["ALL"])
        self.assertIn("Vultr Agent Sandboxing (Cloud Compute)", data["standards"])

    def test_cloud_sandbox_driver_e2b_protocol(self):
        driver = CloudSandboxDriver(api_key="", api_url="https://api.e2b.dev")
        self.assertFalse(driver.is_configured())
        res = driver.create_sandbox()
        self.assertEqual(res["status"], "unconfigured")

        # Configured driver mock
        configured_driver = CloudSandboxDriver(api_key="e2b_test_token_123")
        self.assertTrue(configured_driver.is_configured())

    def test_supabase_config_and_test_endpoints(self):
        # 1. Update config
        set_resp = self.client.post("/api/v1/config/supabase", json={
            "url": "https://sample-project.supabase.co",
            "key": "sb_test_service_role_secret_key_1234567890",
            "bucket": "preflight-reports"
        })
        self.assertEqual(set_resp.status_code, 200)
        cfg_data = set_resp.json()["config"]
        self.assertEqual(cfg_data["url"], "https://sample-project.supabase.co")
        self.assertTrue(cfg_data["has_key"])
        self.assertTrue(cfg_data["is_configured"])

        # 2. Get config
        get_resp = self.client.get("/api/v1/config/supabase")
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.json()["bucket"], "preflight-reports")

        # 3. Test connection (in testing mode)
        test_resp = self.client.post("/api/v1/config/supabase/test")
        self.assertEqual(test_resp.status_code, 200)
        self.assertTrue(test_resp.json()["connected"])

        # 4. Sync report helper
        sync_res = sync_report_to_supabase("test-repo", "sample.md", "# Test Report Content")
        self.assertEqual(sync_res["status"], "success")
        self.assertTrue(sync_res["synced"])


if __name__ == "__main__":
    unittest.main()
