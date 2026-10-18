import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

mock_modules = {
    "app.core.celery": SimpleNamespace(CELERY_BROKER_URL="redis://localhost:6379/0", celery_app=MagicMock()),
    "app.core.graph": MagicMock(),
    "app.core.embedder": MagicMock(),
    "neo4j": MagicMock()
}


with patch.dict(sys.modules, mock_modules):
    from app.core.preflight import (
        extract_json_defect,
        evaluate_red_family,
        evaluate_orange_family,
        evaluate_yellow_family,
        run_3tier_preflight_evaluation,
        compile_pure_english_markdown_report,
        RED_SYSTEM_PROMPT,
        ORANGE_SYSTEM_PROMPT,
        YELLOW_SYSTEM_PROMPT
    )


class TestPreflightEvaluation(unittest.TestCase):

    def test_extract_json_defect_valid(self):
        sample = """
        ```json
        {
          "is_defect": true,
          "confidence": 0.95,
          "plain_english_explanation": "Critical hardcoded OpenAI key found.",
          "business_consequence": "Unauthorized API cost and key theft.",
          "repair_prompt": "Move sk- key to .env file."
        }
        ```
        """
        fallback = {"is_defect": False, "confidence": 0.5, "plain_english_explanation": "", "business_consequence": "", "repair_prompt": ""}
        res = extract_json_defect(sample, fallback)
        self.assertTrue(res["is_defect"])
        self.assertEqual(res["confidence"], 0.95)
        self.assertEqual(res["plain_english_explanation"], "Critical hardcoded OpenAI key found.")
        self.assertEqual(res["business_consequence"], "Unauthorized API cost and key theft.")
        self.assertEqual(res["repair_prompt"], "Move sk- key to .env file.")

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

    @patch("app.core.preflight.call_llm")
    def test_evaluate_red_family_mocked(self, mock_llm):
        mock_llm.return_value = '{"is_defect": true, "confidence": 0.99, "plain_english_explanation": "SQL injection detected in route handler.", "business_consequence": "Complete database compromise.", "repair_prompt": "Use ORM parameterization."}'
        res = evaluate_red_family("query = f'SELECT * FROM users WHERE id={user_input}'")
        self.assertTrue(res["is_defect"])
        self.assertEqual(res["confidence"], 0.99)
        self.assertIn("SQL injection", res["plain_english_explanation"])

    @patch("app.core.preflight.call_llm")
    def test_evaluate_orange_family_mocked(self, mock_llm):
        mock_llm.return_value = '{"is_defect": true, "confidence": 0.91, "plain_english_explanation": "Checkout button allows double clicking without loading lock.", "business_consequence": "Customers may get billed twice.", "repair_prompt": "Disable button upon click."}'
        res = evaluate_orange_family("function onPay() { fetch('/charge'); }")
        self.assertTrue(res["is_defect"])
        self.assertEqual(res["confidence"], 0.91)
        self.assertIn("double clicking", res["plain_english_explanation"])

    @patch("app.core.preflight.call_llm")
    def test_evaluate_yellow_family_mocked(self, mock_llm):
        mock_llm.return_value = '{"is_defect": false, "confidence": 0.85, "plain_english_explanation": "Codebase is clean and documented.", "business_consequence": "Maintainability is high.", "repair_prompt": "No immediate hygiene action needed."}'
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
        self.assertIn("# CodeGraph Brain - Architecture & Preflight Code Review Report", md)
        self.assertIn("🔴 Preflight Defect Family: RED", md)
        self.assertIn("🟠 Preflight Defect Family: ORANGE", md)
        self.assertIn("🟡 Preflight Defect Family: YELLOW", md)
        self.assertIn("Circular Dependency Chains", md)
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
            self.assertIn("# CodeGraph Brain - Architecture & Preflight Code Review Report", data["markdown_report"])



if __name__ == "__main__":
    unittest.main()

