import os
import hashlib
import numpy as np

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "codesage/codesage-large-v2")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))
HF_HOME = os.getenv("HF_HOME", "/app/models") # Default local model dir

def find_hf_model_dir(base_dir: str) -> str:
    """Recursively searches for a valid HuggingFace model directory containing config.json and model weights."""
    if not os.path.exists(base_dir):
        return base_dir
    for root, dirs, files in os.walk(base_dir):
        if "config.json" in files and ("model.safetensors" in files or "pytorch_model.bin" in files or "model.onnx" in files):
            return root
    return base_dir

encoder = None
try:
    from sentence_transformers import SentenceTransformer
    print(f"Loading HuggingFace model for EMBEDDING_MODEL: {EMBEDDING_MODEL}")
    
    # 1. Start with the default mounted models directory
    search_path = "/app/models"
    
    # If the user provided a specific absolute path that exists, use it as the search base
    if os.path.isabs(EMBEDDING_MODEL) and os.path.exists(EMBEDDING_MODEL):
        search_path = EMBEDDING_MODEL
        
    model_path = find_hf_model_dir(search_path)
            
    print(f"[Embedder] Final resolved model_path: {model_path}")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    # trust_remote_code=True is usually required for some custom models
    encoder = SentenceTransformer(model_path, trust_remote_code=True, local_files_only=True)
except Exception as e:
    print(f"Notice: Could not load SentenceTransformer model {model_path}: {e}")

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
