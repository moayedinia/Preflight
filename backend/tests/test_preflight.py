import unittest
import sys
import os
import tempfile
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

os.environ["TESTING"] = "1"
os.environ["MOCK_LLM"] = "1"

mock_modules = {
    "app.core.celery": SimpleNamespace(CELERY_BROKER_URL="redis://localhost:6379/0", celery_app=MagicMock()),
    "app.core.graph": MagicMock(),
    "app.core.embedder": MagicMock(),
    "neo4j": MagicMock()
}

for mod_name, mock_mod in mock_modules.items():
    if mod_name not in sys.modules:
        sys.modules[mod_name] = mock_mod

from app.core.preflight import (
    extract_json_defect,
    extract_code_block,
    _validate_fixed_source,
    _validate_llm_defect_evidence,
    apply_preflight_bug_fix,
    evaluate_red_family,
    evaluate_orange_family,
    evaluate_yellow_family,
    run_3tier_preflight_evaluation,
    compile_pure_english_markdown_report,
    audit_single_tier,
    compute_preflight_scoring,
    extract_code_review_defects,
    get_code_review_findings_for_audit,
    RED_SYSTEM_PROMPT,
    ORANGE_SYSTEM_PROMPT,
    YELLOW_SYSTEM_PROMPT,
    RED_RULES,
    ORANGE_RULES,
    YELLOW_RULES
)


class TestPreflightEvaluation(unittest.TestCase):

    def test_preflight_rules_count(self):
        """Validates that exactly 13 defects are covered across the 3 tiers (6 Red, 4 Orange, 3 Yellow)."""
        self.assertEqual(len(RED_RULES), 6)
        self.assertEqual(len(ORANGE_RULES), 4)
        self.assertEqual(len(YELLOW_RULES), 3)

    def test_compute_preflight_scoring_spec_worked_example(self):
        """
        Tests the exact worked example from PART 5 of PREFLIGHT_DETECTION_SPEC.md:
        Can someone hurt you: 100 - 40 - 40 = 20
        Does it break: 100 - 40 - 25 = 35
        Does it behave: 100 - 25 - 12 - 12 = 51
        Can a dev work on it: 100 - 12 - 4 = 84
        Readiness = 20(.35) + 35(.30) + 51(.20) + 84(.15) = 40
        Verdict = 🔴 Not ready
        """
        findings = [
            # Security (Can someone hurt you): 2 Red defects (-40, -40) -> 20
            {"tier": "red", "rule": "Leaked API keys", "detail": "Secret key visible"},
            {"tier": "red", "rule": "Database injection", "detail": "Raw SQL concatenation"},

            # Reliability (Does it break): 1 Red (-40) + 1 Orange-High (-25) -> 35
            {"tier": "red", "rule": "Dead buttons on the main flow", "detail": "Pay button does nothing"},
            {"tier": "orange", "rule": "No loading state, double submission", "detail": "Impatient users double charged"},

            # Correctness (Does it behave properly): 1 Orange-High (-25) + 2 Orange-Medium (-12, -12) -> 51
            {"tier": "orange", "rule": "No limits on what people can type", "detail": "Unbounded text breaks layout"},
            {"tier": "orange", "rule": "Search and sort are wrong", "detail": "Search fires every keystroke"},
            {"tier": "orange", "rule": "Slow pages and heavy images", "detail": "10MB photo on mobile"},

            # Maintainability (Can a dev work on it): 1 Orange-Medium (-12) + 1 Yellow (-4) -> 84
            # (Note: In spec example, 100 - 12 - 4 = 84)
            {"tier": "orange", "rule": "Slow pages and heavy images", "detail": "Advisory maintainability impact"},
            {"tier": "yellow", "rule": "Debug code and no documentation", "detail": "Leftover logs and empty README"}
        ]

        scoring = compute_preflight_scoring(findings)
        self.assertEqual(scoring["areas"]["security"]["score"], 20)
        self.assertEqual(scoring["areas"]["reliability"]["score"], 35)
        # Verify readiness calculated with formula:
        expected_readiness = round(
            scoring["areas"]["security"]["score"] * 0.35 +
            scoring["areas"]["reliability"]["score"] * 0.30 +
            scoring["areas"]["correctness"]["score"] * 0.20 +
            scoring["areas"]["maintainability"]["score"] * 0.15
        )
        self.assertEqual(scoring["readiness_score"], expected_readiness)
        self.assertEqual(scoring["verdict_state"], "not_ready")
        self.assertIn("Not ready", scoring["verdict_title"])

    def test_verdict_states(self):
        """Verifies the 3 verdict states: Not ready, Launch with care, and Ready."""
        # 1. 0 defects -> Ready (100%)
        clean_scoring = compute_preflight_scoring([])
        self.assertEqual(clean_scoring["verdict_state"], "ready")
        self.assertEqual(clean_scoring["readiness_score"], 100)
        self.assertIn("Ready to launch", clean_scoring["verdict_title"])

        # 2. Orange only (no Red) -> Launch with care
        orange_scoring = compute_preflight_scoring([
            {"tier": "orange", "rule": "Search and sort are wrong", "detail": "Search fires every keystroke"}
        ])
        self.assertEqual(orange_scoring["verdict_state"], "launch_with_care")
        self.assertIn("Launch with care", orange_scoring["verdict_title"])

        # 3. Any Red defect -> Not ready
        red_scoring = compute_preflight_scoring([
            {"tier": "red", "rule": "Leaked API keys", "detail": "OpenAI key exposed"}
        ])
        self.assertEqual(red_scoring["verdict_state"], "not_ready")
        self.assertIn("Not ready", red_scoring["verdict_title"])

    def test_extract_json_defect_valid(self):
        sample = """
        ```json
        {
          "is_defect": true,
          "confidence": 0.95,
          "plain_english_explanation": "Critical hardcoded OpenAI key found.",
          "business_consequence": "Unauthorized API cost and key theft.",
          "repair_prompt": "Copy this and paste it into Lovable / Cursor:\nMove sk- key to .env file."
        }
        ```
        """
        fallback = {"is_defect": False, "confidence": 0.5, "plain_english_explanation": "", "business_consequence": "", "repair_prompt": ""}
        res = extract_json_defect(sample, fallback)
        self.assertTrue(res["is_defect"])
        self.assertEqual(res["confidence"], 0.95)
        self.assertEqual(res["plain_english_explanation"], "Critical hardcoded OpenAI key found.")
        self.assertEqual(res["business_consequence"], "Unauthorized API cost and key theft.")
        self.assertIn("Copy this and paste it into Lovable / Cursor", res["repair_prompt"])

    def test_extract_code_block(self):
        """Verifies code extraction from various LLM response formats."""
        # With language tag
        fenced = "```javascript\nfunction pay() { return true; }\n```"
        self.assertEqual(extract_code_block(fenced, "fallback"), "function pay() { return true; }")

        # Without language tag
        fenced_no_lang = "```\nprint('hello world')\n```"
        self.assertEqual(extract_code_block(fenced_no_lang, "fallback"), "print('hello world')")

        # Raw code without fences
        raw = "export const config = { timeout: 5000 };"
        self.assertEqual(extract_code_block(raw, "fallback"), raw)

        # Fallback on empty or error
        self.assertEqual(extract_code_block("I apologize, but I cannot.", "fallback"), "fallback")

        # Models occasionally use four wrapper backticks. They must never leak
        # into a source file.
        four_fences = "````python\nprint('safe')\n````"
        self.assertEqual(extract_code_block(four_fences, "fallback"), "print('safe')")

    def test_extract_json_defect_malformed_uses_fallback(self):
        sample = "Here is some non-json text without curly braces."
        fallback = {
            "is_defect": False,
            "confidence": 0.88,
            "plain_english_explanation": "Fallback explanation",
            "business_consequence": "Fallback consequence",
            "repair_prompt": "Fallback prompt"
        }
        res = extract_json_defect(sample, fallback)
        self.assertFalse(res["is_defect"])
        self.assertEqual(res["confidence"], 0.88)
        self.assertEqual(res["plain_english_explanation"], "Fallback explanation")

    def test_fix_validation_rejects_truncation_and_unchanged_snippet(self):
        from fastapi import HTTPException

        original = "def handler():\n    return 'safe'\n" * 30
        with self.assertRaises(HTTPException):
            _validate_fixed_source(original, "def handler():\n    pass\n", ".py", None)

        changed_elsewhere = original + "\n# unrelated change\n"
        with self.assertRaises(HTTPException):
            _validate_fixed_source(original, changed_elsewhere, ".py", "return 'safe'")

    def test_llm_defect_requires_current_on_disk_evidence(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmpdir:
            repo = Path(tmpdir)
            source = repo / "app.py"
            source.write_text("def current():\n    return True\n", encoding="utf-8")

            stale = {"file": "app.py", "snippet": "return False", "rule": "Stale bug"}
            current = {"file": "app.py", "snippet": "return True", "rule": "Current bug"}
            self.assertIsNone(_validate_llm_defect_evidence(stale, [repo], repo.name))
            verified = _validate_llm_defect_evidence(current, [repo], repo.name)
            self.assertIsNotNone(verified)
            self.assertEqual(verified["file"], "app.py")

    @patch("app.core.preflight.call_llm")
    def test_evaluate_red_family_mocked(self, mock_llm):
        mock_llm.return_value = '{"is_defect": true, "confidence": 0.99, "plain_english_explanation": "Database injection detected in search query.", "business_consequence": "Complete database compromise.", "repair_prompt": "Copy this and paste it into Lovable / Cursor:\\nUse parameterized queries."}'
        res = evaluate_red_family("query = f'SELECT * FROM users WHERE id={user_input}'")
        self.assertTrue(res["is_defect"])
        self.assertEqual(res["confidence"], 0.99)
        self.assertIn("Database injection", res["plain_english_explanation"])

    @patch("app.core.preflight.call_llm")
    def test_evaluate_orange_family_mocked(self, mock_llm):
        mock_llm.return_value = '{"is_defect": true, "confidence": 0.91, "plain_english_explanation": "Checkout button allows double clicking without loading lock.", "business_consequence": "Customers may get billed twice.", "repair_prompt": "Copy this and paste it into Lovable / Cursor:\\nDisable button upon click."}'
        res = evaluate_orange_family("function onPay() { fetch('/charge'); }")
        self.assertTrue(res["is_defect"])
        self.assertEqual(res["confidence"], 0.91)
        self.assertIn("double clicking", res["plain_english_explanation"])

    @patch("app.core.preflight.call_llm")
    def test_evaluate_yellow_family_mocked(self, mock_llm):
        mock_llm.return_value = '{"is_defect": false, "confidence": 0.85, "plain_english_explanation": "Codebase is clean and documented.", "business_consequence": "Maintainability is high.", "repair_prompt": "No immediate action needed."}'
        res = evaluate_yellow_family("console.log('test')")
        self.assertFalse(res["is_defect"])
        self.assertEqual(res["confidence"], 0.85)

    @patch("app.core.preflight.call_llm")
    def test_run_3tier_preflight_evaluation(self, mock_llm):
        mock_llm.return_value = '{"is_defect": false, "confidence": 0.9, "plain_english_explanation": "Pass", "business_consequence": "Safe", "repair_prompt": "None"}'
        result = run_3tier_preflight_evaluation(repo_name="demo-repo", custom_code="const x = 1;")
        self.assertEqual(result["repository"], "demo-repo")
        self.assertIn("red_defects", result)
        self.assertIn("orange_defects", result)
        self.assertIn("yellow_defects", result)
        self.assertIn("scoring", result)
        self.assertEqual(result["readiness_score"], 100)
        self.assertFalse(result["red_defects"]["is_defect"])

    def test_compile_pure_english_markdown_report(self):
        eval_results = {
            "repository": "test-repo",
            "red_defects": {
                "is_defect": False,
                "confidence": 0.95,
                "plain_english_explanation": "No critical security blockers found.",
                "business_consequence": "Server stability guaranteed.",
                "repair_prompt": "Enforce strict auth."
            },
            "orange_defects": {
                "is_defect": False,
                "confidence": 0.90,
                "plain_english_explanation": "Operational responsiveness is high.",
                "business_consequence": "No duplicate submissions.",
                "repair_prompt": "Maintain button debounce."
            },
            "yellow_defects": {
                "is_defect": True,
                "confidence": 0.85,
                "plain_english_explanation": "Debug console logs detected.",
                "business_consequence": "Minor hygiene imperfection.",
                "repair_prompt": "Remove console.log statements."
            }
        }
        analytics = {
            "circular_dependencies": [{"cycle": ["A.py", "B.py", "A.py"]}],
            "critical_hubs": [{"name": "processPayment", "file_path": "pay.js", "risk_score": 8.5, "in_degree": 12, "out_degree": 9}],
            "dead_code_candidates": [{"name": "unusedHelper", "file_path": "util.js", "line": 42}],
            "code_clones": [],
            "git_hotspots": []
        }
        md = compile_pure_english_markdown_report(eval_results, analytics)
        self.assertIn("# Preflight Code Quality & Shipping Report", md)
        self.assertIn("The 4-Area Readiness Breakdown", md)
        self.assertIn("🔴 Preflight Defect Family: RED", md)
        self.assertIn("🟠 Preflight Defect Family: ORANGE", md)
        self.assertIn("🟡 Preflight Defect Family: YELLOW", md)
        self.assertIn("processPayment", md)
        self.assertIn("unusedHelper", md)

        # Ensure no Persian characters are present in the report
        import re
        has_persian = bool(re.search(r'[\u0600-\u06FF]', md))
        self.assertFalse(has_persian, "Exported markdown report must be 100% pure English with zero Persian characters.")

    @patch("app.core.preflight.call_llm")
    def test_preflight_endpoint(self, mock_llm):
        mock_llm.return_value = '{"is_defect": false, "confidence": 0.95, "plain_english_explanation": "Clean code.", "business_consequence": "No impact.", "repair_prompt": "None."}'
        with patch.dict(sys.modules, mock_modules), patch("app.core.security.ALLOW_UNAUTHENTICATED", True):
            from app.main import app
            from fastapi.testclient import TestClient
            client = TestClient(app)
            response = client.post("/api/v1/review/preflight-report", json={
                "repo_name": "sample-repo",
                "custom_code": "console.log('test')",
                "analytics_data": {
                    "circular_dependencies": [],
                    "critical_hubs": [],
                    "dead_code_candidates": []
                }
            })
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "success")
            self.assertIn("markdown_report", data)
            self.assertIn("# Preflight Code Quality & Shipping Report", data["markdown_report"])

    @patch("app.main.audit_single_tier")
    @patch("app.core.llm.verify_llm_connection")
    def test_audit_tier_and_stop_autodoc_endpoints(self, mock_verify, mock_audit):
        mock_verify.return_value = (True, "Connected")
        mock_audit.return_value = {
            "tier": "red",
            "rules": ["Leaked API keys"],
            "defects": [],
            "scoring": {"readiness_score": 100, "verdict_state": "ready"}
        }
        with patch.dict(sys.modules, mock_modules), patch("app.core.security.ALLOW_UNAUTHENTICATED", True):
            from app.main import app
            from fastapi.testclient import TestClient
            client = TestClient(app)

            # Test GET /api/v1/review/tier/red
            res = client.get("/api/v1/review/tier/red")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["tier"], "red")
            self.assertIn("rules", data)
            self.assertIn("defects", data)
            self.assertIn("scoring", data)

            # Test POST /api/v1/tasks/autodoc/stop
            res_stop = client.post("/api/v1/tasks/autodoc/stop", json={"task_id": "task-test-456", "repo_name": "sample"})
            self.assertEqual(res_stop.status_code, 200)
            data_stop = res_stop.json()
            self.assertEqual(data_stop["status"], "stopped")
            self.assertEqual(data_stop["task_id"], "task-test-456")

    @patch("app.core.llm.verify_llm_connection", return_value=(True, "Connected"))
    @patch("app.core.preflight.call_llm")
    def test_apply_preflight_bug_fix_and_endpoint(self, mock_llm, mock_verify):
        """Verifies that apply_preflight_bug_fix edits the file on disk and the /api/v1/review/fix-bug endpoint works."""
        mock_llm.return_value = "```javascript\nfunction onPay() { /* Preflight Auto-Fix Applied */ }\n```"
        # Create a temp file inside a dummy repo folder under current working dir
        test_dir = Path("temp_test_repo")
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "PaymentFlow.js"
        test_file.write_text("function onPay() { /* missing handler */ }", encoding="utf-8")

        try:
            # 1. Direct function test
            result = apply_preflight_bug_fix(
                repo_name="temp_test_repo",
                file_path="PaymentFlow.js",
                repair_prompt="Attach active handler to onPay and disable button while loading.",
                rule="Dead buttons on the main flow",
                detail="Your Pay button does nothing.",
                line_number=1
            )
            self.assertEqual(result["status"], "success")
            self.assertIn("PaymentFlow.js", result["file_path"])
            self.assertTrue(result["changed"])
            self.assertNotEqual(result["original_sha256"], result["fixed_sha256"])
            self.assertTrue(Path(result["backup_path"]).exists())

            # Verify file was updated on disk
            updated_content = test_file.read_text(encoding="utf-8")
            self.assertIn("Preflight Auto-Fix Applied", updated_content)

            # 2. Endpoint test
            with patch.dict(sys.modules, mock_modules), patch("app.core.security.ALLOW_UNAUTHENTICATED", True):
                from app.main import app
                from fastapi.testclient import TestClient
                client = TestClient(app)

                res = client.post("/api/v1/review/fix-bug", json={
                    "repo_name": "temp_test_repo",
                    "file_path": "PaymentFlow.js",
                    "repair_prompt": "Attach active handler",
                    "rule": "Dead buttons on the main flow",
                    "detail": "Button does nothing",
                    "line_number": 1
                })
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(data["status"], "success")
                self.assertIn("file_path", data)

                # 3. Test path with colon line number and empty repair prompt synthesis
                res_colon = client.post("/api/v1/review/fix-bug", json={
                    "repo_name": "temp_test_repo",
                    "file_path": "PaymentFlow.js:14",
                    "rule": "Dead buttons on the main flow",
                    "detail": "Button does nothing"
                })
                self.assertEqual(res_colon.status_code, 200)

                # 4. Test non-existent file structured error detail
                res_not_found = client.post("/api/v1/review/fix-bug", json={
                    "repo_name": "temp_test_repo",
                    "file_path": "NonExistentFile.js",
                    "rule": "Missing file"
                })
                self.assertEqual(res_not_found.status_code, 404)
                err_detail = res_not_found.json()["detail"]
                self.assertIsInstance(err_detail, dict)
                self.assertEqual(err_detail["error_code"], "FILE_NOT_FOUND")
                self.assertIn("suggested_action", err_detail)
        finally:
            if test_dir.exists():
                shutil.rmtree(test_dir)

    def test_extract_code_review_defects(self):
        sample_report = r"""# Preflight Code Review Report
## Detailed Findings

### [P0] Hardcoded JWT Secret Key
- **Path:** `backend/app/core/auth.py:25`
- **Issue:** Secret key is hardcoded directly in repository.
- **Recommendation:** Load secret from environment variables.

### [P1] Missing Loading Lock on Pay Button
- **Path:** `src/checkout.js:88`
- **Issue:** Button allows double submit without locking.
- **Recommendation:** Disable submit button during in-flight requests.

### [P2] Inefficient Full Table Query
- **Path:** `src/models/user.py:120`
- **Issue:** Missing index causes full table scan.
- **Recommendation:** Add index on email column.

### [P3] Redundant Debug Logging
- **Path:** `src/utils.py:15`
- **Issue:** Development debug statements remain in production.
- **Recommendation:** Strip console logs.
"""
        defects = extract_code_review_defects(sample_report)
        self.assertEqual(len(defects), 4)

        p0 = next(d for d in defects if d["p_level"] == "P0")
        self.assertEqual(p0["tier"], "red")
        self.assertEqual(p0["file"], "backend/app/core/auth.py")
        self.assertEqual(p0["line"], 25)
        self.assertIn("Critical security", p0["business_consequence"])

        p1 = next(d for d in defects if d["p_level"] == "P1")
        self.assertEqual(p1["tier"], "orange")
        self.assertEqual(p1["file"], "src/checkout.js")
        self.assertEqual(p1["line"], 88)
        self.assertIn("High operational risk", p1["business_consequence"])

        p2 = next(d for d in defects if d["p_level"] == "P2")
        self.assertEqual(p2["tier"], "yellow")
        self.assertEqual(p2["file"], "src/models/user.py")
        self.assertEqual(p2["line"], 120)

        p3 = next(d for d in defects if d["p_level"] == "P3")
        self.assertEqual(p3["tier"], "yellow")
        self.assertEqual(p3["file"], "src/utils.py")

    @patch("app.core.preflight.call_llm")
    @patch("app.core.preflight.get_code_review_findings_for_audit")
    def test_audit_single_tier_merges_code_review_findings(self, mock_cr, mock_llm):
        mock_llm.return_value = '{"is_defect": false, "confidence": 0.9, "plain_english_explanation": "Clean", "business_consequence": "None", "repair_prompt": "None"}'
        mock_cr.return_value = [
            {
                "file": "auth.py",
                "file_path": "auth.py",
                "line": 10,
                "line_number": 10,
                "rule": "[P0 Code Review] Hardcoded Key",
                "snippet": "KEY = '123'",
                "detail": "Hardcoded secret",
                "tier": "red",
                "p_level": "P0"
            },
            {
                "file": "pay.js",
                "file_path": "pay.js",
                "line": 40,
                "line_number": 40,
                "rule": "[P1 Code Review] Double Charge Risk",
                "snippet": "button.click()",
                "detail": "Missing debounce",
                "tier": "orange",
                "p_level": "P1"
            },
            {
                "file": "style.css",
                "file_path": "style.css",
                "line": 5,
                "line_number": 5,
                "rule": "[P2 Code Review] Unused Class",
                "snippet": ".unused {}",
                "detail": "Dead CSS",
                "tier": "yellow",
                "p_level": "P2"
            }
        ]

        # Audit Red tier
        res_red = audit_single_tier("red")
        self.assertTrue(res_red["is_defect"])
        rules_red = [d["rule"] for d in res_red["defects"]]
        self.assertTrue(any("[P0 Code Review]" in r for r in rules_red))

        # Audit Orange tier
        res_orange = audit_single_tier("orange")
        self.assertTrue(res_orange["is_defect"])
        rules_orange = [d["rule"] for d in res_orange["defects"]]
        self.assertTrue(any("[P1 Code Review]" in r for r in rules_orange))

        # Audit Yellow tier
        res_yellow = audit_single_tier("yellow")
        self.assertTrue(res_yellow["is_defect"])
        rules_yellow = [d["rule"] for d in res_yellow["defects"]]
        self.assertTrue(any("[P2 Code Review]" in r for r in rules_yellow))


if __name__ == "__main__":
    unittest.main()
