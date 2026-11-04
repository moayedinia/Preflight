import unittest
import os
import sys
import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

os.environ["TESTING"] = "1"
os.environ["MOCK_LLM"] = "1"

from app.main import app
from app.core.llm import get_llm_config, normalize_llm_base_url
from app.core.preflight import compute_preflight_scoring, get_defects_registry, extract_code_review_defects
from app.core.sandbox import get_sandbox_capabilities
from app.core.cloner import verify_repo_limits, MAX_REPO_SIZE_MB, MAX_REPO_FILE_COUNT


class TestChecklistVerification(unittest.TestCase):
    """
    Verification test suite covering every numbered item in R_CHECKLIST.md:
    Items 1, 2, 3, 4, 5, 6, 7, 8, 9.
    """

    def setUp(self):
        self.client = TestClient(app)

    def test_item1_vultr_inference_default(self):
        """Item 1: Default endpoint must be Vultr Serverless Inference, not OpenAI."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_BASE_URL", None)
            os.environ.pop("LLM_MODEL", None)
            settings = get_llm_config()
            self.assertEqual(settings["base_url"], "https://api.vultrinference.com/v1")
            self.assertEqual(settings["model"], "deepseek-v4.1-flash-1017")

    def test_item2_engine_disclosure(self):
        """Item 2: Explicit engine disclosure ('vultr-inference' vs 'heuristic-fallback')."""
        from app.core.preflight import audit_single_tier
        with patch("app.core.llm.call_llm", side_effect=Exception("API timeout")):
            res = audit_single_tier("red")
            self.assertIn("engine", res)
            self.assertIn(res["engine"], ["vultr-inference", "heuristic-fallback"])

    def test_item3_and_4_five_containment_flags_and_verifiable_flags(self):
        """Item 3 & 4: 5 Containment flags and verifiable-flags endpoint."""
        caps = get_sandbox_capabilities()
        self.assertIn("five_containment_flags", caps)
        self.assertEqual(len(caps["five_containment_flags"]), 5)
        flags = [f["cli_flag"] for f in caps["five_containment_flags"]]
        self.assertIn("--cap-drop=ALL", flags)
        self.assertIn("--security-opt=no-new-privileges:true", flags)
        self.assertIn("--pids-limit=64", flags)
        self.assertIn("--memory=256m", flags)
        self.assertIn("--cpus=0.5", flags)

        res = self.client.get("/api/v1/sandbox/verifiable-flags")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("five_containment_flags", data)

    def test_item5_host_process_fallback_guard(self):
        """Item 5: Host process fallback must be guarded when SANDBOX_RUNNER_URL is unset."""
        from app.core.sandbox import start_sandbox_session
        with patch("app.core.sandbox.SANDBOX_RUNNER_URL", ""), \
             patch.dict(os.environ, {"PREFLIGHT_ALLOW_UNSAFE_LOCAL_RUN": "0", "TESTING": "0"}):
            with self.assertRaises(Exception) as ctx:
                start_sandbox_session("sample_repo", port=3000)
            self.assertTrue("Blast Radius Zero" in str(ctx.exception) or "Isolated container runner required" in str(ctx.exception))

    def test_item6_clean_env_example(self):
        """Item 6: .env.example naming, Vultr inference, and hardened limits."""
        env_path = Path(".env.example")
        self.assertTrue(env_path.exists())
        content = env_path.read_text(encoding="utf-8")
        self.assertNotIn("ShipCheck", content)
        self.assertIn("Preflight", content)
        self.assertIn("https://api.vultrinference.com/v1", content)
        self.assertIn("SANDBOX_MEMORY_LIMIT=256m", content)
        self.assertIn("SANDBOX_PIDS_LIMIT=64", content)
        self.assertIn("SANDBOX_DOCKER_NETWORK=preflight_sandbox_net", content)

    def test_item7_repo_limits(self):
        """Item 7: Repo size cap (50MB) and file count cap (1000 files)."""
        import tempfile
        import shutil
        tmp_dir = Path(tempfile.mkdtemp(prefix="test-repo-limits-"))
        try:
            # Create a file that exceeds size limit
            large_file = tmp_dir / "big.bin"
            large_file.write_bytes(b"0" * 1024)
            # Under limit: should not raise
            verify_repo_limits(tmp_dir)

            # Test size limit triggers
            with patch("app.core.cloner.MAX_REPO_SIZE_MB", 0.0001):  # ~100 bytes limit
                with self.assertRaises(Exception) as ctx:
                    verify_repo_limits(tmp_dir)
                self.assertIn("413", str(ctx.exception))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_item8_scoring_registry_dispatch_and_verdict_fix(self):
        """Item 8: Registry lookup and orange findings mandate launch_with_care verdict."""
        reg = get_defects_registry()
        self.assertGreaterEqual(len(reg["defects"]), 13)

        # Confirm that an orange finding NEVER returns green verdict even if score is high
        findings_with_orange = [
            {
                "id": "ORANGE-07",
                "rule": "No loading state, double submission",
                "tier": "orange",
                "unconfirmed": False
            }
        ]
        score_res = compute_preflight_scoring(findings_with_orange)
        self.assertEqual(score_res["verdict_state"], "launch_with_care")
        self.assertEqual(score_res["verdict_badge"], "🟠 Launch with care")
        self.assertNotEqual(score_res["verdict_state"], "ready")

        # 0 red, 0 orange, score >= 90 returns ready
        score_clean = compute_preflight_scoring([])
        self.assertEqual(score_clean["verdict_state"], "ready")
        self.assertEqual(score_clean["verdict_badge"], "🟢 Ready")

    def test_item9_two_services_in_compose(self):
        """Item 9: docker-compose.yml must define exactly 2 services (api & frontend)."""
        import yaml
        compose_path = Path("docker-compose.yml")
        self.assertTrue(compose_path.exists())
        with open(compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        services = data.get("services", {})
        self.assertEqual(set(services.keys()), {"api", "frontend"}, f"Expected 2 services, got {list(services.keys())}")


    def test_v2_string_boolean_unconfirmed_scoring(self):
        """Checklist v2 Item 1: unconfirmed string boolean bug ('false' vs False)."""
        # String 'false' must deduct points (as confirmed finding)
        findings_str_false = [
            {"id": "RED-01", "rule": "Leaked API keys", "tier": "red", "unconfirmed": "false"}
        ]
        score_false = compute_preflight_scoring(findings_str_false)
        self.assertEqual(score_false["areas"]["security"]["score"], 60)
        self.assertEqual(score_false["verdict_state"], "not_ready")

        # String 'true' must deduct 0 points (unconfirmed finding)
        findings_str_true = [
            {"id": "RED-01", "rule": "Leaked API keys", "tier": "red", "unconfirmed": "true"}
        ]
        score_true = compute_preflight_scoring(findings_str_true)
        self.assertEqual(score_true["areas"]["security"]["score"], 100)
        self.assertEqual(score_true["verdict_state"], "ready")

    def test_v2_clean_report_summary_table_ignored(self):
        """Checklist v2 Item 2: Summary table rows with count 0 must never become findings."""
        report = """# Preflight Code Review Report
## Executive Summary & Metrics Table
| Severity | Level | Count | Action |
| :--- | :--- | :--- | :--- |
| **P0** | Critical Blocker | 0 | ❌ Blocks Deployment |
| **P1** | High Blocker | 0 | ❌ Blocks Deployment |
| **P2** | Medium Warning | 0 | 💬 Review Before Shipping |
| **P3** | Low Nit | 0 | 💡 Optional Hygiene |

## Detailed Findings
No critical code review blockers or defects detected during static heuristic analysis.
"""
        defects = extract_code_review_defects(report)
        self.assertEqual(len(defects), 0)

    def test_v2_port_boundary_no_false_proxy_match(self):
        """Checklist v2 Item 3: Port test must be boundary match; :78901 is NOT proxy :7890."""
        from app.core.llm import is_known_proxy_port
        is_proxy_78901, _ = is_known_proxy_port("https://api.vultrinference.com:78901/v1")
        self.assertFalse(is_proxy_78901)

        is_proxy_7890, p7890 = is_known_proxy_port("http://127.0.0.1:7890/v1")
        self.assertTrue(is_proxy_7890)
        self.assertEqual(p7890, 7890)

    def test_v2_no_key_scan_refuses_callout(self):
        """Checklist v2 Item 4: With no API key, call_llm refuses to call out and leaks no code."""
        from app.core.llm import call_llm
        with patch.dict(os.environ, {"TESTING": "0", "MOCK_LLM": "0", "LLM_API_KEY": "", "VULTR_API_KEY": "", "OPENAI_API_KEY": ""}), \
             patch("app.core.llm.get_llm_config", return_value={"api_key": "", "base_url": "https://api.vultrinference.com/v1"}):
            with self.assertRaises(ValueError) as ctx:
                call_llm("test prompt", "system prompt")
            self.assertIn("No AI API key configured", str(ctx.exception))

    def test_v2_eval_and_registry_in_repository(self):
        """Checklist v2 Item 5: eval/run.mjs and defects/registry.json must exist in repo."""
        self.assertTrue(Path("defects/registry.json").exists())
        self.assertTrue(Path("eval/run.mjs").exists())


if __name__ == "__main__":
    unittest.main()
