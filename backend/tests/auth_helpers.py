"""Shared helpers for tests that call authenticated API endpoints."""
import itertools
import os
import tempfile

from app.core import auth, database
from app.core.database import to_iso, transaction, utcnow

_counter = itertools.count(1)
_TEST_DB_DIR = tempfile.mkdtemp(prefix="preflight-test-db-")


def use_temp_db() -> str:
    """Points the accounts database at a throwaway file for this test process."""
    path = os.path.join(_TEST_DB_DIR, "preflight-test.db")
    if database.DB_PATH != os.path.realpath(path):
        database.configure_db_path(path)
    for limiter in (auth.login_failures_by_user, auth.login_failures_by_ip,
                    auth.recovery_attempts, auth.signup_attempts):
        limiter.clear()
    return path


def grant_repo(user_id: int, repo_name: str, url: str = None) -> None:
    """Records ownership directly (bypasses name-collision rules for fixtures on disk)."""
    now = to_iso(utcnow())
    with transaction() as conn:
        conn.execute("DELETE FROM repositories WHERE repository_name = ? COLLATE NOCASE", (repo_name,))
        conn.execute(
            "INSERT INTO repositories (user_id, repository_url, repository_name, source_type, created_at, updated_at) "
            "VALUES (?, ?, ?, 'test', ?, ?)",
            (user_id, url, repo_name, now, now),
        )


def login_client(client, username: str = None, password: str = "Str0ng!Passw0rd", repos=()):
    """
    Signs a fresh user up through the real API, leaves the session cookie on `client`
    and sets the CSRF header. Returns (user_id, username).
    """
    use_temp_db()
    username = username or f"tester{next(_counter)}_{os.getpid()}"
    resp = client.post("/api/v1/auth/signup", json={
        "username": username, "password": password, "confirm_password": password,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    client.headers.update({auth.CSRF_HEADER_NAME: body["csrf_token"]})
    for name in repos:
        grant_repo(body["user"]["id"], name)
    return body["user"]["id"], username
