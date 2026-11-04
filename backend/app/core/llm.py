import os
import re
import json
import logging
import requests
from urllib.parse import urlparse
from typing import Optional, Tuple, List, Dict, Any
from app.core.celery import CELERY_BROKER_URL

logger = logging.getLogger("codegraph.llm")

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

KNOWN_PROXY_PORTS = {20128, 10808, 10809, 7890, 7891}

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
                    cfg["model"] = (os.getenv("LLM_MODEL") or "").strip() or "deepseek-v4.1-flash-1017"
                if not cfg.get("api_key"):
                    cfg["api_key"] = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
                if not cfg.get("http_proxy"):
                    cfg["http_proxy"] = (os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or "").strip()
                return cfg
        except Exception:
            pass
    if _MEMORY_LLM_CONFIG:
        return dict(_MEMORY_LLM_CONFIG)

    raw_env_base = os.getenv("LLM_BASE_URL", "https://api.vultrinference.com/v1")
    clean_env_base = _clean_slashes(raw_env_base)
    env_proxy = (os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or "").strip()

    return {
        "base_url": clean_env_base,
        "model": (os.getenv("LLM_MODEL") or "").strip() or "deepseek-v4.1-flash-1017",
        "api_key": (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip(),
        "http_proxy": env_proxy
    }

def set_llm_config(base_url: str, model: str, api_key: str, http_proxy: Optional[str] = None):
    clean_base = _clean_slashes(base_url)
    proxy_val = (http_proxy or "").strip()

    parsed = urlparse(clean_base)
    if parsed.port in KNOWN_PROXY_PORTS or any(f":{p}" in clean_base for p in KNOWN_PROXY_PORTS):
        raise ValueError(
            f"Configured Base URL '{clean_base}' appears to be a local proxy port ({parsed.port}), not an inference endpoint. "
            "Please configure the Outbound HTTP Proxy field instead, and set API Base URL to https://api.vultrinference.com/v1"
        )

    norm_base = normalize_llm_base_url(clean_base)
    cfg_data = {
        "base_url": norm_base,
        "model": model,
        "api_key": api_key,
        "http_proxy": proxy_val
    }
    _MEMORY_LLM_CONFIG.update(cfg_data)
    if r_client:
        try:
            r_client.set("codegraph_llm_config", json.dumps(cfg_data))
        except Exception:
            pass

def is_running_in_docker() -> bool:
    """Detect if code is executing inside a Docker container."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv") or os.getenv("DOCKER_CONTAINER") == "true"

def resolve_docker_target_url(url: str) -> str:
    """If running in Docker, map localhost/127.0.0.1 to host.docker.internal to reach host services."""
    if not url:
        return url
    if is_running_in_docker():
        return re.sub(r'^(https?://)(?:localhost|127\.0\.0\.1)(:\d+)?', r'\1host.docker.internal\2', url)
    return url

def _sanitize_docker_proxy_env():
    """If running in Docker, ensure local proxies point to host.docker.internal rather than container loopback."""
    if is_running_in_docker():
        for var in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
            val = os.getenv(var)
            if val and ("localhost" in val or "127.0.0.1" in val):
                os.environ[var] = val.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")
        
        # Ensure NO_PROXY always bypasses host.docker.internal, loopback, and docker network aliases
        required_no_proxy = ["localhost", "127.0.0.1", "host.docker.internal", "neo4j", "redis", "api", "celery_worker", "mcp_server"]
        for no_var in ["NO_PROXY", "no_proxy"]:
            current = os.getenv(no_var, "")
            parts = [p.strip() for p in current.split(",") if p.strip()]
            for req in required_no_proxy:
                if req not in parts:
                    parts.append(req)
            os.environ[no_var] = ",".join(parts)

def execute_llm_http(method: str, url: str, **kwargs):
    """
    Executes an HTTP request for LLM calls with automatic resilience:
    1. Docker container -> host mapping (localhost/127.0.0.1 -> host.docker.internal)
    2. Local target proxy bypass (host.docker.internal must never route through outbound proxy)
    3. Docker proxy environment sanitization
    4. Proxy bypass retry if proxy connection fails/times out
    5. Meaningful diagnostic message if host refuses connection
    """
    _sanitize_docker_proxy_env()
    target_url = resolve_docker_target_url(url)
    
    # If the target is host machine or container network, NEVER route through external proxy
    is_local_target = any(h in target_url.lower() for h in [
        "host.docker.internal", "localhost", "127.0.0.1", "::1"
    ])
    if is_local_target:
        if "proxies" not in kwargs:
            kwargs["proxies"] = {"http": None, "https": None}
    else:
        if "proxies" not in kwargs:
            cfg = get_llm_config()
            configured_proxy = (cfg.get("http_proxy") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or "").strip()
            if configured_proxy:
                resolved_proxy = resolve_docker_target_url(configured_proxy)
                kwargs["proxies"] = {"http": resolved_proxy, "https": resolved_proxy}
    
    fn = requests.get if method.lower() == "get" else requests.post
    
    try:
        return fn(target_url, **kwargs)
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as first_err:
        err_msg = str(first_err)
        # Check if error is related to proxy, timeout, or connection refused (e.g. [Errno 111])
        is_refusal = any(sig in err_msg for sig in ["Connection refused", "NewConnectionError", "ProxyError", "Read timed out", "timeout", "111", "20128"])
        
        if is_refusal and kwargs.get("proxies") != {"http": None, "https": None}:
            # Fallback 1: Try without proxy in case of stale/hanging proxy
            no_proxy_kwargs = dict(kwargs)
            no_proxy_kwargs["proxies"] = {"http": None, "https": None}
            try:
                return fn(target_url, **no_proxy_kwargs)
            except Exception:
                pass

            # Fallback 2: If we changed localhost to host.docker.internal, try original URL without proxy
            if target_url != url:
                try:
                    return fn(url, **no_proxy_kwargs)
                except Exception:
                    pass

        # If it still fails and mentions localhost or 20128
        if "20128" in err_msg or "localhost" in err_msg or "127.0.0.1" in err_msg or "111" in err_msg:
            explanation = (
                f"{err_msg} - "
                "Notice: When running inside Docker, 'localhost' refers to the container itself. "
                "If your proxy or local AI endpoint is on your host machine (port 20128), "
                "make sure 'Allow LAN / Allow connection from LAN' is enabled in your proxy software (e.g. v2ray, Clash) "
                "or use https://api.openai.com/v1 if you intend to connect to cloud AI directly."
            )
            raise requests.exceptions.ConnectionError(explanation) from first_err
        raise first_err

def normalize_llm_base_url(base_url: str) -> str:
    """Normalize an OpenAI-compatible API base and add /v1 when omitted, cleaning double slashes."""
    normalized = _clean_slashes(base_url)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("LLM base URL must be an absolute http(s) URL.")
    if not normalized.endswith("/v1"):
        normalized += "/v1"
    return _clean_slashes(normalized)

def verify_llm_connection(timeout: float = 12.0, model: Optional[str] = None) -> Tuple[bool, str]:
    """
    Deeply checks if the LLM provider is reachable, API key is authenticated,
    and the specified model actually exists and accepts chat completions.
    Returns (True, 'Connected message') or (False, 'Reason message').
    """
    if os.getenv("TESTING") == "1" or os.getenv("MOCK_LLM") == "1":
        return True, "Connected (Test Mode)"

    config = get_llm_config()
    base_url = (config.get("base_url") or "").strip()
    api_key = (config.get("api_key") or "").strip()
    target_model = (model or config.get("model") or "gpt-4o-mini").strip()

    if not base_url:
        return False, "LLM Base URL is not configured. Please set it in Settings."

    # Immediate proxy port sanity check to avoid 45s hanging when user misconfigures Base URL
    parsed_base = urlparse(base_url)
    if parsed_base.port in KNOWN_PROXY_PORTS or any(f":{p}" in base_url for p in KNOWN_PROXY_PORTS):
        return False, (
            f"Port {parsed_base.port or 'proxy'} is an HTTP/SOCKS proxy port, NOT an inference endpoint. "
            "Please configure the Outbound HTTP Proxy field in Settings, and set API Base URL to https://api.vultrinference.com/v1"
        )

    is_local = any(h in base_url.lower() for h in ["localhost", "127.0.0.1", "host.docker.internal", "ollama"])
    if not is_local and not api_key:
        return False, "AI API Key is not configured. Please enter your API key in Settings (⚙️)."

    try:
        norm_base = normalize_llm_base_url(base_url)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["x-api-key"] = api_key

        # 1. Deep Probe: Test actual chat completion with the specific model requested
        probe_payload = {
            "model": target_model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False
        }
        
        chat_resp = execute_llm_http("post", f"{norm_base}/chat/completions", headers=headers, json=probe_payload, timeout=timeout)
        
        if chat_resp.ok:
            return True, f"Verified: Successfully connected and authenticated with model '{target_model}'."

        # Parse provider error message
        err_msg = ""
        try:
            err_json = chat_resp.json()
            if isinstance(err_json, dict):
                if "error" in err_json:
                    err_obj = err_json["error"]
                    if isinstance(err_obj, dict):
                        err_msg = err_obj.get("message") or str(err_obj)
                    else:
                        err_msg = str(err_obj)
                elif "detail" in err_json:
                    err_msg = str(err_json["detail"])
                elif "message" in err_json:
                    err_msg = str(err_json["message"])
        except Exception:
            err_msg = chat_resp.text[:200] if chat_resp.text else ""

        if chat_resp.status_code == 401:
            return False, f"Invalid API Key (HTTP 401 Unauthorized). {err_msg}".strip()

        if chat_resp.status_code == 404:
            if "model" in err_msg.lower() or target_model.lower() in err_msg.lower():
                return False, f"Model '{target_model}' does not exist on provider (HTTP 404): {err_msg}".strip()
            # If the /chat/completions endpoint itself returned 404, probe /models to see if the base URL is wrong
            try:
                models_resp = execute_llm_http("get", f"{norm_base}/models", headers=headers, timeout=5.0)
                if not models_resp.ok:
                    return False, f"Endpoint not found at '{norm_base}' (HTTP 404). Please check API Base URL."
            except Exception:
                pass
            return False, f"Model '{target_model}' not found on provider (HTTP 404): {err_msg or 'Resource not found'}".strip()

        if chat_resp.status_code in (400, 422):
            return False, f"Model '{target_model}' rejected (HTTP {chat_resp.status_code}): {err_msg or 'Unsupported model or invalid parameters.'}".strip()

        if chat_resp.status_code == 403:
            return False, f"Access denied to model '{target_model}' (HTTP 403): {err_msg or 'Check account permissions or model access.'}".strip()

        if chat_resp.status_code == 429:
            return False, f"Rate limited or quota exceeded (HTTP 429): {err_msg or 'Check your provider credits.'}".strip()

        return False, f"Provider error (HTTP {chat_resp.status_code}): {err_msg or chat_resp.text[:150]}".strip()

    except Exception as e:
        return False, f"Cannot connect to AI endpoint: {str(e)}"

def _extract_content_from_response(response: requests.Response) -> str:
    """Robustly extracts message content from standard JSON or SSE event stream (data: ...)."""
    text = (response.text or "").strip()
    if not text:
        raise ValueError("LLM endpoint returned an empty response body.")

    # 1. Parse SSE stream if present (data: { ... }\n\n)
    if "data:" in text:
        content_parts = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                choices = chunk.get("choices") or []
                if choices:
                    c0 = choices[0]
                    delta = c0.get("delta") or {}
                    if "content" in delta and delta["content"]:
                        content_parts.append(str(delta["content"]))
                    elif "message" in c0 and isinstance(c0["message"], dict):
                        m_content = c0["message"].get("content")
                        if m_content:
                            content_parts.append(str(m_content))
                    elif "text" in c0 and c0["text"]:
                        content_parts.append(str(c0["text"]))
            except Exception:
                continue
        if content_parts:
            return "".join(content_parts).strip()

    # 2. Parse standard JSON response
    try:
        data = response.json()
        if isinstance(data, dict):
            if "choices" in data and len(data["choices"]) > 0:
                c0 = data["choices"][0]
                if "message" in c0 and isinstance(c0["message"], dict) and c0["message"].get("content") is not None:
                    return str(c0["message"]["content"]).strip()
                if "delta" in c0 and isinstance(c0["delta"], dict) and c0["delta"].get("content") is not None:
                    return str(c0["delta"]["content"]).strip()
                if "text" in c0 and c0["text"] is not None:
                    return str(c0["text"]).strip()
            if "error" in data:
                err = data["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise ValueError(f"LLM API Error: {msg}")
    except (json.JSONDecodeError, ValueError) as err:
        if "LLM API Error" in str(err):
            raise

    # 3. Plain text fallback if not JSON or HTML
    if not text.startswith("{") and not text.startswith("<"):
        return text.strip()

    preview = text[:300].replace("\n", " ")
    raise ValueError(f"Invalid LLM response format: {preview}")

def call_llm(prompt: str, system_prompt: str = "", timeout: int = 120, max_tokens: Optional[int] = None) -> str:
    """Calls any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, Ollama, vLLM) with auto-fallbacks."""
    if os.getenv("TESTING") == "1" or os.getenv("MOCK_LLM") == "1":
        return '{"is_defect": false, "confidence": 0.95, "plain_english_explanation": "Test mode clean.", "business_consequence": "None.", "repair_prompt": "None."}'

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
        "temperature": 0.1,
        "stream": False
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens

    base_url = config.get("base_url", "") or "https://api.vultrinference.com/v1"
    norm_base = normalize_llm_base_url(base_url)
    url = _clean_slashes(f"{norm_base}/chat/completions")

    # Ensure a reasonable minimum timeout for complex LLM reasoning and remote endpoints
    call_timeout = timeout
    if os.getenv("TESTING") != "1" and call_timeout < 45:
        call_timeout = 60

    try:
        response = execute_llm_http("post", url, headers=headers, json=payload, timeout=call_timeout)
        response.raise_for_status()
        return _extract_content_from_response(response)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            alt_url = _clean_slashes(url.replace("/v1/chat/completions", "/chat/completions"))
            if alt_url != url:
                try:
                    alt_resp = execute_llm_http("post", alt_url, headers=headers, json=payload, timeout=call_timeout)
                    if alt_resp.ok:
                        return _extract_content_from_response(alt_resp)
                except Exception:
                    pass
        raise


# ==================== VULTR SERVERLESS INFERENCE INTEGRATION ====================
VULTR_INFERENCE_BASE_URL = "https://api.vultrinference.com/v1"

VULTR_DEFAULT_MODELS: List[Dict[str, Any]] = [
    {"id": "deepseek-v4.1-flash-1017", "name": "DeepSeek V4.1 Flash 1017", "type": "chat", "description": "High-speed reasoning and code analysis"},
    {"id": "glm-5.3", "name": "GLM 5.3", "type": "chat", "description": "Advanced multi-lingual general reasoning"},
    {"id": "glm-5.3-flash", "name": "GLM 5.3 Flash", "type": "chat", "description": "Low-latency lightweight inference"},
    {"id": "mimo-v2.6-pro", "name": "Mimo V2.6 Pro", "type": "chat", "description": "Enterprise-grade complex problem solving"},
    {"id": "mimo-v2.6-flash", "name": "Mimo V2.6 Flash", "type": "chat", "description": "Fast interactive generation"},
    {"id": "qwen3.8-27b", "name": "Qwen 3.8 27B", "type": "chat", "description": "Dense coding and instruction following"},
    {"id": "qwen3.8-flash-next", "name": "Qwen 3.8 Flash Next", "type": "chat", "description": "Next-gen real-time reasoning"},
    {"id": "minimax-m3.1-flash", "name": "MiniMax M3.1 Flash", "type": "chat", "description": "Ultra fast long-context completion"},
    {"id": "qwen3-embedding-4b", "name": "Qwen3 Embedding 4B", "type": "embedding", "description": "Dense vector retrieval and indexing"},
    {"id": "bge-reranker-v2-m3", "name": "BGE M3 Reranker", "type": "reranker", "description": "Cross-encoder semantic ranking"}
]


def fetch_vultr_inference_models(api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fetches the live catalog of models available on Vultr Serverless Inference (https://api.vultrinference.com/v1/models).
    Falls back gracefully to the curated Vultr Serverless models catalog if offline or unreachable.
    """
    if os.getenv("TESTING") == "1":
        return list(VULTR_DEFAULT_MODELS)

    target_url = f"{VULTR_INFERENCE_BASE_URL}/models"
    headers = {"Accept": "application/json"}
    if api_key:
        clean_key = re.sub(r'^(?:Bearer\s+|["\'])', '', api_key.strip(), flags=re.IGNORECASE)
        headers["Authorization"] = f"Bearer {clean_key}"

    try:
        session = requests.Session()
        session.trust_env = False
        resp = session.get(target_url, headers=headers, timeout=5.0)
        if resp.ok:
            payload = resp.json()
            raw_models = payload.get("data", [])
            results = []
            for m in raw_models:
                m_id = m.get("id")
                if not m_id:
                    continue
                is_aux = any(x in m_id.lower() for x in ["embed", "rerank", "speech", "asr", "tts", "image", "audio", "music", "step"])
                results.append({
                    "id": m_id,
                    "name": m.get("name") or m_id,
                    "description": m.get("description", ""),
                    "type": "auxiliary" if is_aux else "chat"
                })
            if results:
                return results
    except Exception as exc:
        logger.debug("Failed to fetch live Vultr Serverless Inference models: %s", exc)

    return list(VULTR_DEFAULT_MODELS)

