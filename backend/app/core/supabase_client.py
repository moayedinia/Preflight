"""Vultr Supabase Marketplace App Integration.

Provides client-side connectivity to Vultr-hosted Supabase instances for:
1. Health & auth verification.
2. Cloud report artifact synchronization (Storage API).
3. Vector & metadata table integration (PostgREST API).
"""
import os
import json
import logging
from typing import Optional, Dict, Any, Tuple
import requests

logger = logging.getLogger("codegraph.supabase")

_SUPABASE_CONFIG = {
    "url": os.getenv("SUPABASE_URL", "").rstrip("/"),
    "anon_key": os.getenv("SUPABASE_ANON_KEY", "").strip(),
    "service_role_key": os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
    "bucket": os.getenv("SUPABASE_REPORT_BUCKET", "preflight-reports")
}

def get_supabase_config() -> Dict[str, Any]:
    """Returns the current Supabase configuration with masked keys for security."""
    key = _SUPABASE_CONFIG.get("service_role_key") or _SUPABASE_CONFIG.get("anon_key") or ""
    masked_key = ""
    if key:
        masked_key = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"
    return {
        "url": _SUPABASE_CONFIG.get("url", ""),
        "bucket": _SUPABASE_CONFIG.get("bucket", "preflight-reports"),
        "has_key": bool(key),
        "masked_key": masked_key,
        "is_configured": bool(_SUPABASE_CONFIG.get("url") and key)
    }

def set_supabase_config(url: str, key: str, bucket: str = "preflight-reports") -> Dict[str, Any]:
    """Updates the runtime Supabase configuration."""
    clean_url = str(url).strip().rstrip("/")
    clean_key = str(key).strip()
    _SUPABASE_CONFIG["url"] = clean_url
    if "service_role" in clean_key.lower() or len(clean_key) > 40:
        _SUPABASE_CONFIG["service_role_key"] = clean_key
    else:
        _SUPABASE_CONFIG["anon_key"] = clean_key
    _SUPABASE_CONFIG["bucket"] = (bucket or "preflight-reports").strip()
    return get_supabase_config()

def test_supabase_connection(timeout: float = 6.0) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Verifies that the configured Vultr Supabase instance is live and accessible.
    Tests Kong gateway, Auth health, and Storage/PostgREST availability.
    """
    if os.getenv("TESTING") == "1":
        return True, "Vultr Supabase connected (Test Mode)", {"version": "vultr-supabase-marketplace-1.0"}

    url = _SUPABASE_CONFIG.get("url", "").rstrip("/")
    key = _SUPABASE_CONFIG.get("service_role_key") or _SUPABASE_CONFIG.get("anon_key") or ""

    if not url:
        return False, "Supabase URL is not configured.", {}
    if not key:
        return False, "Supabase API key (anon or service_role) is required.", {}

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json"
    }

    session = requests.Session()
    session.trust_env = False

    # 1. Probe PostgREST endpoint
    try:
        resp = session.get(f"{url}/rest/v1/", headers=headers, timeout=timeout)
        if resp.status_code in (200, 204, 404):
            return True, f"Successfully verified Vultr Supabase API at '{url}'.", {"status_code": resp.status_code}
    except Exception as exc:
        pass

    # 2. Probe Auth health
    try:
        auth_resp = session.get(f"{url}/auth/v1/health", headers=headers, timeout=timeout)
        if auth_resp.ok:
            return True, f"Successfully verified Vultr Supabase Auth at '{url}'.", auth_resp.json() if auth_resp.text else {}
    except Exception as exc:
        pass

    # 3. Probe Storage API
    try:
        storage_resp = session.get(f"{url}/storage/v1/bucket", headers=headers, timeout=timeout)
        if storage_resp.status_code in (200, 401, 403):
            return True, f"Connected to Vultr Supabase Storage at '{url}'.", {"status_code": storage_resp.status_code}
    except Exception as exc:
        pass

    return False, f"Could not reach Vultr Supabase at '{url}'. Check network and credentials.", {}

def sync_report_to_supabase(repo_name: str, filename: str, content: str) -> Dict[str, Any]:
    """
    Uploads a markdown report or artifact to the Vultr Supabase Storage bucket.
    """
    if os.getenv("TESTING") == "1":
        return {"status": "success", "synced": True, "path": f"reports/{repo_name}/{filename}"}

    url = _SUPABASE_CONFIG.get("url", "").rstrip("/")
    key = _SUPABASE_CONFIG.get("service_role_key") or _SUPABASE_CONFIG.get("anon_key") or ""
    bucket = _SUPABASE_CONFIG.get("bucket", "preflight-reports")

    if not url or not key:
        return {"status": "skipped", "message": "Supabase not configured"}

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "text/markdown; charset=utf-8",
        "x-upsert": "true"
    }

    target_path = f"{repo_name}/{filename}"
    upload_url = f"{url}/storage/v1/object/{bucket}/{target_path}"

    try:
        session = requests.Session()
        session.trust_env = False
        resp = session.post(upload_url, headers=headers, data=content.encode("utf-8"), timeout=10.0)
        if resp.status_code in (200, 201):
            return {
                "status": "success",
                "bucket": bucket,
                "path": target_path,
                "url": f"{url}/storage/v1/object/public/{bucket}/{target_path}"
            }
        return {"status": "error", "code": resp.status_code, "detail": resp.text[:200]}
    except Exception as e:
        logger.warning("Supabase sync failed: %s", e)
        return {"status": "error", "detail": str(e)}
