"""
Repository ownership and authorization.

Every repository is identified across the platform (disk folder, Neo4j graph,
sandbox sessions) by its unique `repository_name`. This module records which
user owns each name and is the single place API handlers use to decide whether
the authenticated user may touch a repository. Ownership always comes from the
validated session, never from client-supplied identifiers.
"""
import sqlite3
from typing import Dict, List, Optional, Set

from fastapi import HTTPException

from app.core.auth import CurrentUser
from app.core.database import get_db, to_iso, transaction, utcnow
from app.core.security import ALLOWED_REPO_DIR, sanitize_name

REPO_NOT_FOUND = "Repository not found."


def _not_found() -> HTTPException:
    # Same response whether the repository does not exist or belongs to someone else.
    return HTTPException(status_code=404, detail=REPO_NOT_FOUND)


def list_user_repositories(user_id: int) -> List[Dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, repository_url, repository_name, source_type, created_at, updated_at "
            "FROM repositories WHERE user_id = ? ORDER BY repository_name COLLATE NOCASE",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def owned_repository_names(user_id: int) -> Set[str]:
    return {r["repository_name"] for r in list_user_repositories(user_id)}


def get_owned_repository(user: CurrentUser, repo_name: Optional[str]) -> Dict:
    """Returns the ownership record for `repo_name` or raises 404."""
    try:
        clean = sanitize_name(repo_name) if repo_name else None
    except HTTPException:
        raise _not_found()
    if not clean:
        raise _not_found()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, user_id, repository_url, repository_name, source_type, created_at, updated_at "
            "FROM repositories WHERE repository_name = ? COLLATE NOCASE",
            (clean,),
        ).fetchone()
    if not row or row["user_id"] != user.id:
        raise _not_found()
    return dict(row)


def authorize_repo(user: CurrentUser, repo_name: Optional[str]) -> str:
    """
    Resolves a client-supplied repository name to a repository owned by `user`.

    A missing name is rejected rather than treated as "all repositories", because
    the analysis engines interpret an empty scope as every repository on the server.
    """
    try:
        clean = sanitize_name(repo_name) if repo_name else None
    except HTTPException:
        raise _not_found()
    if not clean:
        raise HTTPException(status_code=400, detail="repo_name is required: select one of your repositories.")
    return get_owned_repository(user, clean)["repository_name"]


def is_owned(user: CurrentUser, repo_name: Optional[str]) -> bool:
    try:
        get_owned_repository(user, repo_name)
        return True
    except HTTPException:
        return False


def _name_used_outside_registry(name: str) -> bool:
    """True when data for `name` exists on disk without an owner (legacy, pre-auth data)."""
    try:
        return (ALLOWED_REPO_DIR / name).exists()
    except OSError:
        return True


def _with_suffix(base: str, n: int) -> str:
    suffix = f"-{n}"
    return base[: 100 - len(suffix)] + suffix


def register_repository(
    user: CurrentUser,
    desired_name: Optional[str],
    repository_url: Optional[str] = None,
    source_type: str = "git",
    explicit_name: bool = False,
) -> str:
    """
    Records that `user` owns a repository and returns the name to store it under.

    - Re-registering a repository the user already owns reuses that record.
    - A name owned by another user (or unowned legacy data on disk) is never taken
      over: an explicitly requested name fails with 409, an inferred name gets a
      numeric suffix.
    """
    clean = sanitize_name(desired_name) if desired_name else None
    if not clean:
        raise HTTPException(status_code=400, detail="A valid repository name is required.")
    url = (repository_url or "").strip() or None
    now = to_iso(utcnow())

    for _ in range(3):  # retry on a concurrent insert of the same name
        try:
            with transaction() as conn:
                if url and not explicit_name:
                    same_source = conn.execute(
                        "SELECT repository_name FROM repositories WHERE user_id = ? AND repository_url = ? "
                        "ORDER BY id LIMIT 1",
                        (user.id, url),
                    ).fetchone()
                    if same_source:
                        conn.execute(
                            "UPDATE repositories SET updated_at = ? WHERE user_id = ? AND repository_name = ?",
                            (now, user.id, same_source["repository_name"]),
                        )
                        return same_source["repository_name"]

                candidate = clean
                n = 1
                while True:
                    row = conn.execute(
                        "SELECT user_id, repository_name FROM repositories WHERE repository_name = ? COLLATE NOCASE",
                        (candidate,),
                    ).fetchone()
                    # Reuse the user's own record when they named it explicitly (re-import),
                    # or for URL-less sources (ZIP re-upload). An inferred name from a
                    # different URL must not overwrite another repository of theirs.
                    if row and row["user_id"] == user.id and (explicit_name or not url):
                        conn.execute(
                            "UPDATE repositories SET repository_url = COALESCE(?, repository_url), "
                            "source_type = ?, updated_at = ? WHERE repository_name = ?",
                            (url, source_type, now, row["repository_name"]),
                        )
                        return row["repository_name"]

                    # If no user owns this record in SQLite:
                    if row is None:
                        # If an unowned directory exists on disk, and explicit_name=True, allow the user to claim it!
                        # If explicit_name=False, avoid colliding with the unowned directory by suffixing (legacy-2).
                        if explicit_name or not _name_used_outside_registry(candidate):
                            break

                    # If another user owns it (or current user with different URL and explicit_name=True):
                    if explicit_name:
                        raise HTTPException(
                            status_code=409,
                            detail=f"The repository name '{clean}' is not available. Please choose another name.",
                        )

                    n += 1
                    if n > 500:
                        raise HTTPException(status_code=409, detail="Could not allocate a repository name.")
                    candidate = _with_suffix(clean, n)

                conn.execute(
                    "INSERT INTO repositories (user_id, repository_url, repository_name, source_type, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user.id, url, candidate, source_type, now, now),
                )
                return candidate
        except sqlite3.IntegrityError:
            continue
    raise HTTPException(status_code=409, detail="Could not register repository. Please try again.")


def unregister_repository(user: CurrentUser, repo_name: str) -> None:
    with transaction() as conn:
        conn.execute(
            "DELETE FROM repositories WHERE user_id = ? AND repository_name = ? COLLATE NOCASE",
            (user.id, repo_name),
        )
        conn.execute(
            "DELETE FROM task_owners WHERE user_id = ? AND repository_name = ? COLLATE NOCASE",
            (user.id, repo_name),
        )


def find_repositories_by_url(repository_url: str) -> List[Dict]:
    """Every ownership record whose source URL matches (used by the push webhook)."""
    candidates = {repository_url.strip(), repository_url.strip().rstrip("/")}
    for c in list(candidates):
        candidates.add(c[:-4] if c.endswith(".git") else c + ".git")
    placeholders = ",".join("?" for _ in candidates)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, user_id, repository_url, repository_name FROM repositories "
            f"WHERE repository_url IN ({placeholders})",
            tuple(candidates),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Background task ownership
# --------------------------------------------------------------------------- #
def record_task_owner(user_id: int, task_id: str, repo_name: Optional[str]) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO task_owners (task_id, user_id, repository_name, created_at) VALUES (?, ?, ?, ?)",
            (task_id, user_id, repo_name, to_iso(utcnow())),
        )


def record_task(user: CurrentUser, task_id: str, repo_name: Optional[str]) -> None:
    record_task_owner(user.id, task_id, repo_name)


def authorize_task(user: CurrentUser, task_id: str) -> Dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT task_id, user_id, repository_name FROM task_owners WHERE task_id = ?", (task_id,)
        ).fetchone()
    if not row or row["user_id"] != user.id:
        raise HTTPException(status_code=404, detail="Task not found.")
    return dict(row)


def claim_unowned_disk_repositories(user_id: int) -> List[str]:
    """Assigns unowned folders in ALLOWED_REPO_DIR to user_id if no other user owns them."""
    claimed = []
    if not ALLOWED_REPO_DIR.exists():
        return claimed
    now = to_iso(utcnow())
    with transaction() as conn:
        for child in ALLOWED_REPO_DIR.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            name = child.name
            row = conn.execute(
                "SELECT user_id FROM repositories WHERE repository_name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO repositories (user_id, repository_url, repository_name, source_type, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'disk', ?, ?)",
                    (user_id, None, name, now, now),
                )
                claimed.append(name)
    return claimed
