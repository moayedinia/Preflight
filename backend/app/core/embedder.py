import os
import hashlib
import numpy as np

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"Warning: Could not initialize OpenAI client: {e}")

def _generate_fallback_embedding(text: str, dim: int = 1536) -> list[float]:
    """Generates a deterministic pseudo-random normalized vector for offline/mock mode."""
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()

def get_embedding(text: str, model: str = "text-embedding-3-small") -> list[float]:
    """
    Generates a 1536-dim vector embedding for a given code snippet or text.
    Falls back gracefully to a deterministic normalized embedding if API key is not configured.
    """
    if not text or not text.strip():
        return [0.0] * 1536

    clean_text = text.replace("\n", " ")[:8000]

    if client:
        try:
            response = client.embeddings.create(input=[clean_text], model=model)
            return response.data[0].embedding
        except Exception as e:
            print(f"OpenAI embedding error: {e}. Using deterministic fallback.")
            return _generate_fallback_embedding(clean_text)
    
    return _generate_fallback_embedding(clean_text)
