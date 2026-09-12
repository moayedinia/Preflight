import os
import hashlib
import json
import urllib.request
import numpy as np

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "jina-embeddings-v2-base-code" if OLLAMA_BASE_URL else "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768" if OLLAMA_BASE_URL else "1536"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

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

def _query_ollama_native(text: str, model: str, base_url: str) -> list[float]:
    """Queries Ollama native /api/embeddings endpoint."""
    url = f"{base_url.rstrip('/')}/api/embeddings"
    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("embedding", [])

def _generate_fallback_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Generates a deterministic pseudo-random normalized vector for offline/mock mode."""
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()

def get_embedding(text: str, model: str = None) -> list[float]:
    """
    Generates a vector embedding for code snippet or text using:
    1. Ollama (e.g. Jina Embeddings: jina-embeddings-v2-base-code) if OLLAMA_BASE_URL is set.
    2. OpenAI (text-embedding-3-small) if OPENAI_API_KEY is set.
    3. Deterministic normalized embedding fallback if neither is reachable.
    """
    if not text or not text.strip():
        return [0.0] * EMBEDDING_DIM

    clean_text = text.replace("\n", " ")[:8000]
    target_model = model or EMBEDDING_MODEL

    # 1. Ollama (Local/Corporate)
    if OLLAMA_BASE_URL:
        if client:
            try:
                response = client.embeddings.create(input=[clean_text], model=target_model)
                return response.data[0].embedding
            except Exception:
                pass
        try:
            emb = _query_ollama_native(clean_text, target_model, OLLAMA_BASE_URL)
            if emb:
                return emb
        except Exception as ex:
            print(f"Ollama embedding query failed ({ex}). Falling back to deterministic vector.")
            return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)

    # 2. OpenAI
    if client and OPENAI_API_KEY:
        try:
            response = client.embeddings.create(input=[clean_text], model=target_model)
            return response.data[0].embedding
        except Exception as e:
            print(f"OpenAI embedding error: {e}. Using deterministic fallback.")
            return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)

    # 3. Deterministic fallback
    return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)

