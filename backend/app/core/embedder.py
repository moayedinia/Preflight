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


def normalize_ollama_base_url(value: str | None) -> str:
    """Return the Ollama server root (native API base, without /v1)."""
    base = (value or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    if base:
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid OLLAMA_BASE_URL: {value!r}")
    return base


OLLAMA_BASE_URL = normalize_ollama_base_url(
    os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
)
DEFAULT_EMBEDDING_MODEL = "unclemusclez/jina-embeddings-v2-base-code:latest"
PREFERRED_EMBEDDING_MODELS = [
    "unclemusclez/jina-embeddings-v2-base-code:latest",
    "unclemusclez/jina-embeddings-v2-base-code",
    "jina-embeddings-v2-base-code:latest",
    "jina-embeddings-v2-base-code",
    "jina-embeddings-v2-code:latest",
    "jina-embeddings-v2-code",
]
EMBEDDING_MODEL = DEFAULT_EMBEDDING_MODEL
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ALLOW_FAKE_EMBEDDINGS = _as_bool(os.getenv("ALLOW_FAKE_EMBEDDINGS"), default=False)

_STATUS_CACHE: dict = {"timestamp": 0, "result": None}
_LAST_LOGGED_ERROR: str | None = None
_VERIFIED_WORKING_OLLAMA_URL: str | None = None
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
    global _STATUS_CACHE, _LAST_LOGGED_ERROR, _VERIFIED_WORKING_OLLAMA_URL, _ENSURED_EMBEDDING_MODEL
    _STATUS_CACHE = {"timestamp": 0, "result": None}
    _LAST_LOGGED_ERROR = None
    _VERIFIED_WORKING_OLLAMA_URL = None
    _ENSURED_EMBEDDING_MODEL = None

def get_active_ollama_url() -> str:
    """
    Returns the verified reachable Ollama URL for code embeddings.
    Dedicated exclusively to embedding operations (not mutated by chat LLM settings).
    """
    global _VERIFIED_WORKING_OLLAMA_URL
    if _VERIFIED_WORKING_OLLAMA_URL:
        return _VERIFIED_WORKING_OLLAMA_URL

    configured_url = OLLAMA_BASE_URL
    port = 11434
    try:
        parsed = urlparse(configured_url)
        if parsed.port:
            port = parsed.port
    except Exception:
        pass

    candidates = []
    # 1. Configured URL
    if configured_url:
        candidates.append(configured_url)
    # 2. Known WSL2/Hyper-V host gateway
    candidates.append(f"http://172.17.80.1:{port}")
    # 3. Gateway from Linux routing table (Docker container default gateway to host)
    gw_ip = _get_linux_default_gateway()
    if gw_ip:
        candidates.append(f"http://{gw_ip}:{port}")
    # 4. Standard host adapters
    candidates.append(f"http://172.26.0.1:{port}")
    candidates.append(f"http://10.164.1.43:{port}")
    candidates.append(f"http://host.docker.internal:{port}")
    candidates.append(f"http://host-gateway:{port}")
    candidates.append(f"http://172.17.0.1:{port}")
    candidates.append(f"http://127.0.0.1:{port}")
    candidates.append(f"http://localhost:{port}")

    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            req = urllib.request.Request(f"{cand}/api/tags", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    _VERIFIED_WORKING_OLLAMA_URL = cand
                    if cand != configured_url:
                        print(f"[Embedder] Connected to Ollama endpoint at {cand} (primary '{configured_url}' deferred).", flush=True)
                    return _VERIFIED_WORKING_OLLAMA_URL
        except Exception:
            continue

    # Fallback to configured URL if all probes fail
    return configured_url or "http://127.0.0.1:11434"

def pull_ollama_model(base_url: str, model_name: str, timeout: int = 300) -> bool:
    """
    Requests Ollama to pull/download the specified model from the Ollama library.
    POST /api/pull with {"name": model_name, "stream": false}.
    """
    base = normalize_ollama_base_url(base_url)
    if not base:
        return False
    url = f"{base}/api/pull"
    payload = json.dumps({"name": model_name, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"}
    )
    print(f"[Embedder] Requesting Ollama at {base} to pull model '{model_name}'...", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "success" or "success" in str(data).lower():
                print(f"[Embedder] Successfully pulled model '{model_name}' on Ollama.", flush=True)
                return True
            print(f"[Embedder] Ollama pull status: {data}", flush=True)
            return True
    except Exception as e:
        print(f"[Embedder Notice] Ollama pull request for '{model_name}' encountered: {e}", flush=True)
        return False

def ensure_embedding_model(base_url: str) -> str:
    """
    Finds the active Jina code embedding model on Ollama.
    If none is found, initiates an automatic pull of DEFAULT_EMBEDDING_MODEL.
    """
    global _ENSURED_EMBEDDING_MODEL
    if _ENSURED_EMBEDDING_MODEL:
        return _ENSURED_EMBEDDING_MODEL

    available = list_ollama_models(base_url, timeout=2)
    if available:
        for pref in PREFERRED_EMBEDDING_MODELS:
            if pref in available:
                _ENSURED_EMBEDDING_MODEL = pref
                return pref
        for m in available:
            low = m.lower()
            if "jina" in low and ("code" in low or "embed" in low):
                _ENSURED_EMBEDDING_MODEL = m
                return m

    # Not installed on Ollama -> Request Ollama to pull default model
    if base_url:
        print(f"[Embedder] Embedding model not detected on Ollama. Triggering auto-pull for '{DEFAULT_EMBEDDING_MODEL}'...", flush=True)
        if pull_ollama_model(base_url, DEFAULT_EMBEDDING_MODEL, timeout=180):
            _ENSURED_EMBEDDING_MODEL = DEFAULT_EMBEDDING_MODEL
            return DEFAULT_EMBEDDING_MODEL

    _ENSURED_EMBEDDING_MODEL = DEFAULT_EMBEDDING_MODEL
    return DEFAULT_EMBEDDING_MODEL

class EmbeddingError(RuntimeError):
    """Raised when a real embedding cannot be produced safely."""


def validate_embedding_model_name(model: str) -> str:
    """Reject rerankers early: Ollama's embedding endpoint requires an embedding model."""
    normalized = (model or "").strip()
    if not normalized:
        raise EmbeddingError("EMBEDDING_MODEL is empty.")
    if "rerank" in normalized.lower():
        raise EmbeddingError(
            f"'{normalized}' is a reranker, not an embedding model. Use "
            f"'{DEFAULT_EMBEDDING_MODEL}' for jina-embeddings-v2-code on Ollama."
        )
    return normalized

client = None
if OLLAMA_BASE_URL:
    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=f"{OLLAMA_BASE_URL.rstrip('/')}/v1",
            api_key="ollama"
        )
    except Exception as e:
        print(f"Notice: Could not initialize Ollama OpenAI-compatible client: {e}")
elif OPENAI_API_KEY:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"Notice: Could not initialize OpenAI client: {e}")

def list_ollama_models(base_url: str = None, timeout: int = 2) -> list[str]:
    """Fetches list of available model tags with fast timeout and suppressed repeated error spam."""
    global _LAST_LOGGED_ERROR
    target_url = (base_url or get_active_ollama_url() or "").rstrip('/')
    if not target_url or not target_url.startswith("http"):
        return []
    try:
        req = urllib.request.Request(f"{target_url}/api/tags", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _LAST_LOGGED_ERROR = None
            return [m.get("name") for m in data.get("models", []) if m.get("name")]
    except Exception as e:
        err_msg = str(e)
        if _LAST_LOGGED_ERROR != err_msg:
            print(f"Notice: Ollama not reachable at {target_url} ({err_msg}). Switching to fallback mode.")
            _LAST_LOGGED_ERROR = err_msg
        return []

def _query_ollama(text: str, model: str, base_url: str, timeout: int = 60) -> tuple[list[float], str]:

    """
    Queries remote/internal Ollama server across standard embedding endpoints:
    1. /api/embed (Modern Ollama >= 0.1.44)
    2. /api/embeddings (Ollama legacy endpoint)
    3. /v1/embeddings (OpenAI-compatible Ollama endpoint)
    Returns: (embedding_vector, last_error_message)
    """
    base = normalize_ollama_base_url(base_url)
    try:
        model = validate_embedding_model_name(model)
    except EmbeddingError as exc:
        return [], str(exc)
    clean_text = text.replace("\n", " ")[:8000]
    errors = []

    # 1. Try modern /api/embed
    try:
        url = f"{base}/api/embed"
        payload = json.dumps({"model": model, "input": clean_text}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            embs = data.get("embeddings")
            if embs and len(embs) > 0 and len(embs[0]) > 0:
                return embs[0], ""
    except urllib.error.HTTPError as he:
        try:
            errors.append(f"/api/embed: HTTP {he.code} - {he.read().decode('utf-8')}")
        except Exception:
            errors.append(f"/api/embed: HTTP {he.code}")
    except Exception as e:
        errors.append(f"/api/embed: {e}")

    # 2. Try legacy /api/embeddings
    try:
        url = f"{base}/api/embeddings"
        payload = json.dumps({"model": model, "prompt": clean_text}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            emb = data.get("embedding")
            if emb and len(emb) > 0:
                return emb, ""
    except urllib.error.HTTPError as he:
        try:
            errors.append(f"/api/embeddings: HTTP {he.code} - {he.read().decode('utf-8')}")
        except Exception:
            errors.append(f"/api/embeddings: HTTP {he.code}")
    except Exception as e:
        errors.append(f"/api/embeddings: {e}")

    # 3. Try /v1/embeddings (OpenAI format on Ollama)
    try:
        url = f"{base}/v1/embeddings"
        payload = json.dumps({"model": model, "input": clean_text}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer ollama"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            data_arr = data.get("data", [])
            if data_arr and "embedding" in data_arr[0]:
                return data_arr[0]["embedding"], ""
    except Exception as e:
        errors.append(f"/v1/embeddings: {e}")

    # If the model failed with 404 / not found, try to auto-detect matching available model or auto-pull on Ollama
    if any("not found" in str(err).lower() for err in errors):
        try:
            available = list_ollama_models(base, timeout=2)
            matched = None
            if available:
                for cand in available:
                    if any(token in cand.lower() for token in ["jina", "embed", "code"]):
                        matched = cand
                        break
            if matched and matched != model:
                print(f"[Embedder] Model '{model}' not found on Ollama. Auto-fallback to available model '{matched}'.", flush=True)
                return _query_ollama(text, matched, base_url, timeout=timeout)
            
            # Trigger auto-pull of default embedding model
            print(f"[Embedder] Embedding model not installed on Ollama. Initiating auto-pull of '{DEFAULT_EMBEDDING_MODEL}'...", flush=True)
            if pull_ollama_model(base, DEFAULT_EMBEDDING_MODEL, timeout=180):
                return _query_ollama(text, DEFAULT_EMBEDDING_MODEL, base_url, timeout=timeout)
        except Exception:
            pass

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
    1. Ollama (jina-embeddings-v2-base-code) on remote/internal server with auto-discovery and auto-pull.
    2. OpenAI (text-embedding-3-small) if OPENAI_API_KEY is set.
    3. Optional deterministic test vectors only when ALLOW_FAKE_EMBEDDINGS=true.
    """
    if not text or not text.strip():
        return [0.0] * EMBEDDING_DIM

    clean_text = text.replace("\n", " ")[:8000]
    active_url = get_active_ollama_url()
    target_model = model or (ensure_embedding_model(active_url) if active_url else DEFAULT_EMBEDDING_MODEL)
    target_model = validate_embedding_model_name(target_model)

    # 1. Ollama
    if active_url:
        emb, err = _query_ollama(clean_text, target_model, active_url)
        if emb:
            if len(emb) != EMBEDDING_DIM:
                raise EmbeddingError(
                    f"Embedding dimension mismatch for '{target_model}': "
                    f"Ollama returned {len(emb)}, but EMBEDDING_DIM={EMBEDDING_DIM}. "
                    "Update EMBEDDING_DIM and recreate/re-index the vector data."
                )
            return emb
        if ALLOW_FAKE_EMBEDDINGS:
            print(f"[Embedder] Ollama query failed: {err}. ALLOW_FAKE_EMBEDDINGS is enabled; using a deterministic test vector.")
            return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)
        raise EmbeddingError(
            f"Ollama embedding failed for '{target_model}' at '{active_url}': {err}"
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
    active_url = get_active_ollama_url()
    if not active_url:
        if ALLOW_FAKE_EMBEDDINGS:
            return EMBEDDING_DIM
        raise EmbeddingError("OLLAMA_BASE_URL is not configured.")

    effective_model = ensure_embedding_model(active_url)
    vector, err = _query_ollama("code graph embedding health check", effective_model, active_url, timeout=timeout)
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

def verify_ollama_status() -> dict:
    """
    Diagnostic tool to verify connectivity to the remote/internal Ollama server,
    check model availability, and validate embedding output & dimension.
    Caches result for 15 seconds to prevent hammering sockets or blocking worker threads.
    """
    import time
    global _STATUS_CACHE
    now = time.time()
    if _STATUS_CACHE.get("result") and (now - _STATUS_CACHE.get("timestamp", 0)) < 15:
        return _STATUS_CACHE["result"]

    active_url = get_active_ollama_url()
    if not active_url:
        res = {
            "status": "not_configured",
            "message": "Ollama base URL is not configured.",
            "server_url": None,
            "target_model": DEFAULT_EMBEDDING_MODEL,
            "available_models": []
        }
        _STATUS_CACHE = {"timestamp": now, "result": res}
        return res

    effective_model = ensure_embedding_model(active_url)
    available_models = list_ollama_models(active_url, timeout=2)
    test_vec, err = _query_ollama("test code embedding", effective_model, active_url, timeout=4)

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
            "error_detail": err or "Ollama server is offline or unreachable."
        }

    _STATUS_CACHE = {"timestamp": now, "result": res}
    return res

