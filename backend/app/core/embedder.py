import os
import hashlib
import numpy as np

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "jinaai/jina-embeddings-v2-base-code")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))
HF_HOME = os.getenv("HF_HOME", "/app/models") # Default local model dir

encoder = None
try:
    from sentence_transformers import SentenceTransformer
    print(f"Loading HuggingFace model: {EMBEDDING_MODEL}")
    
    # Resolve absolute path if the model is locally downloaded in HF_HOME
    model_path = EMBEDDING_MODEL
    if not os.path.isabs(model_path):
        # Check standard name
        local_path = os.path.join(HF_HOME, model_path)
        # Check HuggingFace cache format name (models--org--model)
        hf_cache_name = "models--" + model_path.replace("/", "--")
        local_cache_path = os.path.join(HF_HOME, hf_cache_name)
        
        if os.path.exists(local_path):
            model_path = local_path
        elif os.path.exists(local_cache_path):
            model_path = local_cache_path
        elif os.path.exists(os.path.join(HF_HOME, "hub", hf_cache_name)):
            # If user mapped ~/.cache/huggingface/hub to /app/models/hub
            model_path = os.path.join(HF_HOME, "hub", hf_cache_name)
            
    # trust_remote_code=True is usually required for Jina embeddings
    encoder = SentenceTransformer(model_path, trust_remote_code=True)
except Exception as e:
    print(f"Notice: Could not load SentenceTransformer model {EMBEDDING_MODEL}: {e}")

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
    Generates a vector embedding for code snippet or text using local HuggingFace model.
    """
    global EMBEDDING_DIM
    if not text or not text.strip():
        return [0.0] * EMBEDDING_DIM

    clean_text = text.replace("\n", " ")[:8000]

    if encoder:
        try:
            emb = encoder.encode(clean_text).tolist()
            if len(emb) != EMBEDDING_DIM:
                EMBEDDING_DIM = len(emb)
            return emb
        except Exception as e:
            print(f"[Embedder] HF embedding error: {e}. Using fallback.")
            return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)

    return _generate_fallback_embedding(clean_text, EMBEDDING_DIM)

def verify_ollama_status() -> dict:
    """
    Diagnostic tool, updated for local HuggingFace.
    """
    if encoder:
        return {
            "status": "connected",
            "server_url": "local_huggingface",
            "target_model": EMBEDDING_MODEL,
            "model_present": True,
            "configured_dimension": EMBEDDING_DIM,
            "available_models": [EMBEDDING_MODEL]
        }
    else:
        return {
            "status": "error",
            "server_url": "local_huggingface",
            "target_model": EMBEDDING_MODEL,
            "configured_dimension": EMBEDDING_DIM,
            "available_models": [],
            "error_detail": "Failed to load HuggingFace model."
        }
