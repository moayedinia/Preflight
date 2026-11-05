import io
import zipfile
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.core.security import ALLOWED_REPO_DIR
from tests.auth_helpers import login_client

client = TestClient(app)


def test_download_repository_zip_success(tmp_path):
    repo_name = "test_download_sample"
    login_client(client, repos=[repo_name])

    repo_dir = ALLOWED_REPO_DIR / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Create sample files inside the repo
        (repo_dir / "index.js").write_text("console.log('hello world');", encoding="utf-8")
        (repo_dir / "README.md").write_text("# Sample Project", encoding="utf-8")
        
        # Create ignored items that should not be in zip
        git_dir = repo_dir / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "config").write_text("git config", encoding="utf-8")

        pycache_dir = repo_dir / "__pycache__"
        pycache_dir.mkdir(exist_ok=True)
        (pycache_dir / "cache.pyc").write_bytes(b"byte-code")

        # Call endpoint via path parameter
        res = client.get(f"/api/v1/repos/{repo_name}/download")
        assert res.status_code == 200
        assert res.headers["content-type"] == "application/zip"
        assert f"{repo_name}.zip" in res.headers["content-disposition"]

        # Validate that returned content is a valid ZIP
        zf = zipfile.ZipFile(io.BytesIO(res.content))
        namelist = zf.namelist()
        assert "index.js" in namelist
        assert "README.md" in namelist
        assert not any(".git" in name for name in namelist)
        assert not any("__pycache__" in name for name in namelist)
        assert zf.read("index.js").decode("utf-8") == "console.log('hello world');"

        # Also test via query parameter
        res_query = client.get(f"/api/v1/repos/download?repo_name={repo_name}")
        assert res_query.status_code == 200
        assert res_query.headers["content-type"] == "application/zip"
    finally:
        import shutil
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_download_repository_not_found():
    login_client(client)
    res = client.get("/api/v1/repos/nonexistent_repo_xyz_999/download")
    assert res.status_code == 404


def test_download_repository_path_traversal():
    login_client(client)
    res = client.get("/api/v1/repos/download?repo_name=../../../etc")
    assert res.status_code in (400, 404)
