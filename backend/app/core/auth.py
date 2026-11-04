"""
User accounts, password hashing, server-side sessions, CSRF protection and
password recovery.

- Passwords and recovery keys are hashed with Argon2id (argon2-cffi).
- Sessions are opaque random tokens kept in an HttpOnly cookie; only their
  SHA-256 hash is stored in SQLite, so a database leak does not leak sessions.
- State-changing requests made with a session must carry the session's CSRF
  token in the `X-CSRF-Token` header (synchronizer-token pattern).
- Password reset tokens are random, stored hashed, expire quickly and are
  single-use.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response, status

from app.core.database import from_iso, get_db, to_iso, transaction, utcnow

SESSION_COOKIE_NAME = "preflight_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


SESSION_TTL_HOURS = _int_env("SESSION_TTL_HOURS", 24)
SESSION_IDLE_MINUTES = _int_env("SESSION_IDLE_MINUTES", 480)
PASSWORD_RESET_TTL_MINUTES = _int_env("PASSWORD_RESET_TTL_MINUTES", 15)

_password_hasher = PasswordHasher()  # Argon2id with argon2-cffi's recommended parameters
# Verified against when a username does not exist, so response time does not reveal it.
_DUMMY_HASH = _password_hasher.hash(secrets.token_urlsafe(16))

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{1,30})[A-Za-z0-9]$")
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
_COMMON_PASSWORDS = {
    "password", "password1", "password123", "123456789", "1234567890", "qwertyuiop",
    "iloveyou", "letmein123", "welcome123", "admin12345", "passw0rd!", "changeme123",
}


@dataclass
class CurrentUser:
    id: int
    username: str
    session_id: int
    csrf_token: str


# --------------------------------------------------------------------------- #
# Hashing helpers
# --------------------------------------------------------------------------- #
def hash_secret(value: str) -> str:
    return _password_hasher.hash(value)


def verify_secret(stored_hash: Optional[str], value: str) -> bool:
    if not stored_hash:
        return False
    try:
        return _password_hasher.verify(stored_hash, value)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_recovery_key() -> str:
    """Returns a 100-bit recovery key formatted as XXXXX-XXXXX-XXXXX-XXXXX."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(20))
    return "-".join(raw[i:i + 5] for i in range(0, 20, 5))


def normalize_recovery_key(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    return "-".join(cleaned[i:i + 5] for i in range(0, len(cleaned), 5))


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_PATTERN.match(username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-32 characters, start and end with a letter or digit, "
                   "and contain only letters, digits, '.', '_' or '-'.",
        )
    return username


def password_strength_errors(password: str, username: str = "") -> List[str]:
    errors = []
    password = password or ""
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"Password must be at least {PASSWORD_MIN_LENGTH} characters long.")
    if len(password) > PASSWORD_MAX_LENGTH:
        errors.append(f"Password must be at most {PASSWORD_MAX_LENGTH} characters long.")
    classes = sum([
        bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[A-Z]", password)),
        bool(re.search(r"\d", password)),
        bool(re.search(r"[^A-Za-z0-9]", password)),
    ])
    if classes < 3:
        errors.append("Password must contain at least three of: lowercase letters, uppercase letters, digits, symbols.")
    if username and len(username) >= 3 and username.lower() in password.lower():
        errors.append("Password must not contain your username.")
    if password.lower() in _COMMON_PASSWORDS:
        errors.append("Password is too common.")
    return errors


def ensure_password_strength(password: str, confirm_password: str, username: str = "") -> None:
    if password != confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    errors = password_strength_errors(password, username)
    if errors:
        raise HTTPException(status_code=400, detail=" ".join(errors))


# --------------------------------------------------------------------------- #
# Rate limiting (per process, sliding window)
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self, max_events: int, window_seconds: int):
        self.max_events = max_events
        self.window = window_seconds
        self._events: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> Deque[float]:
        q = self._events.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def is_limited(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.max_events

    def hit(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


login_failures_by_user = RateLimiter(max_events=10, window_seconds=15 * 60)
login_failures_by_ip = RateLimiter(max_events=50, window_seconds=15 * 60)
recovery_attempts = RateLimiter(max_events=10, window_seconds=15 * 60)
signup_attempts = RateLimiter(max_events=20, window_seconds=60 * 60)


def client_ip(request: Request) -> str:
    if os.getenv("TRUST_PROXY_HEADERS", "false").lower() in ("1", "true", "yes"):
        forwarded = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
        if forwarded.strip():
            return forwarded.strip()
    return request.client.host if request.client else "unknown"


def raise_rate_limited() -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many attempts. Please wait a few minutes and try again.",
    )


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def create_user(username: str, password: str) -> Tuple[Dict, str]:
    """Creates a user and returns (user_row, plaintext_recovery_key)."""
    username = validate_username(username)
    recovery_key = generate_recovery_key()
    password_hash = hash_secret(password)
    recovery_hash = hash_secret(recovery_key)
    now = to_iso(utcnow())
    try:
        with transaction() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, recovery_key_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, password_hash, recovery_hash, now, now),
            )
            user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="That username is already taken.")
    return {"id": user_id, "username": username, "created_at": now}, recovery_key


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", ((username or "").strip(),)
        ).fetchone()


def authenticate(username: str, password: str) -> Optional[sqlite3.Row]:
    user = get_user_by_username(username)
    if not user:
        verify_secret(_DUMMY_HASH, password)
        return None
    if not verify_secret(user["password_hash"], password):
        return None
    try:
        if _password_hasher.check_needs_rehash(user["password_hash"]):
            with transaction() as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                    (hash_secret(password), to_iso(utcnow()), user["id"]),
                )
    except Exception:
        pass
    return user


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def create_session(user_id: int) -> Tuple[str, str]:
    """Creates a session and returns (session_token, csrf_token)."""
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = utcnow()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, csrf_token, expires_at, created_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, _sha256(token), csrf_token, to_iso(now + timedelta(hours=SESSION_TTL_HOURS)),
             to_iso(now), to_iso(now)),
        )
        # Opportunistic cleanup of long-dead sessions.
        conn.execute(
            "DELETE FROM sessions WHERE expires_at < ? OR revoked_at IS NOT NULL AND revoked_at < ?",
            (to_iso(now - timedelta(days=7)), to_iso(now - timedelta(days=7))),
        )
    return token, csrf_token


def resolve_session(token: Optional[str]) -> Optional[CurrentUser]:
    if not token or len(token) > 200:
        return None
    now = utcnow()
    with get_db() as conn:
        row = conn.execute(
            "SELECT s.id AS session_id, s.csrf_token, s.expires_at, s.revoked_at, s.last_seen_at, "
            "       u.id AS user_id, u.username "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (_sha256(token),),
        ).fetchone()
        if not row or row["revoked_at"]:
            return None
        if from_iso(row["expires_at"]) <= now:
            return None
        last_seen = from_iso(row["last_seen_at"])
        if now - last_seen > timedelta(minutes=SESSION_IDLE_MINUTES):
            return None
        if (now - last_seen).total_seconds() > 60:
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (to_iso(now), row["session_id"]))
    return CurrentUser(
        id=row["user_id"], username=row["username"],
        session_id=row["session_id"], csrf_token=row["csrf_token"],
    )


def revoke_session(session_id: int) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (to_iso(utcnow()), session_id),
        )


def revoke_all_sessions(user_id: int) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (to_iso(utcnow()), user_id),
        )


def _cookie_secure(request: Request) -> bool:
    mode = os.getenv("COOKIE_SECURE", "auto").strip().lower()
    if mode in ("1", "true", "yes"):
        return True
    if mode in ("0", "false", "no"):
        return False
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME, path="/", httponly=True, secure=_cookie_secure(request), samesite="lax"
    )


# --------------------------------------------------------------------------- #
# Origin / CSRF checks
# --------------------------------------------------------------------------- #
def _allowed_origin_hosts() -> List[str]:
    raw = os.getenv("ALLOWED_ORIGINS", "")
    hosts = []
    for origin in raw.split(","):
        origin = origin.strip()
        if origin and origin != "*":
            hosts.append((urlparse(origin).hostname or "").lower())
    return [h for h in hosts if h]


def ensure_same_origin(request: Request) -> None:
    """Rejects cross-site state-changing requests based on the Origin/Referer header."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return  # Non-browser clients send neither; SameSite cookies cover browsers.
    origin_host = (urlparse(source).hostname or "").lower()
    request_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "")
    request_host = (urlparse("//" + request_host.split(",")[0].strip()).hostname or "").lower()
    if origin_host and (origin_host == request_host or origin_host in _allowed_origin_hosts()):
        return
    raise HTTPException(status_code=403, detail="Cross-origin request rejected.")


def _is_csrf_exempt(path: str) -> bool:
    # The sandbox preview proxy forwards the previewed app's own form posts/XHRs,
    # which cannot carry our header. It is still authenticated and ownership-checked,
    # and the SameSite=Lax session cookie is not sent on cross-site POSTs.
    return path.startswith("/api/v1/sandbox/proxy/")


def require_user(request: Request) -> CurrentUser:
    """FastAPI dependency: returns the authenticated user or raises 401/403."""
    user = resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
        )
    if request.method.upper() not in SAFE_METHODS and not _is_csrf_exempt(request.url.path):
        supplied = request.headers.get(CSRF_HEADER_NAME, "")
        if not supplied or not hmac.compare_digest(supplied, user.csrf_token):
            raise HTTPException(status_code=403, detail="Missing or invalid CSRF token.")
    request.state.user = user
    return user


# --------------------------------------------------------------------------- #
# Password recovery
# --------------------------------------------------------------------------- #
def issue_password_reset_token(user_id: int) -> Tuple[str, str]:
    """Creates a single-use reset token. Returns (token, expires_at_iso)."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    expires_at = to_iso(now + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES))
    with transaction() as conn:
        # Only the newest token is valid: retire any outstanding ones.
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (to_iso(now), user_id),
        )
        conn.execute(
            "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (user_id, _sha256(token), expires_at, to_iso(now)),
        )
    return token, expires_at


def request_reset_with_recovery_key(username: str, recovery_key: str) -> Optional[Tuple[str, str]]:
    """Verifies the username + recovery key pair and returns a reset token on success."""
    user = get_user_by_username(username)
    if not user:
        verify_secret(_DUMMY_HASH, recovery_key)
        return None
    if not verify_secret(user["recovery_key_hash"], normalize_recovery_key(recovery_key)):
        return None
    return issue_password_reset_token(user["id"])


def reset_password_with_token(token: str, new_password: str, confirm_password: str) -> Tuple[str, str]:
    """
    Consumes a reset token, sets the new password, revokes every session and
    rotates the recovery key. Returns (username, new_recovery_key).
    """
    if not token or len(token) > 200:
        raise HTTPException(status_code=400, detail="This password reset link is invalid or has expired.")
    now = utcnow()
    with get_db() as conn:
        row = conn.execute(
            "SELECT t.id, t.user_id, t.expires_at, t.used_at, u.username "
            "FROM password_reset_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?",
            (_sha256(token),),
        ).fetchone()
    if not row or row["used_at"] or from_iso(row["expires_at"]) <= now:
        raise HTTPException(status_code=400, detail="This password reset link is invalid or has expired.")

    ensure_password_strength(new_password, confirm_password, row["username"])
    new_recovery_key = generate_recovery_key()
    password_hash = hash_secret(new_password)
    recovery_hash = hash_secret(new_recovery_key)

    with transaction() as conn:
        claimed = conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (to_iso(now), row["id"]),
        ).rowcount
        if claimed != 1:
            raise HTTPException(status_code=400, detail="This password reset link is invalid or has expired.")
        conn.execute(
            "UPDATE users SET password_hash = ?, recovery_key_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, recovery_hash, to_iso(now), row["user_id"]),
        )
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (to_iso(now), row["user_id"]),
        )
    return row["username"], new_recovery_key
