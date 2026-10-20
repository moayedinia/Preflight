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


def normalize_vllm_base_url(value: str | None) -> str:
    """Return the vLLM OpenAI-compatible API base URL (ensuring /v1 suffix)."""
    base = (value or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid VLLM_BASE_URL: {value!r}")
    return base


VLLM_BASE_URL = normalize_vllm_base_url(
    os.getenv("VLLM_BASE_URL", "http://vllm:8007/v1")
)

DEFAULT_EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL", "jinaai/jina-embeddings-v5-text-small-retrieval"
)
PREFERRED_EMBEDDING_MODELS = [
    "jinaai/jina-embeddings-v5-text-small-retrieval",
    "jinaai/jina-embeddings-v5-text-small",
    "jinaai/jina-embeddings-v3",
]
EMBEDDING_MODEL = DEFAULT_EMBEDDING_MODEL
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "512"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ALLOW_FAKE_EMBEDDINGS = _as_bool(os.getenv("ALLOW_FAKE_EMBEDDINGS"), default=False)

_STATUS_CACHE: dict = {"timestamp": 0, "result": None}
_LAST_LOGGED_ERROR: str | None = None
_VERIFIED_WORKING_VLLM_URL: str | None = None
_ENSURED_EMBEDDING_MODEL: str | None = None

def _get_linux_default_gateway() -> str | None:
    """Reads default gateway IP from /proc/net/route if running in Linux/Docker container."""
    try:
        with open("/proc/net/route", "r") as f:
            for line in f:
                fields = line.strip().split()
                if len(fields) >= 3 and (fields[1] == '00000000' or int(fields[1], 16) == 0):
                    import socket, struct
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except Exception:
        pass
    return None

def clear_embedder_cache():
    """Invalidates the health/status check and verified endpoint cache."""
    global _STATUS_CACHE, _LAST_LOGGED_ERROR, _VERIFIED_WORKING_VLLM_URL, _ENSURED_EMBEDDING_MODEL
    _STATUS_CACHE = {"timestamp": 0, "result": None}
    _LAST_LOGGED_ERROR = None
    _VERIFIED_WORKING_VLLM_URL = None
    _ENSURED_EMBEDDING_MODEL = None

def get_active_vllm_url() -> str:
    """
    Returns the verified reachable vLLM base URL (/v1) for code embeddings.
    Dedicated exclusively to embedding operations.
    """
    global _VERIFIED_WORKING_VLLM_URL
    if _VERIFIED_WORKING_VLLM_URL:
        return _VERIFIED_WORKING_VLLM_URL

    configured_url = (VLLM_BASE_URL or "").strip()
    candidates = []
    if configured_url:
        candidates.append(configured_url)
    
    candidates.append("http://vllm:8007/v1")
    candidates.append("http://graphrag_vllm:8007/v1")
    candidates.append("http://host.docker.internal:8007/v1")
    gw_ip = _get_linux_default_gateway()
    if gw_ip:
        candidates.append(f"http://{gw_ip}:8007/v1")
    candidates.append("http://172.17.80.1:8007/v1")
    candidates.append("http://127.0.0.1:8007/v1")

    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            req = urllib.request.Request(f"{cand}/models", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=0.6) as resp:
                if resp.status == 200:
                    _VERIFIED_WORKING_VLLM_URL = cand
                    return _VERIFIED_WORKING_VLLM_URL
        except Exception:
            continue

    return configured_url or "http://vllm:8007/v1"


def ensure_embedding_model(base_url: str) -> str:
    """
    Finds the active embedding model on vLLM.
    vLLM serves models loaded at startup.
    """
    global _ENSURED_EMBEDDING_MODEL
    if _ENSURED_EMBEDDING_MODEL:
        return _ENSURED_EMBEDDING_MODEL

    available = list_vllm_models(base_url, timeout=2)
    if available:
        for pref in PREFERRED_EMBEDDING_MODELS:
            if pref in available:
                _ENSURED_EMBEDDING_MODEL = pref
                return pref
        _ENSURED_EMBEDDING_MODEL = available[0]
        return available[0]

    _ENSURED_EMBEDDING_MODEL = DEFAULT_EMBEDDING_MODEL
    return DEFAULT_EMBEDDING_MODEL


class EmbeddingError(RuntimeError):
    """Raised when a real embedding cannot be produced safely."""


def validate_embedding_model_name(model: str) -> str:
    """Reject rerankers early: Embedding endpoint requires an embedding model."""
    normalized = (model or "").strip()
    if not normalized:
        raise EmbeddingError("EMBEDDING_MODEL is empty.")
    if "rerank" in normalized.lower():
        raise EmbeddingError(
            f"'{normalized}' is a reranker, not an embedding model. Use "
            f"'{DEFAULT_EMBEDDING_MODEL}' for vLLM embeddings."
        )
    return normalized


client = None
if VLLM_BASE_URL:
    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=VLLM_BASE_URL,
            api_key=os.getenv("VLLM_API_KEY", "EMPTY")
        )
    except Exception as e:
        print(f"Notice: Could not initialize vLLM OpenAI-compatible client: {e}")
elif OPENAI_API_KEY:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"Notice: Could not initialize OpenAI client: {e}")


def list_vllm_models(base_url: str = None, timeout: int = 2) -> list[str]:
    """Fetches list of available models from vLLM GET /v1/models."""
    global _LAST_LOGGED_ERROR
    target_url = (base_url or get_active_vllm_url() or "").rstrip('/')
    if not target_url or not target_url.startswith("http"):
        return []
    try:
        req = urllib.request.Request(f"{target_url}/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _LAST_LOGGED_ERROR = None
            models = []
            for item in data.get("data", []):
                mid = item.get("id")
                if mid:
                    models.append(mid)
            return models
    except Exception as e:
        err_msg = str(e)
        if _LAST_LOGGED_ERROR != err_msg:
            print(f"Notice: vLLM not reachable at {target_url} ({err_msg}).")
            _LAST_LOGGED_ERROR = err_msg
        return []


def _query_vllm(text: str, model: str, base_url: str, timeout: int = 60) -> tuple[list[float], str]:
    """
    Queries remote/internal vLLM OpenAI-compatible embeddings endpoint:
    POST {base_url}/embeddings with {"model": model, "input": text}
    Returns: (embedding_vector, last_error_message)
    """
    try:
        base = normalize_vllm_base_url(base_url)
    except Exception as e:
        return [], str(e)

    try:
        model = validate_embedding_model_name(model)
    except EmbeddingError as exc:
        return [], str(exc)

    clean_text = text.replace("\n", " ")[:8000]
    errors = []

    try:
        url = f"{base}/embeddings"
        payload = json.dumps({"model": model, "input": clean_text}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer EMPTY"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            data_arr = data.get("data", [])
            if data_arr and "embedding" in data_arr[0]:
                return data_arr[0]["embedding"], ""
    except urllib.error.HTTPError as he:
        try:
            err_body = he.read().decode('utf-8')
            errors.append(f"/embeddings: HTTP {he.code} - {err_body}")
        except Exception:
            errors.append(f"/embeddings: HTTP {he.code}")
    except Exception as e:
        errors.append(f"/embeddings: {e}")

    return [], "; ".join(errors)


def _generate_fallback_embedding(text: str, dim: int = None) -> list[float]:
    """Generates a deterministic pseudo-random normalized vector for offline/mock mode."""
    target_dim = dim or EMBEDDING_DIM
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(target_dim)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


def get_embedding(text: str, model: str = None) -> list[float]:
    """
    Generates a vector embedding for code snippet or text using:
    1. vLLM (jinaai/jina-embeddings-v5-text-small-retrieval) with pooling runner.
    2. OpenAI (text-embedding-3-small) if OPENAI_API_KEY is set.
    3. Optional deterministic test vectors only when ALLOW_FAKE_EMBEDDINGS=true.
    """
    if not text or not text.strip():
        return [0.0] * EMBEDDING_DIM

    clean_text = text.replace("\n", " ")[:8000]
    active_url = get_active_vllm_url()
    target_model = model or (ensure_embedding_model(active_url) if active_url else DEFAULT_EMBEDDING_MODEL)
    target_model = validate_embedding_model_name(target_model)

    # 1. vLLM
    if active_url:
        emb, err = _query_vllm(clean_text, target_model, active_url)
        if emb:
            if len(emb) != EMBEDDING_DIM:
                raise EmbeddingError(
                    f"Embedding dimension mismatch for '{target_model}': "
                    f"vLLM returned {len(emb)}, but EMBEDDING_DIM={EMBEDDING_DIM}. "
                    "Update EMBEDDING_DIM and recreate/re-index the vector data."
                )
            return emb
        if ALLOW_FAKE_EMBEDDINGS:
            print(f"[Embedder] vLLM query failed: {err}. ALLOW_FAKE_EMBEDDINGS is enabled; using a deterministic test vector.")
            return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)
        raise EmbeddingError(
            f"vLLM embedding failed for '{target_model}' at '{active_url}': {err}"
        )

    # 2. OpenAI
    if client and OPENAI_API_KEY:
        try:
            response = client.embeddings.create(input=[clean_text], model=target_model)
            return response.data[0].embedding
        except Exception as e:
            if ALLOW_FAKE_EMBEDDINGS:
                print(f"[Embedder] OpenAI error: {e}. Using a deterministic test vector.")
                return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)
            raise EmbeddingError(f"OpenAI embedding failed: {e}") from e

    if ALLOW_FAKE_EMBEDDINGS:
        return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)
    raise EmbeddingError("No embedding provider is configured and fake embeddings are disabled.")


def validate_embedding_configuration(timeout: int = 5) -> int:
    """Verify provider reachability, model availability, and vector dimension with quick timeout."""
    active_url = get_active_vllm_url()
    if not active_url:
        if ALLOW_FAKE_EMBEDDINGS:
            return EMBEDDING_DIM
        raise EmbeddingError("VLLM_BASE_URL is not configured.")

    effective_model = ensure_embedding_model(active_url)
    vector, err = _query_vllm("code graph embedding health check", effective_model, active_url, timeout=timeout)
    if not vector:
        raise EmbeddingError(
            f"Cannot initialize embeddings from '{active_url}' using "
            f"'{effective_model}': {err}"
        )
    if len(vector) != EMBEDDING_DIM:
        raise EmbeddingError(
            f"Embedding dimension mismatch for '{effective_model}': "
            f"detected {len(vector)}, configured {EMBEDDING_DIM}."
        )
    return EMBEDDING_DIM


def verify_vllm_status() -> dict:
    """
    Diagnostic tool to verify connectivity to the vLLM server,
    check model availability, and validate embedding output & dimension.
    Caches result for 15 seconds to prevent hammering sockets.
    """
    import time
    global _STATUS_CACHE
    now = time.time()
    if _STATUS_CACHE.get("result") and (now - _STATUS_CACHE.get("timestamp", 0)) < 15:
        return _STATUS_CACHE["result"]

    active_url = get_active_vllm_url()
    if not active_url:
        res = {
            "status": "not_configured",
            "message": "vLLM base URL is not configured.",
            "server_url": None,
            "target_model": DEFAULT_EMBEDDING_MODEL,
            "available_models": []
        }
        _STATUS_CACHE = {"timestamp": now, "result": res}
        return res

    effective_model = ensure_embedding_model(active_url)
    available_models = list_vllm_models(active_url, timeout=2)
    test_vec, err = _query_vllm("test code embedding", effective_model, active_url, timeout=4)

    if test_vec:
        dimension_matches = len(test_vec) == EMBEDDING_DIM
        res = {
            "status": "connected" if dimension_matches else "dimension_mismatch",
            "server_url": active_url,
            "target_model": effective_model,
            "model_present": (effective_model in available_models) if available_models else True,
            "detected_dimension": len(test_vec),
            "configured_dimension": EMBEDDING_DIM,
            "dimension_matches": dimension_matches,
            "available_models": available_models,
            "sample_vector_preview": test_vec[:5]
        }
    else:
        res = {
            "status": "offline",
            "server_url": active_url,
            "target_model": effective_model,
            "configured_dimension": EMBEDDING_DIM,
            "available_models": available_models,
            "error_detail": err or "vLLM server is offline or unreachable."
        }

    _STATUS_CACHE = {"timestamp": now, "result": res}
    return res


# --- DEDICATED EMBEDDING ENGINE CONFIGURATION (vLLM / GPU Accelerated) ---
def _init_saved_embedding_config():
    """Loads saved embedding host and model configuration from Redis if available."""
    global VLLM_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIM, ALLOW_FAKE_EMBEDDINGS
    try:
        from app.core.celery import CELERY_BROKER_URL
        import redis
        r = redis.from_url(CELERY_BROKER_URL, decode_responses=True)
        raw = r.get("codegraph_vllm_config") or r.get("codegraph_embedding_config")
        if raw and isinstance(raw, (str, bytes)):
            cfg = json.loads(raw)
            host_val = cfg.get("vllm_host")
            if host_val:
                VLLM_BASE_URL = normalize_vllm_base_url(host_val)
            if cfg.get("model"):
                EMBEDDING_MODEL = cfg["model"].strip()
            if cfg.get("dimension"):
                EMBEDDING_DIM = int(cfg["dimension"])
            if "allow_fallback" in cfg:
                ALLOW_FAKE_EMBEDDINGS = bool(cfg["allow_fallback"])
    except Exception:
        pass

_init_saved_embedding_config()

def get_embedding_config() -> dict:
    """Returns the dedicated vLLM embedding engine configuration."""
    _init_saved_embedding_config()
    return {
        "vllm_host": VLLM_BASE_URL,
        "model": EMBEDDING_MODEL,
        "dimension": EMBEDDING_DIM,
        "allow_fallback": ALLOW_FAKE_EMBEDDINGS,
        "provider": "vllm"
    }

def set_embedding_config(
    vllm_host: str = None,
    model: str = None,
    dimension: int = None,
    allow_fallback: bool = None
):
    """Updates and persists the dedicated vLLM embedding engine configuration."""
    global VLLM_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIM, ALLOW_FAKE_EMBEDDINGS
    raw_host = (vllm_host or "").strip()
    if raw_host and "://" not in raw_host:
        raw_host = f"http://{raw_host}"
    clean_host = normalize_vllm_base_url(raw_host) if raw_host else VLLM_BASE_URL
    VLLM_BASE_URL = clean_host
    if model:
        EMBEDDING_MODEL = model.strip()
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
            "vllm_host": VLLM_BASE_URL,
            "model": EMBEDDING_MODEL,
            "dimension": EMBEDDING_DIM,
            "allow_fallback": ALLOW_FAKE_EMBEDDINGS,
            "provider": "vllm"
        }
        r.set("codegraph_vllm_config", json.dumps(cfg_data))
    except Exception:
        pass
    return get_embedding_config()

def test_vllm_embedding_connection(vllm_host: str = None, model: str = None) -> dict:
    """Directly tests reachability, models list, and embedding generation for a specified vLLM host IP."""
    raw_host = (vllm_host or VLLM_BASE_URL or get_active_vllm_url() or "").strip()
    if raw_host and "://" not in raw_host:
        raw_host = f"http://{raw_host}"

    try:
        host = normalize_vllm_base_url(raw_host)
    except Exception as e:
        return {
            "status": "error",
            "host": raw_host,
            "target_model": model or EMBEDDING_MODEL,
            "available_models": [],
            "message": f"Invalid vLLM URL '{raw_host}': {str(e)}. Example: http://vllm:8007/v1 or http://host.docker.internal:8007/v1"
        }

    target_model = model or EMBEDDING_MODEL
    
    models = list_vllm_models(host, timeout=3.0)
    if not models:
        suggested_msg = f"Cannot connect to vLLM at {host}. Please verify vLLM is running and accessible."
        try:
            parsed = urlparse(host)
            hostname = parsed.hostname or ""
            if hostname in ("localhost", "127.0.0.1"):
                suggested_msg = (
                    f"Cannot reach vLLM at {host}. Inside Docker, 'localhost' refers to the container itself, "
                    "not your host machine! If vLLM is running in Docker Compose, use 'http://vllm:8007/v1'. "
                    "If running directly on your host machine, use 'http://host.docker.internal:8007/v1'."
                )
            elif "host.docker.internal" in hostname or "host-gateway" in hostname:
                suggested_msg = (
                    f"Connection refused at {host}. vLLM was reached on the host, "
                    "but no service is listening on port 8007 or host firewall blocked it. "
                    "Make sure vLLM container is running with -p 8007:8007."
                )
        except Exception:
            pass

        return {
            "status": "offline",
            "host": host,
            "target_model": target_model,
            "available_models": [],
            "message": suggested_msg
        }
        
    model_present = target_model in models or any(target_model.split("/")[-1] in m for m in models)
    
    vec, err = _query_vllm("CodeGraph Brain test embedding probe", target_model, host, timeout=10)
    if vec:
        return {
            "status": "connected",
            "host": host,
            "target_model": target_model,
            "model_present": True,
            "detected_dimension": len(vec),
            "configured_dimension": EMBEDDING_DIM,
            "available_models": models,
            "sample_vector_preview": vec[:5],
            "message": f"Successfully connected to vLLM at {host}! Model '{target_model}' generated a {len(vec)}-dim vector."
        }
    else:
        return {
            "status": "model_missing" if not model_present else "error",
            "host": host,
            "target_model": target_model,
            "model_present": model_present,
            "available_models": models,
            "error_detail": err or "Failed to generate embedding vector from vLLM.",
            "message": f"Connected to vLLM, but model '{target_model}' returned: {err}"
        }

def reembed_graph_entities(repo_name: str = None, force: bool = False) -> dict:
    """
    On-demand embedding generation for all functions and classes in Neo4j.
    Can be executed at any stage (در هر مرحله ای).
    """
    from app.core.graph import driver
    active_url = get_active_vllm_url()
    effective_model = EMBEDDING_MODEL
    
    with driver.session() as session:
        # 1. Functions
        query_fn = """
        MATCH (f:Function)
        WHERE ($repo_name IS NULL OR f.repo = $repo_name)
          AND ($force = true OR f.embedding IS NULL)
        RETURN coalesce(f.id, elementId(f)) AS id, f.name AS name, f.code AS code, f.file_path AS file_path
        LIMIT 500
        """
        fn_records = [dict(r) for r in session.run(query_fn, repo_name=repo_name, force=force)]
        
        # 2. Classes
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
            print(f"[Embedder] Error embedding function {f.get('name')}: {e}")
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
            print(f"[Embedder] Error embedding class {c.get('name')}: {e}")
            error_count += 1

    return {
        "status": "success" if error_count == 0 else "partial_success",
        "repo_name": repo_name or "All Repositories",
        "total_entities_evaluated": total_to_embed,
        "embedded_successfully": success_count,
        "errors": error_count,
        "model_used": effective_model,
        "active_vllm_url": active_url,
        "dimension": EMBEDDING_DIM
    }
