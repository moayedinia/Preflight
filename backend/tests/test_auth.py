"""
End-to-end tests for accounts, sessions, password recovery and per-user
repository ownership, exercised through the real FastAPI app.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core import auth, database
from app.core.database import to_iso, transaction, utcnow
from app.main import app
from tests.auth_helpers import grant_repo, use_temp_db

PASSWORD = "Correct-Horse-9"


def _task(task_id):
    return MagicMock(id=task_id)


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.db_path = use_temp_db()
        with transaction() as conn:
            for table in ("task_owners", "password_reset_tokens", "sessions", "repositories", "users"):
                conn.execute(f"DELETE FROM {table}")
        self.repos_root = Path(tempfile.mkdtemp(prefix="preflight-repos-"))
        self.patches = [
            patch("app.core.repo_access.ALLOWED_REPO_DIR", self.repos_root),
            patch("app.main.ALLOWED_REPO_DIR", self.repos_root),
            patch("app.main.list_all_repositories", side_effect=lambda: [
                {"name": d.name, "file_count": 1, "files_count": 1, "total_files": 1, "class_count": 0,
                 "classes_count": 0, "total_classes": 0, "func_count": 0, "functions_count": 0,
                 "total_functions": 0, "languages": ["python"]}
                for d in self.repos_root.iterdir() if d.is_dir()
            ]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    # -- helpers --------------------------------------------------------- #
    def new_client(self):
        return TestClient(app)

    def signup(self, client, username, password=PASSWORD):
        res = client.post("/api/v1/auth/signup", json={
            "username": username, "password": password, "confirm_password": password})
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        client.headers.update({"X-CSRF-Token": body["csrf_token"]})
        return body

    def import_git(self, client, url, name=None, task_id="t-1"):
        with patch("app.main.async_clone_and_index") as task:
            task.delay.return_value = _task(task_id)
            payload = {"git_url": url}
            if name:
                payload["repo_name"] = name
            res = client.post("/api/v1/ingest/git", json=payload)
            return res, task


class SignupLoginTests(AuthTestCase):
    def test_signup_creates_user_with_hashed_password_and_recovery_key(self):
        client = self.new_client()
        body = self.signup(client, "alice")
        self.assertEqual(body["user"]["username"], "alice")
        self.assertRegex(body["recovery_key"], r"^[A-Z2-9]{5}(-[A-Z2-9]{5}){3}$")

        cookie = client.cookies.get(auth.SESSION_COOKIE_NAME)
        self.assertTrue(cookie)
        with sqlite3.connect(self.db_path) as conn:
            pw_hash, rk_hash = conn.execute(
                "SELECT password_hash, recovery_key_hash FROM users WHERE username='alice'").fetchone()
            token_hash = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
        self.assertTrue(pw_hash.startswith("$argon2id$"))
        self.assertNotIn(PASSWORD, pw_hash)
        self.assertTrue(rk_hash.startswith("$argon2id$"))
        self.assertNotEqual(token_hash, cookie, "session tokens must be stored hashed")

    def test_session_cookie_flags(self):
        client = self.new_client()
        res = client.post("/api/v1/auth/signup", json={
            "username": "cookieuser", "password": PASSWORD, "confirm_password": PASSWORD})
        set_cookie = res.headers["set-cookie"].lower()
        self.assertIn("httponly", set_cookie)
        self.assertIn("samesite=lax", set_cookie)
        self.assertIn("path=/", set_cookie)

    def test_duplicate_username_rejected_case_insensitively(self):
        self.signup(self.new_client(), "Bob")
        res = self.new_client().post("/api/v1/auth/signup", json={
            "username": "bob", "password": PASSWORD, "confirm_password": PASSWORD})
        self.assertEqual(res.status_code, 409)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_signup_validation(self):
        client = self.new_client()
        cases = [
            ({"username": "ab", "password": PASSWORD, "confirm_password": PASSWORD}, "Username"),
            ({"username": "bad name!", "password": PASSWORD, "confirm_password": PASSWORD}, "Username"),
            ({"username": "carol", "password": "short1A!", "confirm_password": "short1A!"}, "at least 10"),
            ({"username": "carol", "password": "alllowercaseletters", "confirm_password": "alllowercaseletters"}, "three of"),
            ({"username": "carol", "password": "Carol-Password-1", "confirm_password": "Carol-Password-1"}, "username"),
            ({"username": "carol", "password": PASSWORD, "confirm_password": PASSWORD + "x"}, "do not match"),
        ]
        for payload, fragment in cases:
            res = client.post("/api/v1/auth/signup", json=payload)
            self.assertEqual(res.status_code, 400, payload)
            self.assertIn(fragment.lower(), res.json()["detail"].lower())

    def test_login_success_and_invalid_credentials(self):
        self.signup(self.new_client(), "dave")
        client = self.new_client()
        ok = client.post("/api/v1/auth/login", json={"username": "dave", "password": PASSWORD})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["user"]["username"], "dave")
        self.assertTrue(ok.json()["csrf_token"])
        self.assertEqual(client.get("/api/v1/auth/me").json()["user"]["username"], "dave")

        wrong_pw = self.new_client().post("/api/v1/auth/login", json={"username": "dave", "password": "Wrong-Pass-123"})
        unknown = self.new_client().post("/api/v1/auth/login", json={"username": "nobody", "password": PASSWORD})
        self.assertEqual(wrong_pw.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong_pw.json()["detail"], unknown.json()["detail"], "must not reveal which part was wrong")

    def test_login_rate_limited(self):
        self.signup(self.new_client(), "erin")
        client = self.new_client()
        for _ in range(10):
            self.assertEqual(client.post("/api/v1/auth/login", json={"username": "erin", "password": "nope"}).status_code, 401)
        res = client.post("/api/v1/auth/login", json={"username": "erin", "password": PASSWORD})
        self.assertEqual(res.status_code, 429)

    def test_logout_invalidates_session(self):
        client = self.new_client()
        self.signup(client, "frank")
        token = client.cookies.get(auth.SESSION_COOKIE_NAME)
        self.assertEqual(client.post("/api/v1/auth/logout").status_code, 200)

        replay = self.new_client()
        replay.cookies.set(auth.SESSION_COOKIE_NAME, token)
        self.assertEqual(replay.get("/api/v1/auth/me").status_code, 401)
        self.assertEqual(replay.get("/api/v1/repos").status_code, 401)

    def test_expired_and_idle_sessions_rejected(self):
        client = self.new_client()
        self.signup(client, "gina")
        with transaction() as conn:
            conn.execute("UPDATE sessions SET expires_at = ?", (to_iso(utcnow() - timedelta(seconds=1)),))
        self.assertEqual(client.get("/api/v1/auth/me").status_code, 401)

        client2 = self.new_client()
        client2.post("/api/v1/auth/login", json={"username": "gina", "password": PASSWORD})
        with transaction() as conn:
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE revoked_at IS NULL",
                         (to_iso(utcnow() - timedelta(minutes=auth.SESSION_IDLE_MINUTES + 1)),))
        self.assertEqual(client2.get("/api/v1/auth/me").status_code, 401)

    def test_csrf_token_required_for_state_changes(self):
        client = self.new_client()
        self.signup(client, "hank")
        csrf = client.headers.pop("X-CSRF-Token")
        res = client.post("/api/v1/config/supabase", json={"url": "", "key": ""})
        self.assertEqual(res.status_code, 403)
        res = client.post("/api/v1/config/supabase", json={"url": "", "key": ""}, headers={"X-CSRF-Token": "forged"})
        self.assertEqual(res.status_code, 403)
        res = client.post("/api/v1/config/supabase", json={"url": "", "key": ""}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(res.status_code, 200)

    def test_cross_origin_login_rejected(self):
        self.signup(self.new_client(), "ivan")
        res = self.new_client().post("/api/v1/auth/login", json={"username": "ivan", "password": PASSWORD},
                                     headers={"Origin": "https://evil.example"})
        self.assertEqual(res.status_code, 403)


class ProtectedApiTests(AuthTestCase):
    def test_unauthenticated_requests_are_rejected(self):
        client = self.new_client()
        routes = [
            ("get", "/api/v1/repos"), ("get", "/api/v1/repos/x"), ("delete", "/api/v1/repos/x"),
            ("post", "/api/v1/repos/x/reindex"), ("post", "/api/v1/ingest/git"), ("post", "/api/v1/chat"),
            ("get", "/api/v1/tasks/abc"), ("get", "/api/v1/graph/stats"), ("get", "/api/v1/graph/elements"),
            ("get", "/api/v1/graph/node/x"), ("get", "/api/v1/vector-db/records"),
            ("get", "/api/v1/review/tier/red?repo_name=x"), ("post", "/api/v1/review/fix-bug"),
            ("get", "/api/v1/sandbox/status"), ("post", "/api/v1/sandbox/start"),
            ("get", "/api/v1/sandbox/proxy/x/"), ("get", "/api/v1/config/llm"), ("post", "/api/v1/config/llm"),
            ("get", "/api/v1/mcp/tools"), ("post", "/api/v1/mcp/execute"), ("get", "/api/v1/skills"),
            ("get", "/api/v1/analytics/dead-code"), ("get", "/api/v1/auth/me"),
            ("get", "/api/v1/sandbox/preflight-review/download?repo_name=x"),
        ]
        for method, path in routes:
            res = getattr(client, method)(path)
            self.assertEqual(res.status_code, 401, f"{method.upper()} {path} -> {res.status_code}")

    def test_public_routes_still_public(self):
        client = self.new_client()
        self.assertNotEqual(client.get("/").status_code, 401)

    def test_llm_api_key_is_masked(self):
        client = self.new_client()
        self.signup(client, "kim")
        with patch("app.main.get_llm_config", return_value={"base_url": "https://x/v1", "model": "m", "api_key": "sk-supersecretvalue1234"}):
            body = client.get("/api/v1/config/llm").json()
        self.assertNotIn("supersecret", body["api_key"])
        self.assertTrue(body["api_key_configured"])
        with patch("app.main.get_llm_config", return_value={"base_url": "https://x/v1", "model": "m", "api_key": "sk-supersecretvalue1234"}), \
             patch("app.main.set_llm_config") as set_cfg:
            client.post("/api/v1/config/llm", json={"base_url": "https://x/v1", "model": "m", "api_key": body["api_key"]})
            self.assertEqual(set_cfg.call_args[0][2], "sk-supersecretvalue1234", "masked echo must keep the real key")


class RepositoryOwnershipTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.alice = self.new_client()
        self.alice_id = self.signup(self.alice, "alice")["user"]["id"]
        self.bob = self.new_client()
        self.bob_id = self.signup(self.bob, "bob")["user"]["id"]

    def owner_of(self, name):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT user_id, repository_url FROM repositories WHERE repository_name = ?", (name,)).fetchone()
        return row

    def make_on_disk(self, name):
        d = self.repos_root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "main.py").write_text("print('hi')\n", encoding="utf-8")
        return d

    def test_imported_repository_is_assigned_to_authenticated_user(self):
        res, task = self.import_git(self.alice, "https://github.com/example/repository-a.git")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["repo_name"], "repository-a")
        self.assertEqual(task.delay.call_args.kwargs["repo_name"], "repository-a")
        self.assertEqual(self.owner_of("repository-a"), (self.alice_id, "https://github.com/example/repository-a.git"))

        res, _ = self.import_git(self.bob, "https://github.com/example/repository-b", task_id="t-2")
        self.assertEqual(self.owner_of("repository-b")[0], self.bob_id)

    def test_frontend_cannot_choose_owner(self):
        with patch("app.main.async_clone_and_index") as task:
            task.delay.return_value = _task("t-x")
            self.bob.post("/api/v1/ingest/git", json={
                "git_url": "https://github.com/example/sneaky", "user_id": self.alice_id})
        self.assertEqual(self.owner_of("sneaky")[0], self.bob_id)

    def test_credentials_are_stripped_from_stored_url(self):
        self.import_git(self.alice, "https://user:ghp_secret@github.com/example/private-repo.git?x=1")
        self.assertEqual(self.owner_of("private-repo")[1], "https://github.com/example/private-repo.git")

    def test_duplicate_registration_handling(self):
        url = "https://github.com/example/dup"
        self.import_git(self.alice, url)
        res, _ = self.import_git(self.alice, url, task_id="t-2")
        self.assertEqual(res.json()["repo_name"], "dup", "same user + same URL reuses the record")

        res, _ = self.import_git(self.bob, url, task_id="t-3")
        self.assertEqual(res.json()["repo_name"], "dup-2", "another user's name is never taken over")
        self.assertEqual(self.owner_of("dup")[0], self.alice_id)
        self.assertEqual(self.owner_of("dup-2")[0], self.bob_id)

        res, _ = self.import_git(self.bob, "https://github.com/other/thing", name="dup", task_id="t-4")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.owner_of("dup")[0], self.alice_id)

        res, _ = self.import_git(self.alice, "https://github.com/someone-else/dup", task_id="t-5")
        self.assertEqual(res.json()["repo_name"], "dup-3", "inferred name from a different URL must not overwrite")

        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM repositories WHERE repository_name='dup'").fetchone()[0], 1)

    def test_unowned_legacy_folder_is_not_claimed(self):
        self.make_on_disk("legacy")
        res, _ = self.import_git(self.bob, "https://github.com/x/legacy")
        self.assertEqual(res.json()["repo_name"], "legacy-2")

    def test_zip_upload_registers_owner(self):
        with patch("app.main.async_extract_and_index") as task:
            task.delay.return_value = _task("zip-1")
            res = self.alice.post("/api/v1/ingest/upload", files={"file": ("my-project.zip", b"PK\x05\x06" + b"\x00" * 18)})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["repo_name"], "my-project")
        self.assertEqual(self.owner_of("my-project")[0], self.alice_id)

    def test_listing_only_shows_own_repositories(self):
        for name, uid in (("a-one", self.alice_id), ("a-two", self.alice_id), ("b-one", self.bob_id)):
            self.make_on_disk(name)
            grant_repo(uid, name)
        self.make_on_disk("unowned")
        alice_names = [r["name"] for r in self.alice.get("/api/v1/repos").json()]
        bob_names = [r["name"] for r in self.bob.get("/api/v1/repos").json()]
        self.assertEqual(alice_names, ["a-one", "a-two"])
        self.assertEqual(bob_names, ["b-one"])

    def test_cross_user_access_is_denied_everywhere(self):
        self.make_on_disk("alice-private")
        grant_repo(self.alice_id, "alice-private", "https://github.com/alice/private")
        self.assertEqual(self.alice.get("/api/v1/repos/alice-private").status_code, 200)

        with patch("app.core.llm.verify_llm_connection", return_value=(True, "ok")), \
             patch("app.main.audit_single_tier") as audit, \
             patch("app.main.delete_repository") as delete, \
             patch("app.main.async_index_repository") as reindex, \
             patch("app.core.sandbox.proxy_sandbox_http_request") as proxy:
            attempts = [
                ("get", "/api/v1/repos/alice-private", None),
                ("get", "/api/v1/repos/ALICE-PRIVATE", None),
                ("delete", "/api/v1/repos/alice-private", None),
                ("post", "/api/v1/repos/alice-private/reindex", None),
                ("get", "/api/v1/review/tier/red?repo_name=alice-private", None),
                ("post", "/api/v1/review/tier/red", {"repo_name": "alice-private"}),
                ("post", "/api/v1/review/fix-bug", {"repo_name": "alice-private", "file_path": "main.py"}),
                ("post", "/api/v1/chat", {"message": "hi", "repo_name": "alice-private"}),
                ("post", "/api/v1/chat", {"message": "/read main.py", "repo_name": "alice-private", "mode": "direct"}),
                ("post", "/api/v1/skills/execute", {"skill_name": "read_file", "repo_name": "alice-private", "params": {"file_path": "main.py"}}),
                ("get", "/api/v1/graph/elements?repo_name=alice-private", None),
                ("get", "/api/v1/vector-db/records?repo_name=alice-private", None),
                ("get", "/api/v1/analytics/dead-code?repo_name=alice-private", None),
                ("get", "/api/v1/sandbox/status?repo_name=alice-private", None),
                ("post", "/api/v1/sandbox/start", {"repo_name": "alice-private"}),
                ("post", "/api/v1/sandbox/stop", {"repo_name": "alice-private"}),
                ("get", "/api/v1/sandbox/preflight-review/download?repo_name=alice-private", None),
                ("get", "/api/v1/sandbox/proxy/alice-private/", None),
                ("post", "/api/v1/sandbox/proxy/alice-private/api/data", {}),
                ("post", "/api/v1/assets/generate", {"repo_name": "alice-private", "asset_type": "readme"}),
                ("post", "/api/v1/analyze/code", {"repo_name": "alice-private", "file_path": "x.py", "code_content": "x=1"}),
                ("post", "/api/v1/analyze/repo", {"repo_path": str(self.repos_root / "alice-private"), "repo_name": "mine"}),
                ("post", "/api/v1/mcp/execute", {"tool_name": "read_repository_file", "arguments": {"file_path": "main.py", "repo_name": "alice-private"}}),
            ]
            for method, path, body in attempts:
                kwargs = {"json": body} if body is not None else {}
                res = getattr(self.bob, method)(path, **kwargs)
                self.assertIn(res.status_code, (403, 404, 409), f"{method.upper()} {path} -> {res.status_code} {res.text[:200]}")
                self.assertNotIn("github.com/alice", res.text)
            audit.assert_not_called()
            delete.assert_not_called()
            reindex.delay.assert_not_called()
            proxy.assert_not_called()

        self.assertTrue((self.repos_root / "alice-private").exists())
        self.assertEqual(self.owner_of("alice-private")[0], self.alice_id)

    def test_repo_scope_is_required(self):
        with patch("app.core.llm.verify_llm_connection", return_value=(True, "ok")):
            for path in ("/api/v1/review/tier/red", "/api/v1/sandbox/plan", "/api/v1/graph/elements",
                         "/api/v1/analytics/hotspots", "/api/v1/review/preflight-status"):
                res = self.alice.get(path)
                self.assertEqual(res.status_code, 400, path)

    def test_owner_can_use_repository(self):
        self.make_on_disk("mine")
        grant_repo(self.alice_id, "mine")
        with patch("app.core.llm.verify_llm_connection", return_value=(True, "ok")), \
             patch("app.main.audit_single_tier", return_value={"tier": "red", "defects": []}) as audit:
            res = self.alice.get("/api/v1/review/tier/red?repo_name=mine")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(audit.call_args.kwargs["repo_name"], "mine")

        with patch("app.main.delete_repository", return_value={"status": "success"}):
            self.assertEqual(self.alice.delete("/api/v1/repos/mine").status_code, 200)
        self.assertIsNone(self.owner_of("mine"))

    def test_task_status_is_private(self):
        self.import_git(self.alice, "https://github.com/example/tasky", task_id="task-alice")
        with patch("app.main.celery_app") as celery:
            celery.AsyncResult.return_value = MagicMock(status="PENDING", info={}, ready=lambda: False)
            self.assertEqual(self.alice.get("/api/v1/tasks/task-alice").status_code, 200)
            self.assertEqual(self.bob.get("/api/v1/tasks/task-alice").status_code, 404)
            self.assertEqual(self.bob.get("/api/v1/tasks/unknown-task").status_code, 404)

    def test_graph_node_lookup_checks_owner(self):
        grant_repo(self.alice_id, "graph-repo")
        node = {"id": "fn:1", "type": "Function", "properties": {"repo": "graph-repo", "code": "secret()"}}
        with patch("app.main.get_node_details", return_value=node):
            self.assertEqual(self.alice.get("/api/v1/graph/node/fn:1").status_code, 200)
            res = self.bob.get("/api/v1/graph/node/fn:1")
            self.assertEqual(res.status_code, 404)
            self.assertNotIn("secret", res.text)
        with patch("app.main.get_node_vector", return_value={"id": "fn:1", "repo": "graph-repo", "vector": [0.1]}):
            self.assertEqual(self.bob.get("/api/v1/vector-db/vector/fn:1").status_code, 404)
            self.assertEqual(self.alice.get("/api/v1/vector-db/vector/fn:1").status_code, 200)

    def test_graph_stats_scoped_to_owner(self):
        grant_repo(self.alice_id, "stats-repo")
        with patch("app.main.get_graph_stats", return_value={"repositories": 1}) as stats:
            self.alice.get("/api/v1/graph/stats")
            self.assertEqual(stats.call_args.kwargs["repo_names"], ["stats-repo"])
            self.bob.get("/api/v1/graph/stats")
            self.assertEqual(stats.call_args.kwargs["repo_names"], [])

    def test_mcp_execute_policy(self):
        grant_repo(self.alice_id, "mcp-repo")
        res = self.alice.post("/api/v1/mcp/execute", json={"tool_name": "clone_and_index_git", "arguments": {"git_url": "https://x/y"}})
        self.assertEqual(res.status_code, 403)
        res = self.alice.post("/api/v1/mcp/execute", json={"tool_name": "get_codebase_stats"})
        self.assertEqual(res.status_code, 403)
        res = self.alice.post("/api/v1/mcp/execute", json={"tool_name": "list_repositories"})
        self.assertIn("mcp-repo", res.json()["result"])
        self.assertNotIn("mcp-repo", self.bob.post("/api/v1/mcp/execute", json={"tool_name": "list_repositories"}).json()["result"])

    def test_nl2cypher_disabled(self):
        grant_repo(self.alice_id, "cy-repo")
        with patch("app.core.rag.execute_nl2cypher_query") as nl2:
            res = self.alice.post("/api/v1/chat", json={"message": "/cypher MATCH (n) RETURN n", "repo_name": "cy-repo"})
            self.assertEqual(res.json()["warning"], "nl2cypher_disabled")
            nl2.assert_not_called()

    def test_webhook_only_refreshes_registered_repositories(self):
        grant_repo(self.alice_id, "hooked", "https://github.com/example/hooked.git")
        body = b'{"ref": "refs/heads/main", "repository": {"name": "hooked", "clone_url": "https://github.com/example/hooked.git"}}'
        unknown = b'{"ref": "refs/heads/main", "repository": {"name": "hooked", "clone_url": "https://github.com/attacker/hooked.git"}}'
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "dev-insecure-webhook"}), \
             patch("app.main.async_clone_and_index") as task:
            task.delay.return_value = _task("hook-1")
            res = self.new_client().post("/api/v1/webhooks/github", content=body, headers={"X-GitHub-Event": "push"})
            self.assertEqual(res.json()["status"], "accepted")
            self.assertEqual(task.delay.call_args.kwargs["repo_name"], "hooked")
            task.delay.reset_mock()
            res = self.new_client().post("/api/v1/webhooks/github", content=unknown, headers={"X-GitHub-Event": "push"})
            self.assertEqual(res.json()["status"], "ignored")
            task.delay.assert_not_called()
        with patch("app.main.celery_app") as celery:
            celery.AsyncResult.return_value = MagicMock(status="PENDING", info={}, ready=lambda: False)
            self.assertEqual(self.alice.get("/api/v1/tasks/hook-1").status_code, 200)


class PasswordRecoveryTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.client = self.new_client()
        self.recovery_key = self.signup(self.client, "rita")["recovery_key"]

    def forgot(self, key, username="rita"):
        return self.new_client().post("/api/v1/auth/password/forgot", json={"username": username, "recovery_key": key})

    def reset(self, token, pw="Brand-New-Pass-42", confirm=None):
        return self.new_client().post("/api/v1/auth/password/reset", json={
            "token": token, "new_password": pw, "confirm_password": confirm or pw})

    def test_full_recovery_flow(self):
        self.assertEqual(self.forgot("AAAAA-BBBBB-CCCCC-DDDDD").status_code, 400)
        self.assertEqual(self.forgot(self.recovery_key, username="nobody").status_code, 400)

        res = self.forgot(self.recovery_key.lower().replace("-", " "))  # tolerant formatting
        self.assertEqual(res.status_code, 200, res.text)
        token = res.json()["reset_token"]
        with sqlite3.connect(self.db_path) as conn:
            stored = conn.execute("SELECT token_hash FROM password_reset_tokens WHERE used_at IS NULL").fetchone()[0]
        self.assertNotEqual(stored, token, "reset tokens must be stored hashed")

        self.assertEqual(self.reset(token, "weak").status_code, 400)
        res = self.reset(token)
        self.assertEqual(res.status_code, 200, res.text)
        new_key = res.json()["recovery_key"]
        self.assertNotEqual(new_key, self.recovery_key)

        # single use
        self.assertEqual(self.reset(token, "Another-Pass-99").status_code, 400)
        # existing sessions were revoked
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 401)
        # old password fails, new password works
        self.assertEqual(self.new_client().post("/api/v1/auth/login", json={"username": "rita", "password": PASSWORD}).status_code, 401)
        self.assertEqual(self.new_client().post("/api/v1/auth/login", json={"username": "rita", "password": "Brand-New-Pass-42"}).status_code, 200)
        # recovery key rotated
        self.assertEqual(self.forgot(self.recovery_key).status_code, 400)
        self.assertEqual(self.forgot(new_key).status_code, 200)

    def test_expired_token_rejected(self):
        token = self.forgot(self.recovery_key).json()["reset_token"]
        with transaction() as conn:
            conn.execute("UPDATE password_reset_tokens SET expires_at = ?", (to_iso(utcnow() - timedelta(seconds=1)),))
        self.assertEqual(self.reset(token).status_code, 400)

    def test_new_request_invalidates_older_token(self):
        first = self.forgot(self.recovery_key).json()["reset_token"]
        second = self.forgot(self.recovery_key).json()["reset_token"]
        self.assertEqual(self.reset(first).status_code, 400)
        self.assertEqual(self.reset(second).status_code, 200)

    def test_admin_reset_link(self):
        from app import auth_admin
        with patch("builtins.print") as out:
            self.assertEqual(auth_admin.main(["reset-link", "rita", "--base-url", "http://localhost:8001"]), 0)
        link = next(c.args[0] for c in out.call_args_list if "#reset-password?token=" in str(c.args[0]))
        token = link.split("token=")[1]
        self.assertEqual(self.reset(token).status_code, 200)


class DatabaseTests(AuthTestCase):
    def test_foreign_keys_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with transaction() as conn:
                conn.execute(
                    "INSERT INTO repositories (user_id, repository_name, created_at, updated_at) VALUES (999999, 'orphan', 'x', 'x')")

    def test_migrations_are_idempotent_and_preserve_data(self):
        self.signup(self.new_client(), "persist")
        database.configure_db_path(self.db_path)  # forces init_db to run again
        database.init_db()
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users WHERE username='persist'").fetchone()[0], 1)
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        self.assertEqual(versions, [1])

    def test_deleting_user_cascades_ownership(self):
        uid = self.signup(self.new_client(), "temp")["user"]["id"]
        grant_repo(uid, "temp-repo")
        with transaction() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM repositories WHERE repository_name='temp-repo'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
