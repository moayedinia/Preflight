import os
import json
import requests
from typing import Optional
from app.core.celery import CELERY_BROKER_URL

try:
    import redis
    r_client = redis.from_url(CELERY_BROKER_URL, decode_responses=True)
except Exception:
    r_client = None

def get_llm_config() -> dict:
    if r_client:
        config = r_client.get("codegraph_llm_config")
        if config:
            return json.loads(config)
    return {
        "base_url": os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        "model": os.getenv("LLM_MODEL", "llama3"),
        "api_key": os.getenv("LLM_API_KEY", "")
    }

def set_llm_config(base_url: str, model: str, api_key: str):
    if r_client:
        r_client.set("codegraph_llm_config", json.dumps({
            "base_url": base_url,
            "model": model,
            "api_key": api_key
        }))

def call_llm(prompt: str, system_prompt: str = "") -> str:
    """Calls any OpenAI-compatible endpoint (Ollama, vLLM, LMStudio, OpenAI)."""
    config = get_llm_config()
    headers = {"Content-Type": "application/json"}
    if config.get("api_key"):
        headers["Authorization"] = f"Bearer {config['api_key']}"
    
    payload = {
        "model": config.get("model", ""),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    
    url = f"{config.get('base_url', '').rstrip('/')}/chat/completions"
    
    response = requests.post(url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()
