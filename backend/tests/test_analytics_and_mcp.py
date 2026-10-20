import sys
import unittest
from unittest.mock import patch, MagicMock

if "neo4j" not in sys.modules:
    sys.modules["neo4j"] = MagicMock()

from app.core.analytics import get_documentation_coverage_stats, get_full_project_code_and_docs_data
from app.core.preflight import audit_single_tier

class AnalyticsCoverageTests(unittest.TestCase):
    @patch("app.core.analytics.driver")
    def test_get_documentation_coverage_stats(self, mock_driver):
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session
        
        mock_counts_res = MagicMock()
        mock_counts_res.single.return_value = {
            "total_functions": 10,
            "documented_functions": 8,
            "total_classes": 3,
            "documented_classes": 3
        }
        
        mock_missing_records = [
            {"name": "foo", "file_path": "a.py", "line": 12, "repo": "test_repo"},
            {"name": "bar", "file_path": "b.py", "line": 45, "repo": "test_repo"}
        ]
        
        mock_session.run.side_effect = [
            mock_counts_res,
            mock_missing_records
        ]
        
        stats = get_documentation_coverage_stats("test_repo")
        self.assertEqual(stats["repo_name"], "test_repo")
        self.assertEqual(stats["total_functions"], 10)
        self.assertEqual(stats["documented_functions"], 8)
        self.assertEqual(stats["undocumented_functions"], 2)
        self.assertEqual(stats["coverage_percent"], 80.0)
        self.assertEqual(len(stats["undocumented_sample"]), 2)

    @patch("app.core.analytics.driver")
    def test_get_full_project_code_and_docs_data(self, mock_driver):
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session
        
        mock_counts_res = MagicMock()
        mock_counts_res.single.return_value = {
            "total_functions": 1,
            "documented_functions": 1,
            "total_classes": 1,
            "documented_classes": 1
        }
        
        mock_files = [{"name": "app.py", "path": "/src/app.py", "repo": "test_repo", "loc": 100}]
        mock_classes = [{"class_name": "Server", "file_path": "/src/app.py", "docstring": "Main server class", "start_line": 5}]
        mock_functions = [{
            "name": "start_server",
            "file_path": "/src/app.py",
            "docstring": "Starts the HTTP listener.",
            "start_line": 20,
            "end_line": 35,
            "code": "def start_server(): pass",
            "is_method": False,
            "callers": ["main"],
            "callees": ["listen"]
        }]
        
        mock_session.run.side_effect = [
            mock_files,
            mock_classes,
            mock_functions,
            mock_counts_res,
            []
        ]
        
        data = get_full_project_code_and_docs_data("test_repo")
        self.assertEqual(data["repo_name"], "test_repo")
        self.assertIn("Complete Codebase & Documentation Dossier", data["markdown_report"])
        self.assertIn("Class `Server`", data["markdown_report"])
        self.assertIn("start_server", data["markdown_report"])
        self.assertIn("Starts the HTTP listener.", data["markdown_report"])

    def test_3tier_preflight_evaluation(self):
        from app.core.preflight import run_3tier_preflight_evaluation
        result = run_3tier_preflight_evaluation(repo_name="test_repo")
        self.assertEqual(result["repository"], "test_repo")
        self.assertIn("red_defects", result)
        self.assertIn("orange_defects", result)
        self.assertIn("yellow_defects", result)
        self.assertIn("rules", result["red_defects"])
        self.assertIn("rules", result["orange_defects"])
        self.assertIn("rules", result["yellow_defects"])

    def test_audit_single_tier_engine(self):
        for tier in ["red", "orange", "yellow"]:
            res = audit_single_tier(tier, repo_name="sample")
            self.assertEqual(res["tier"], tier)
            self.assertIn(res["status"], ["PASS", "DEFECT DETECTED"])
            self.assertIn("rules", res)
            self.assertIn("plain_english_explanation", res)
            self.assertIn("business_consequence", res)
            self.assertIn("repair_prompt", res)
            self.assertIn("markdown_report", res)
            self.assertIn("defects", res)

if __name__ == "__main__":
    unittest.main()
