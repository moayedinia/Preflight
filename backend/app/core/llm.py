import os
import re
import json
import requests
from urllib.parse import urlparse
from typing import Optional
from app.core.celery import CELERY_BROKER_URL

try:
    import redis
    r_client = redis.from_url(CELERY_BROKER_URL, decode_responses=True)
except Exception:
    r_client = None

def _clean_slashes(url: str) -> str:
    """Removes duplicate slashes in URL while preserving http:// and https://."""
    if not url:
        return ""
    s = str(url).strip().rstrip("/")
    return re.sub(r'(?<!:)/+', '/', s)

_MEMORY_LLM_CONFIG = {}

def get_llm_config() -> dict:
    if r_client:
        try:
            config = r_client.get("codegraph_llm_config")
            if config:
                cfg = json.loads(config)
                if cfg.get("base_url"):
                    cfg["base_url"] = _clean_slashes(cfg["base_url"])
                if not cfg.get("model"):
                    cfg["model"] = (os.getenv("LLM_MODEL") or "").strip() or "llama3"
                return cfg
        except Exception:
            pass
    if _MEMORY_LLM_CONFIG:
        return dict(_MEMORY_LLM_CONFIG)

    raw_env_base = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    return {
        "base_url": _clean_slashes(raw_env_base),
        "model": (os.getenv("LLM_MODEL") or "").strip() or "llama3",
        "api_key": os.getenv("LLM_API_KEY", "")
    }

def set_llm_config(base_url: str, model: str, api_key: str):
    clean_base = _clean_slashes(base_url)
    norm_base = normalize_llm_base_url(clean_base)
    cfg_data = {
        "base_url": norm_base,
        "model": model,
        "api_key": api_key
    }
    _MEMORY_LLM_CONFIG.update(cfg_data)
    if r_client:
        try:
            r_client.set("codegraph_llm_config", json.dumps(cfg_data))
        except Exception:
            pass


def normalize_llm_base_url(base_url: str) -> str:
    """Normalize an OpenAI-compatible API base and add /v1 when omitted, cleaning double slashes."""
    normalized = _clean_slashes(base_url)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("LLM base URL must be an absolute http(s) URL.")
    if not normalized.endswith("/v1"):
        normalized += "/v1"
    return _clean_slashes(normalized)

def call_llm(prompt: str, system_prompt: str = "") -> str:
    """Calls any OpenAI-compatible endpoint (Ollama, vLLM, LMStudio, LocalAI, OpenAI) with auto-fallbacks."""
    config = get_llm_config()
    headers = {"Content-Type": "application/json"}
    api_key = (config.get("api_key") or os.getenv("LLM_API_KEY") or "").strip()
    if api_key:
        # Strip accidental leading 'Bearer ' or enclosing quotes
        clean_key = re.sub(r'^(?:Bearer\s+|["\'])', '', api_key, flags=re.IGNORECASE)
        clean_key = re.sub(r'["\']$', '', clean_key).strip()
        headers["Authorization"] = f"Bearer {clean_key}"
        headers["api-key"] = clean_key
        headers["x-api-key"] = clean_key
        headers["X-API-KEY"] = clean_key
    
    payload = {
        "model": config.get("model", ""),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    
    base_url = config.get("base_url", "")
    norm_base = normalize_llm_base_url(base_url)
    url = _clean_slashes(f"{norm_base}/chat/completions")
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.exceptions.HTTPError as e:
        # Fallback 1: If /v1/chat/completions returns 404, try /chat/completions without /v1
        if e.response is not None and e.response.status_code == 404:
            alt_url = _clean_slashes(url.replace("/v1/chat/completions", "/chat/completions"))
            if alt_url != url:
                try:
                    alt_resp = requests.post(alt_url, headers=headers, json=payload, timeout=120)
                    if alt_resp.ok:
                        data = alt_resp.json()
                        return data["choices"][0]["message"]["content"].strip()
                except Exception:
                    pass
            
            # Fallback 2: Ollama native endpoint /api/chat
            root_url = _clean_slashes(re.sub(r'/v1.*$', '', url))
            ollama_url = f"{root_url}/api/chat"
            ollama_payload = {
                "model": config.get("model", ""),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "stream": False
            }
            try:
                ol_resp = requests.post(ollama_url, headers=headers, json=ollama_payload, timeout=120)
                if ol_resp.ok:
                    data = ol_resp.json()
                    return data.get("message", {}).get("content", "").strip()
            except Exception:
                pass
        raise
