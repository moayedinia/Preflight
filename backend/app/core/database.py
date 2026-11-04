"""
SQLite persistence for user accounts, sessions, password reset tokens and
repository ownership.

The database file is created automatically on startup and migrated forward
with versioned, additive migrations. Existing databases are never dropped or
overwritten: each migration runs once and is recorded in `schema_migrations`.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _resolve_db_path() -> Path:
    env_path = (os.getenv("PREFLIGHT_DB_PATH") or "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    # backend/data/preflight.db (inside Docker: /app/data/preflight.db)
    return (Path(__file__).resolve().parent.parent.parent / "data" / "preflight.db")


DB_PATH = _resolve_db_path()

# Ordered, append-only list of migrations. Never edit an applied migration;
# add a new one instead.
MIGRATIONS = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS users (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            username          TEXT    NOT NULL COLLATE NOCASE,
            password_hash     TEXT    NOT NULL,
            recovery_key_hash TEXT,
            created_at        TEXT    NOT NULL,
            updated_at        TEXT    NOT NULL,
            CONSTRAINT uq_users_username UNIQUE (username)
        );

        CREATE TABLE IF NOT EXISTS repositories (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            repository_url  TEXT,
            repository_name TEXT    NOT NULL COLLATE NOCASE,
            source_type     TEXT    NOT NULL DEFAULT 'git',
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL,
            CONSTRAINT uq_repositories_name UNIQUE (repository_name)
        );
        CREATE INDEX IF NOT EXISTS ix_repositories_user_id ON repositories(user_id);
        CREATE INDEX IF NOT EXISTS ix_repositories_user_url ON repositories(user_id, repository_url);

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT    NOT NULL,
            expires_at TEXT    NOT NULL,
            used_at    TEXT,
            created_at TEXT    NOT NULL,
            CONSTRAINT uq_reset_token_hash UNIQUE (token_hash)
        );
        CREATE INDEX IF NOT EXISTS ix_reset_tokens_user_id ON password_reset_tokens(user_id);

        CREATE TABLE IF NOT EXISTS sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash   TEXT    NOT NULL,
            csrf_token   TEXT    NOT NULL,
            expires_at   TEXT    NOT NULL,
            revoked_at   TEXT,
            created_at   TEXT    NOT NULL,
            last_seen_at TEXT    NOT NULL,
            CONSTRAINT uq_sessions_token_hash UNIQUE (token_hash)
        );
        CREATE INDEX IF NOT EXISTS ix_sessions_user_id ON sessions(user_id);

        CREATE TABLE IF NOT EXISTS task_owners (
            task_id         TEXT    PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            repository_name TEXT,
            created_at      TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_task_owners_user_id ON task_owners(user_id);
        """,
    ),
]

_init_lock = threading.Lock()
_initialized_path = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def configure_db_path(path) -> None:
    """Points the module at a different database file (used by tests and admin tools)."""
    global DB_PATH, _initialized_path
    with _init_lock:
        DB_PATH = Path(path).expanduser().resolve()
        _initialized_path = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db() -> None:
    """Creates the database file if needed and applies pending migrations."""
    global _initialized_path
    with _init_lock:
        if _initialized_path == DB_PATH:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect()
        try:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                pass
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, sql in MIGRATIONS:
                if version in applied:
                    continue
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in [s.strip() for s in sql.split(";") if s.strip()]:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, to_iso(utcnow())),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        finally:
            conn.close()
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
        _initialized_path = DB_PATH


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Yields a connection with foreign keys enabled (autocommit mode)."""
    init_db()
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Yields a connection inside a write-locked (IMMEDIATE) transaction."""
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
