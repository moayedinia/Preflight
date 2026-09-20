import os
import re
from pathlib import Path
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional

# Environment configurations
API_KEY = os.getenv("API_KEY")
ALLOW_UNAUTHENTICATED = os.getenv("ALLOW_UNAUTHENTICATED", "false").lower() in ("true", "1", "yes")

# Base directory for allowed repository scanning
ALLOWED_REPO_DIR = Path(os.getenv("ALLOWED_REPO_DIR", "/app/repos")).resolve()

# Security schemes
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)

def verify_api_key(
    header_key: Optional[str] = Security(api_key_header),
    token: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme)
) -> bool:
    """
    Verifies that the incoming request contains a valid API key.
    Checks either 'X-API-Key' header or 'Authorization: Bearer <key>'.
    """
    # If unauthenticated access is explicitly enabled, bypass checks
    if ALLOW_UNAUTHENTICATED:
        return True

    if not API_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server security configuration error: API_KEY is not configured in environment."
        )

    provided_key = header_key or (token.credentials if token else None)

    if not provided_key or provided_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key. Provide a valid 'X-API-Key' header.",
            headers={"WWW-Authenticate": "ApiKey"}
        )

    return True

def sanitize_name(name: Optional[str]) -> Optional[str]:
    """
    Sanitizes repository or entity name to prevent injection / path traversal.
    Supports Unicode alphanumeric names (including Persian / Arabic), spaces, dashes, dots, and underscores.
    Gracefully normalizes empty strings and "All Repositories" wildcards to None.
    """
    if not name:
        return None
    name = name.strip()
    if name in ("", "All Repositories", "ALL", "null", "undefined", "All", "none", "None"):
        return None
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="Invalid name length.")
    if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(status_code=400, detail="Name contains invalid path characters.")
    # Allow alphanumeric (including Unicode/Persian letters and numbers), spaces, underscore, dash, dot
    if not re.match(r"^[\w\-\.\s]+$", name, re.UNICODE):
        raise HTTPException(status_code=400, detail="Name contains invalid characters.")
    return name

def validate_safe_path(input_path: str) -> Path:
    """
    Prevents Path Traversal and Directory Escaping.
    Ensures the target path exists and resides strictly within the allowed directory sandbox.
    """
    if not input_path:
        raise HTTPException(status_code=400, detail="Path must not be empty.")

    # Disallow explicit traversal sequences
    if ".." in input_path:
        raise HTTPException(status_code=400, detail="Directory traversal sequences ('..') are forbidden.")

    # Resolve target path
    target = Path(input_path).resolve()

    # Ensure target is within the sandbox if ALLOWED_REPO_DIR is enforced
    if ALLOWED_REPO_DIR.exists():
        try:
            if not target.is_relative_to(ALLOWED_REPO_DIR) and target != ALLOWED_REPO_DIR:
                raise HTTPException(
                    status_code=403,
                    detail=f"Access forbidden: Path must reside inside the sandbox directory: {ALLOWED_REPO_DIR}"
                )
        except AttributeError:
            # Fallback for Python < 3.9
            if not str(target).startswith(str(ALLOWED_REPO_DIR)):
                raise HTTPException(status_code=403, detail="Access forbidden: Path is outside allowed directory.")

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Directory or file does not exist: {input_path}")

    return target
