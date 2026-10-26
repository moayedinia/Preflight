import os
import re
import json
import requests
from urllib.parse import urlparse
from typing import Optional, Tuple
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
                    cfg["model"] = (os.getenv("LLM_MODEL") or "").strip() or "gpt-4o-mini"
                if not cfg.get("api_key"):
                    cfg["api_key"] = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
                return cfg
        except Exception:
            pass
    if _MEMORY_LLM_CONFIG:
        return dict(_MEMORY_LLM_CONFIG)

    raw_env_base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    return {
        "base_url": _clean_slashes(raw_env_base),
        "model": (os.getenv("LLM_MODEL") or "").strip() or "gpt-4o-mini",
        "api_key": (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
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

def verify_llm_connection(timeout: float = 3.0) -> Tuple[bool, str]:
    """
    Checks if the LLM provider has an API key and is reachable.
    Returns (True, 'Connected') or (False, 'Reason message').
    """
    if os.getenv("TESTING") == "1" or os.getenv("MOCK_LLM") == "1":
        return True, "Connected (Test Mode)"

    config = get_llm_config()
    base_url = (config.get("base_url") or "").strip()
    api_key = (config.get("api_key") or "").strip()

    if not base_url:
        return False, "آدرس سرور هوش مصنوعی (LLM Base URL) تنظیم نشده است."

    is_local = any(h in base_url.lower() for h in ["localhost", "127.0.0.1", "host.docker.internal", "ollama"])
    if not is_local and not api_key:
        return False, "کلید API هوش مصنوعی وارد نشده است. لطفاً در بخش تنظیمات (Settings) کلید API خود را وارد کنید."

    try:
        norm_base = normalize_llm_base_url(base_url)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["x-api-key"] = api_key

        resp = requests.get(f"{norm_base}/models", headers=headers, timeout=timeout)
        if resp.status_code in (200, 201):
            return True, "Connected"
        if resp.status_code == 401:
            return False, "کلید API نامعتبر است (HTTP 401 Unauthorized). لطفاً کلید صحیح را در تنظیمات وارد کنید."
        if resp.status_code == 404:
            probe_payload = {
                "model": config.get("model", "gpt-4o-mini"),
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1
            }
            chat_resp = requests.post(f"{norm_base}/chat/completions", headers=headers, json=probe_payload, timeout=timeout)
            if chat_resp.ok:
                return True, "Connected"
            if chat_resp.status_code == 401:
                return False, "کلید API نامعتبر است (HTTP 401 Unauthorized)."
            return False, f"خطای ارتباط با مدل هوش مصنوعی (HTTP {chat_resp.status_code})."
        return False, f"خطای ارتباط با سرور هوش مصنوعی (HTTP {resp.status_code})."
    except Exception as e:
        return False, f"عدم امکان برقراری ارتباط با هوش مصنوعی: {str(e)}"

def call_llm(prompt: str, system_prompt: str = "", timeout: int = 120) -> str:
    """Calls any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, Ollama, vLLM) with auto-fallbacks."""
    config = get_llm_config()
    headers = {"Content-Type": "application/json"}
    api_key = (config.get("api_key") or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    if api_key:
        clean_key = re.sub(r'^(?:Bearer\s+|["\'])', '', api_key, flags=re.IGNORECASE)
        clean_key = re.sub(r'["\']$', '', clean_key).strip()
        headers["Authorization"] = f"Bearer {clean_key}"
        headers["api-key"] = clean_key
        headers["x-api-key"] = clean_key
        headers["X-API-KEY"] = clean_key

    payload = {
        "model": config.get("model", "") or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }

    base_url = config.get("base_url", "") or "https://api.openai.com/v1"
    norm_base = normalize_llm_base_url(base_url)
    url = _clean_slashes(f"{norm_base}/chat/completions")

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.exceptions.HTTPError as e:
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
        raise
