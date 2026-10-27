import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.assets import (
    detect_repo_tech_stack,
    extract_repo_context_for_readme,
    scan_repo_environment_variables,
    generate_project_asset
)

class TestAssetsGenerator(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.repo_path = Path(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_detect_repo_tech_stack_dotnet(self):
        (self.repo_path / "MyProject.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>", encoding="utf-8")
        (self.repo_path / "Program.cs").write_text("var builder = WebApplication.CreateBuilder(args);", encoding="utf-8")
        (self.repo_path / "appsettings.json").write_text("{\"Logging\": {}}", encoding="utf-8")

        stack = detect_repo_tech_stack(self.repo_path)
        self.assertIn("C# / .NET", stack["languages"])
        self.assertIn("ASP.NET Core", stack["frameworks"])
        self.assertIn("appsettings.json", stack["manifests"])
        self.assertIn("Program.cs", stack["entrypoints"])

    def test_detect_repo_tech_stack_node_react(self):
        pkg_json = '{"dependencies": {"react": "^18.0.0", "next": "^14.0.0", "tailwindcss": "^3.0.0"}}'
        (self.repo_path / "package.json").write_text(pkg_json, encoding="utf-8")
        (self.repo_path / "src").mkdir()
        (self.repo_path / "src" / "index.tsx").write_text("import React from 'react';", encoding="utf-8")

        stack = detect_repo_tech_stack(self.repo_path)
        self.assertIn("TypeScript", stack["languages"])
        self.assertIn("React", stack["frameworks"])
        self.assertIn("Next.js", stack["frameworks"])
        self.assertIn("Tailwind CSS", stack["frameworks"])
        self.assertIn("package.json", stack["manifests"])

    def test_scan_repo_environment_variables(self):
        # Create files referencing env vars
        src_dir = self.repo_path / "src"
        src_dir.mkdir()
        (src_dir / "config.js").write_text(
            "const dbUrl = process.env.DATABASE_URL;\nconst apiKey = process.env.STRIPE_SECRET_KEY;",
            encoding="utf-8"
        )
        (src_dir / "service.py").write_text(
            "port = os.getenv('PORT', 8080)\ntoken = os.environ.get('AUTH_TOKEN')",
            encoding="utf-8"
        )
        (self.repo_path / "appsettings.json").write_text(
            '{"ConnectionStrings": {"DefaultConnection": "Server=localhost"}, "JwtSettings": {"Secret": "secretkey"}}',
            encoding="utf-8"
        )

        env_keys = scan_repo_environment_variables(self.repo_path)
        var_names = [item["variable"] for item in env_keys]
        self.assertIn("DATABASE_URL", var_names)
        self.assertIn("STRIPE_SECRET_KEY", var_names)
        self.assertIn("AUTH_TOKEN", var_names)
        self.assertIn("ConnectionStrings__DefaultConnection", var_names)
        self.assertIn("JwtSettings__Secret", var_names)

    def test_extract_repo_context_for_readme(self):
        (self.repo_path / "README.md").write_text("# Old Readme\nA simple book service.", encoding="utf-8")
        (self.repo_path / "package.json").write_text('{"name": "book-service"}', encoding="utf-8")

        ctx = extract_repo_context_for_readme("test-repo", self.repo_path)
        self.assertIn("test-repo", ctx)
        self.assertIn("Old Readme", ctx)

    @patch("app.core.assets.call_llm")
    @patch("app.core.assets.get_candidate_directories")
    def test_generate_project_asset_readme(self, mock_candidates, mock_llm):
        mock_candidates.return_value = [self.repo_path]
        mock_llm.return_value = "# My Awesome App\n\nGenerated Documentation"
        (self.repo_path / "package.json").write_text('{"name": "my-app"}', encoding="utf-8")

        result = generate_project_asset(repo_name="my-app", asset_type="readme")
        self.assertTrue(result["success"])
        self.assertEqual(result["filename"], "README.md")
        self.assertEqual(result["content"], "# My Awesome App\n\nGenerated Documentation")

    @patch("app.core.assets.call_llm")
    @patch("app.core.assets.get_candidate_directories")
    def test_generate_project_asset_env(self, mock_candidates, mock_llm):
        mock_candidates.return_value = [self.repo_path]
        mock_llm.return_value = "DATABASE_URL=postgres://...\nPORT=8080"

        result = generate_project_asset(repo_name="my-app", asset_type="env")
        self.assertTrue(result["success"])
        self.assertEqual(result["filename"], ".env.example")
        self.assertIn("DATABASE_URL", result["content"])

if __name__ == "__main__":
    unittest.main()
