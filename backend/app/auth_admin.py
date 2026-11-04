"""
Operator CLI for account administration (run on the server, never exposed over HTTP).

    python -m app.auth_admin list-users
    python -m app.auth_admin reset-link <username> [--base-url http://localhost:8001]
    python -m app.auth_admin assign-repo <repository_name> <username> [--url <git url>]

`reset-link` is the recovery path for users who lost their recovery key: an
operator verifies the person out-of-band and hands them the single-use link.
`assign-repo` gives ownership of repositories that were imported before
accounts existed (unowned repositories are invisible to every user).
"""
import argparse
import sys

from app.core.auth import get_user_by_username, issue_password_reset_token, PASSWORD_RESET_TTL_MINUTES
from app.core.database import DB_PATH, get_db, to_iso, transaction, utcnow
from app.core.security import sanitize_name


def _list_users() -> int:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT u.id, u.username, u.created_at, COUNT(r.id) AS repos "
            "FROM users u LEFT JOIN repositories r ON r.user_id = u.id GROUP BY u.id ORDER BY u.id"
        ).fetchall()
    print(f"Database: {DB_PATH}")
    for r in rows:
        print(f"{r['id']:>5}  {r['username']:<32}  repos={r['repos']:<4}  created={r['created_at']}")
    return 0


def _reset_link(username: str, base_url: str) -> int:
    user = get_user_by_username(username)
    if not user:
        print(f"No such user: {username}", file=sys.stderr)
        return 1
    token, expires_at = issue_password_reset_token(user["id"])
    print(f"Single-use reset link for '{user['username']}' (expires {expires_at}, {PASSWORD_RESET_TTL_MINUTES} min):")
    print(f"{base_url.rstrip('/')}/#reset-password?token={token}")
    return 0


def _assign_repo(repo_name: str, username: str, url: str) -> int:
    clean = sanitize_name(repo_name)
    user = get_user_by_username(username)
    if not clean or not user:
        print("Unknown user or invalid repository name.", file=sys.stderr)
        return 1
    now = to_iso(utcnow())
    with transaction() as conn:
        row = conn.execute(
            "SELECT user_id FROM repositories WHERE repository_name = ? COLLATE NOCASE", (clean,)
        ).fetchone()
        if row and row["user_id"] != user["id"]:
            print(f"'{clean}' is already owned by another user; refusing to reassign.", file=sys.stderr)
            return 1
        if not row:
            conn.execute(
                "INSERT INTO repositories (user_id, repository_url, repository_name, source_type, created_at, updated_at) "
                "VALUES (?, ?, ?, 'legacy', ?, ?)",
                (user["id"], url or None, clean, now, now),
            )
    print(f"Repository '{clean}' is owned by '{user['username']}'.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth_admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-users")
    p_reset = sub.add_parser("reset-link")
    p_reset.add_argument("username")
    p_reset.add_argument("--base-url", default="http://localhost:8001")
    p_assign = sub.add_parser("assign-repo")
    p_assign.add_argument("repository_name")
    p_assign.add_argument("username")
    p_assign.add_argument("--url", default="")
    args = parser.parse_args(argv)

    if args.cmd == "list-users":
        return _list_users()
    if args.cmd == "reset-link":
        return _reset_link(args.username, args.base_url)
    return _assign_repo(args.repository_name, args.username, args.url)


if __name__ == "__main__":
    sys.exit(main())
