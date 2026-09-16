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
DEFAULT_EMBEDDING_MODEL = "jina-embeddings-v2-code:latest"
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ALLOW_FAKE_EMBEDDINGS = _as_bool(os.getenv("ALLOW_FAKE_EMBEDDINGS"), default=False)


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

def list_ollama_models(base_url: str = None) -> list[str]:
    """Fetches list of available model tags from the remote or local Ollama server."""
    target_url = (base_url or OLLAMA_BASE_URL or "").rstrip('/')
    if not target_url or not target_url.startswith("http"):
        return []
    try:
        req = urllib.request.Request(f"{target_url}/api/tags", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [m.get("name") for m in data.get("models", []) if m.get("name")]
    except Exception as e:
        print(f"Notice: Unable to query Ollama tags from {target_url}: {e}")
        return []

def _query_ollama(text: str, model: str, base_url: str) -> tuple[list[float], str]:
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
        with urllib.request.urlopen(req, timeout=60) as resp:
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
        with urllib.request.urlopen(req, timeout=60) as resp:
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            data_arr = data.get("data", [])
            if data_arr and "embedding" in data_arr[0]:
                return data_arr[0]["embedding"], ""
    except Exception as e:
        errors.append(f"/v1/embeddings: {e}")

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
    1. Ollama (e.g. nomic-embed-text, jina-embeddings-v2-base-code) on remote/internal server if OLLAMA_BASE_URL is set.
    2. OpenAI (text-embedding-3-small) if OPENAI_API_KEY is set.
    3. Optional deterministic test vectors only when ALLOW_FAKE_EMBEDDINGS=true.
    """
    if not text or not text.strip():
        return [0.0] * EMBEDDING_DIM

    clean_text = text.replace("\n", " ")[:8000]
    target_model = validate_embedding_model_name(model or EMBEDDING_MODEL)

    # 1. Ollama
    if OLLAMA_BASE_URL:
        emb, err = _query_ollama(clean_text, target_model, OLLAMA_BASE_URL)
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
            f"Ollama embedding failed for '{target_model}' at '{OLLAMA_BASE_URL}': {err}"
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


def validate_embedding_configuration() -> int:
    """Verify provider reachability, model availability, and vector dimension."""
    if not OLLAMA_BASE_URL:
        if ALLOW_FAKE_EMBEDDINGS:
            return EMBEDDING_DIM
        raise EmbeddingError("OLLAMA_BASE_URL is not configured.")

    vector, err = _query_ollama("code graph embedding health check", EMBEDDING_MODEL, OLLAMA_BASE_URL)
    if not vector:
        raise EmbeddingError(
            f"Cannot initialize embeddings from '{OLLAMA_BASE_URL}' using "
            f"'{EMBEDDING_MODEL}': {err}"
        )
    if len(vector) != EMBEDDING_DIM:
        raise EmbeddingError(
            f"Embedding dimension mismatch for '{EMBEDDING_MODEL}': "
            f"detected {len(vector)}, configured {EMBEDDING_DIM}."
        )
    return EMBEDDING_DIM

def verify_ollama_status() -> dict:
    """
    Diagnostic tool to verify connectivity to the remote/internal Ollama server,
    check model availability, and validate embedding output & dimension.
    """
    if not OLLAMA_BASE_URL:
        return {
            "status": "not_configured",
            "message": "OLLAMA_BASE_URL is not set in environment.",
            "server_url": None,
            "target_model": EMBEDDING_MODEL,
            "available_models": []
        }

    available_models = list_ollama_models(OLLAMA_BASE_URL)
    test_vec, err = _query_ollama("test code embedding", EMBEDDING_MODEL, OLLAMA_BASE_URL)

    if test_vec:
        dimension_matches = len(test_vec) == EMBEDDING_DIM
        return {
            "status": "connected" if dimension_matches else "dimension_mismatch",
            "server_url": OLLAMA_BASE_URL,
            "target_model": EMBEDDING_MODEL,
            "model_present": (EMBEDDING_MODEL in available_models) if available_models else True,
            "detected_dimension": len(test_vec),
            "configured_dimension": EMBEDDING_DIM,
            "dimension_matches": dimension_matches,
            "available_models": available_models,
            "sample_vector_preview": test_vec[:5]
        }
    else:
        return {
            "status": "error",
            "server_url": OLLAMA_BASE_URL,
            "target_model": EMBEDDING_MODEL,
            "configured_dimension": EMBEDDING_DIM,
            "available_models": available_models,
            "error_detail": err or "No embedding returned from Ollama endpoint."
        }
