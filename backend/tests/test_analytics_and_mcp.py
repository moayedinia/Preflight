import sys
import unittest
from unittest.mock import patch, MagicMock

if "neo4j" not in sys.modules:
    sys.modules["neo4j"] = MagicMock()

from app.core.analytics import get_documentation_coverage_stats, get_full_project_code_and_docs_data

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

if __name__ == "__main__":
    unittest.main()
