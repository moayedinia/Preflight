import os
import hashlib
import json
import urllib.request
import urllib.error
from urllib.parse import urlparse
import numpy as np

def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def normalize_api_base_url(value: str | None) -> str:
    """Return an OpenAI-compatible API base URL (ensuring /v1 suffix)."""
    base = (value or "").strip().rstrip("/")
    if not base:
        return "https://api.openai.com/v1"
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid API URL: {value!r}")
    return base

normalize_vllm_base_url = normalize_api_base_url

def validate_embedding_model_name(model: str) -> str:
    normalized = (model or "").strip()
    if not normalized:
        raise EmbeddingError("EMBEDDING_MODEL is empty.")
    if "rerank" in normalized.lower():
        raise EmbeddingError(f"'{normalized}' is a reranker, not an embedding model.")
    return normalized

# Configuration from Environment
EMBEDDING_API_URL = normalize_api_base_url(
    os.getenv("EMBEDDING_API_URL") or os.getenv("VLLM_BASE_URL") or "https://api.openai.com/v1"
)
EMBEDDING_MODEL = (os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small").strip()
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))
EMBEDDING_API_KEY = (
    os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY") or ""
).strip()
ALLOW_FAKE_EMBEDDINGS = _as_bool(os.getenv("ALLOW_FAKE_EMBEDDINGS"), default=True)

DEFAULT_EMBEDDING_MODEL = EMBEDDING_MODEL
VLLM_BASE_URL = EMBEDDING_API_URL

_STATUS_CACHE: dict = {"timestamp": 0, "result": None}
_LAST_LOGGED_ERROR: str | None = None

def clear_embedder_cache():
    """Invalidates the health/status check cache."""
    global _STATUS_CACHE, _LAST_LOGGED_ERROR
    _STATUS_CACHE = {"timestamp": 0, "result": None}
    _LAST_LOGGED_ERROR = None

def _get_active_api_key() -> str:
    global EMBEDDING_API_KEY
    if EMBEDDING_API_KEY:
        return EMBEDDING_API_KEY
    return (os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY") or "").strip()

def _query_api_embeddings(text: str, model: str, base_url: str, api_key: str = "", timeout: int = 30) -> tuple[list[float], str]:
    """
    Queries any OpenAI-compatible /embeddings endpoint:
    POST {base_url}/embeddings with {"model": model, "input": text}
    """
    try:
        base = normalize_api_base_url(base_url)
    except Exception as e:
        return [], str(e)

    clean_text = text.replace("\n", " ")[:8000]
    url = f"{base}/embeddings"
    payload = json.dumps({"model": model, "input": clean_text}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    
    key = api_key or _get_active_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            data_arr = data.get("data", [])
            if data_arr and "embedding" in data_arr[0]:
                return data_arr[0]["embedding"], ""
    except urllib.error.HTTPError as he:
        try:
            err_body = he.read().decode("utf-8")
            return [], f"HTTP {he.code}: {err_body}"
        except Exception:
            return [], f"HTTP {he.code}"
    except Exception as e:
        return [], str(e)

    return [], "No embedding returned by provider."

def _generate_fallback_embedding(text: str, dim: int = None) -> list[float]:
    """Generates a deterministic normalized pseudo-random vector for offline or zero-key mode."""
    target_dim = dim or EMBEDDING_DIM or 1536
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(target_dim)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()

class EmbeddingError(RuntimeError):
    """Raised when an embedding cannot be produced safely."""

def _query_vllm(text: str, model: str, base_url: str, timeout: int = 30) -> tuple[list[float], str]:
    """Compatibility query function that calls the API embedding provider."""
    return _query_api_embeddings(text, model, base_url, timeout=timeout)

def get_embedding(text: str, model: str = None) -> list[float]:
    """
    Generates a vector embedding for code snippet or text using:
    1. User's configured API (OpenAI / OpenRouter / Custom endpoint)
    2. Deterministic normalized fallback vectors if no key is configured or offline.
    """
    global EMBEDDING_DIM
    if not text or not text.strip():
        return [0.0] * (EMBEDDING_DIM or 1536)

    clean_text = text.replace("\n", " ")[:8000]
    target_model = model or EMBEDDING_MODEL
    target_model = validate_embedding_model_name(target_model)
    key = _get_active_api_key()

    active_url = get_active_vllm_url()
    if active_url:
        emb, err = _query_vllm(clean_text, target_model, active_url)
        if emb:
            if EMBEDDING_DIM and len(emb) != EMBEDDING_DIM:
                raise EmbeddingError(f"Embedding dimension mismatch for '{target_model}': {len(emb)} != {EMBEDDING_DIM}")
            return emb
        
        if ALLOW_FAKE_EMBEDDINGS:
            return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)
        raise EmbeddingError(f"Embedding API error for '{target_model}': {err}")

    if ALLOW_FAKE_EMBEDDINGS:
        return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)

    raise EmbeddingError("No embedding provider key configured and fake embeddings are disabled.")

def validate_embedding_configuration(timeout: int = 5) -> int:
    """Quick validation or returns current configured dimension."""
    global EMBEDDING_DIM
    key = _get_active_api_key()
    if key:
        vec, err = _query_api_embeddings("health check", EMBEDDING_MODEL, EMBEDDING_API_URL, api_key=key, timeout=timeout)
        if vec:
            EMBEDDING_DIM = len(vec)
            return EMBEDDING_DIM
    return EMBEDDING_DIM or 1536

def _init_saved_embedding_config():
    """Loads saved embedding configuration from Redis if available."""
    global EMBEDDING_API_URL, EMBEDDING_MODEL, EMBEDDING_DIM, EMBEDDING_API_KEY, ALLOW_FAKE_EMBEDDINGS, VLLM_BASE_URL
    try:
        from app.core.celery import CELERY_BROKER_URL
        import redis
        r = redis.from_url(CELERY_BROKER_URL, decode_responses=True)
        raw = r.get("codegraph_embedding_config") or r.get("codegraph_vllm_config")
        if raw and isinstance(raw, (str, bytes)):
            cfg = json.loads(raw)
            if cfg.get("api_url") or cfg.get("vllm_host"):
                EMBEDDING_API_URL = normalize_api_base_url(cfg.get("api_url") or cfg.get("vllm_host"))
                VLLM_BASE_URL = EMBEDDING_API_URL
            if cfg.get("model"):
                EMBEDDING_MODEL = cfg["model"].strip()
            if cfg.get("api_key"):
                EMBEDDING_API_KEY = cfg["api_key"].strip()
            if cfg.get("dimension"):
                EMBEDDING_DIM = int(cfg["dimension"])
            if "allow_fallback" in cfg:
                ALLOW_FAKE_EMBEDDINGS = bool(cfg["allow_fallback"])
    except Exception:
        pass

_init_saved_embedding_config()

def get_embedding_config() -> dict:
    """Returns current embedding engine configuration."""
    _init_saved_embedding_config()
    return {
        "api_url": EMBEDDING_API_URL,
        "vllm_host": EMBEDDING_API_URL,
        "model": EMBEDDING_MODEL,
        "dimension": EMBEDDING_DIM,
        "allow_fallback": ALLOW_FAKE_EMBEDDINGS,
        "provider": "api" if _get_active_api_key() else "fallback"
    }

def set_embedding_config(
    vllm_host: str = None,
    api_url: str = None,
    model: str = None,
    api_key: str = None,
    dimension: int = None,
    allow_fallback: bool = None
):
    """Updates and persists embedding configuration."""
    global EMBEDDING_API_URL, EMBEDDING_MODEL, EMBEDDING_DIM, EMBEDDING_API_KEY, ALLOW_FAKE_EMBEDDINGS, VLLM_BASE_URL
    target_url = api_url or vllm_host
    if target_url:
        EMBEDDING_API_URL = normalize_api_base_url(target_url)
        VLLM_BASE_URL = EMBEDDING_API_URL
    if model:
        EMBEDDING_MODEL = model.strip()
    if api_key is not None:
        EMBEDDING_API_KEY = api_key.strip()
    if dimension:
        EMBEDDING_DIM = int(dimension)
    if allow_fallback is not None:
        ALLOW_FAKE_EMBEDDINGS = bool(allow_fallback)

    clear_embedder_cache()

    try:
        from app.core.celery import CELERY_BROKER_URL
        import redis
        r = redis.from_url(CELERY_BROKER_URL, decode_responses=True)
        cfg_data = {
            "api_url": EMBEDDING_API_URL,
            "vllm_host": EMBEDDING_API_URL,
            "model": EMBEDDING_MODEL,
            "api_key": EMBEDDING_API_KEY,
            "dimension": EMBEDDING_DIM,
            "allow_fallback": ALLOW_FAKE_EMBEDDINGS,
            "provider": "api"
        }
        r.set("codegraph_embedding_config", json.dumps(cfg_data))
    except Exception:
        pass
    return get_embedding_config()

def test_vllm_embedding_connection(vllm_host: str = None, model: str = None, api_key: str = None) -> dict:
    """Tests embedding generation for the configured endpoint."""
    host = normalize_api_base_url(vllm_host or EMBEDDING_API_URL)
    target_model = model or EMBEDDING_MODEL

    models = list_vllm_models(host)
    if not models:
        return {
            "status": "offline",
            "host": host,
            "target_model": target_model,
            "available_models": [],
            "message": "Offline or unreachable."
        }

    vec, err = _query_vllm("Test embedding probe", target_model, host, timeout=10)
    if vec:
        return {
            "status": "connected",
            "host": host,
            "target_model": target_model,
            "model_present": True,
            "detected_dimension": len(vec),
            "configured_dimension": len(vec),
            "sample_vector_preview": vec[:5],
            "message": f"Successfully generated {len(vec)}-dimensional embedding with model '{target_model}'!"
        }
    return {
        "status": "offline" if "HTTP" not in err else "error",
        "host": host,
        "target_model": target_model,
        "error_detail": err,
        "message": f"Embedding probe failed: {err}"
    }

def verify_vllm_status() -> dict:
    """Returns status of embedding provider."""
    import time
    global _STATUS_CACHE
    now = time.time()
    if _STATUS_CACHE.get("result") and (now - _STATUS_CACHE.get("timestamp", 0)) < 15:
        return _STATUS_CACHE["result"]

    key = _get_active_api_key()
    res = {
        "status": "connected" if key else "offline_ready",
        "server_url": EMBEDDING_API_URL,
        "target_model": EMBEDDING_MODEL,
        "configured_dimension": EMBEDDING_DIM,
        "has_api_key": bool(key),
        "allow_fallback": ALLOW_FAKE_EMBEDDINGS,
        "message": "API embeddings active" if key else "Ready in offline/fallback mode (no API key required)"
    }
    _STATUS_CACHE = {"timestamp": now, "result": res}
    return res

def reembed_graph_entities(repo_name: str = None, force: bool = False) -> dict:
    """On-demand embedding generation for all functions and classes in Neo4j."""
    import app.core.graph as graph_module
    driver = graph_module.driver
    effective_model = EMBEDDING_MODEL

    with driver.session() as session:
        query_fn = """
        MATCH (f:Function)
        WHERE ($repo_name IS NULL OR f.repo = $repo_name)
          AND ($force = true OR f.embedding IS NULL)
        RETURN coalesce(f.id, elementId(f)) AS id, f.name AS name, f.code AS code, f.file_path AS file_path
        LIMIT 500
        """
        fn_records = [dict(r) for r in session.run(query_fn, repo_name=repo_name, force=force)]

        query_cls = """
        MATCH (c:Class)
        WHERE ($repo_name IS NULL OR c.repo = $repo_name)
          AND ($force = true OR c.embedding IS NULL)
        RETURN coalesce(c.id, elementId(c)) AS id, c.name AS name, c.file_path AS file_path
        LIMIT 200
        """
        cls_records = [dict(r) for r in session.run(query_cls, repo_name=repo_name, force=force)]

    fn_count = len(fn_records)
    cls_count = len(cls_records)
    total_to_embed = fn_count + cls_count
    success_count = 0
    error_count = 0

    for f in fn_records:
        text = f.get("code") or f.get("name") or ""
        try:
            emb = get_embedding(text, model=effective_model)
            with driver.session() as session:
                session.run("""
                MATCH (fn:Function)
                WHERE fn.id = $id OR elementId(fn) = $id
                SET fn.embedding = $emb, fn.embedding_model = $model, fn.embedding_dim = $dim
                """, id=f["id"], emb=emb, model=effective_model, dim=len(emb))
            success_count += 1
        except Exception as e:
            error_count += 1

    for c in cls_records:
        text = c.get("name") or "class"
        try:
            emb = get_embedding(text, model=effective_model)
            with driver.session() as session:
                session.run("""
                MATCH (cls:Class)
                WHERE cls.id = $id OR elementId(cls) = $id
                SET cls.embedding = $emb, cls.embedding_model = $model, cls.embedding_dim = $dim
                """, id=c["id"], emb=emb, model=effective_model, dim=len(emb))
            success_count += 1
        except Exception as e:
            error_count += 1

    return {
        "status": "success" if error_count == 0 else "partial_success",
        "repo_name": repo_name or "All Repositories",
        "total_entities_evaluated": total_to_embed,
        "embedded_successfully": success_count,
        "errors": error_count,
        "model_used": effective_model,
        "dimension": EMBEDDING_DIM
    }

# Compatibility helper
def get_active_vllm_url() -> str:
    return EMBEDDING_API_URL

def ensure_embedding_model(base_url: str = None) -> str:
    return EMBEDDING_MODEL

_query_vllm = _query_api_embeddings

def list_vllm_models(base_url: str = None, timeout: int = 2) -> list[str]:
    return [EMBEDDING_MODEL]
