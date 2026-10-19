import unittest
from fastapi import HTTPException
from app.core.security import sanitize_name

class SecuritySanitizeTests(unittest.TestCase):
    def test_wildcard_all_repositories_returns_none(self):
        self.assertIsNone(sanitize_name("All Repositories"))
        self.assertIsNone(sanitize_name("ALL"))
        self.assertIsNone(sanitize_name("null"))
        self.assertIsNone(sanitize_name("undefined"))
        self.assertIsNone(sanitize_name(""))
        self.assertIsNone(sanitize_name(None))

    def test_repo_with_spaces_allowed(self):
        self.assertEqual(sanitize_name("code graph brain"), "code graph brain")
        self.assertEqual(sanitize_name("my repo 123"), "my repo 123")

    def test_unicode_repo_name_allowed(self):
        self.assertEqual(sanitize_name("repo_alpha_1"), "repo_alpha_1")
        self.assertEqual(sanitize_name("main_project"), "main_project")

    def test_path_traversal_rejected(self):
        with self.assertRaises(HTTPException):
            sanitize_name("../../../etc/passwd")
        with self.assertRaises(HTTPException):
            sanitize_name("repo/subfolder")
        with self.assertRaises(HTTPException):
            sanitize_name("repo\\subfolder")
        with self.assertRaises(HTTPException):
            sanitize_name("repo\x00evil")

if __name__ == "__main__":
    unittest.main()
