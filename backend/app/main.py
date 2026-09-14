import os
import shutil
import tempfile
try:
    import redis
except ImportError:
    redis = None
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Security, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

from app.core.parser import parse_source_file
from app.core.graph import (
    driver,
    ingest_parsed_data,
    init_graph_schema,
    get_graph_stats,
    get_graph_elements
)
from app.core.rag import execute_graphrag_query
from app.core.scanner import scan_and_ingest_directory
from app.tasks.ingestion import (
    async_index_repository,
    async_clone_and_index,
    async_extract_and_index
)
from app.core.celery import celery_app, CELERY_BROKER_URL
from app.core.security import (
    verify_api_key,
    validate_safe_path,
    sanitize_name,
    ALLOWED_REPO_DIR
)
from app.core.cloner import clone_git_repo, extract_safe_zip
from app.core.analytics import (
    detect_circular_dependencies,
    detect_high_risk_hubs,
    detect_dead_code_candidates,
    calculate_blast_radius
)
from app.core.repo_manager import (
    list_all_repositories,
    get_repository_summary,
    delete_repository
)
from app.core.reviewer import generate_architectural_code_review

app = FastAPI(
    title="Enterprise Code Knowledge Graph & GraphRAG API",
    description="Automated Git cloning, ZIP extraction, AST ingestion, vector embeddings, GraphRAG retrieval, Code Smells analysis, and PR Review.",
    version="2.0.0"
)

# Configure CORS securely from environment
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
allowed_origins = [orig.strip() for orig in allowed_origins_env.split(",") if orig.strip()]
if "*" in allowed_origins or not allowed_origins:
    allowed_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    try:
        init_graph_schema()
    except Exception as e:
        print(f"Schema initialization warning: {e}")

# Request Models
class CodeUpload(BaseModel):
    repo_name: str = Field(..., min_length=1, max_length=100)
    file_path: str = Field(..., min_length=1, max_length=300)
    code_content: str = Field(..., min_length=1, max_length=1_000_000)

class GitIngestRequest(BaseModel):
    git_url: str = Field(..., min_length=5, max_length=500, description="Git clone URL (e.g. https://github.com/org/repo.git)")
    repo_name: Optional[str] = Field(None, max_length=100, description="Optional custom name for repository")
    branch: Optional[str] = Field(None, max_length=100, description="Optional branch or tag")
    auth_token: Optional[str] = Field(None, max_length=200, description="Optional GitHub/GitLab personal access token")
    is_async: Optional[bool] = True

class RepoScanRequest(BaseModel):
    repo_path: str = Field(..., min_length=1, max_length=500)
    repo_name: Optional[str] = Field(None, max_length=100)
    is_async: Optional[bool] = True

class QueryRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=2000)
    repo_name: Optional[str] = Field(None, max_length=100)
    top_k: Optional[int] = Field(5, ge=1, le=25)

class DiffReviewRequest(BaseModel):
    diff_text: str = Field(..., min_length=5, max_length=500_000, description="Unified Git diff content")
    repo_name: Optional[str] = Field(None, max_length=100)

class LLMConfigRequest(BaseModel):
    base_url: str = Field(..., max_length=500)
    model: str = Field(..., max_length=100)
    api_key: Optional[str] = Field("", max_length=200)

class AutoDocRequest(BaseModel):
    repo_name: Optional[str] = Field(None, max_length=100)

@app.get("/healthz")
def healthcheck():
    """
    Production Healthcheck: Verifies connectivity to Neo4j graph database and Redis message broker.
    Returns 200 if all core dependencies are responsive, otherwise 503 Service Unavailable.
    """
    neo4j_status = "unknown"
    redis_status = "unknown"
    is_healthy = True

    try:
        driver.verify_connectivity()
        neo4j_status = "healthy"
    except Exception as e:
        neo4j_status = f"unhealthy: {str(e)}"
        is_healthy = False

    if redis:
        try:
            r = redis.from_url(CELERY_BROKER_URL, socket_timeout=2)
            r.ping()
            redis_status = "healthy"
        except Exception as e:
            redis_status = f"unhealthy: {str(e)}"
            is_healthy = False
    else:
        redis_status = "unhealthy: redis module not installed"
        is_healthy = False

    payload = {
        "status": "healthy" if is_healthy else "degraded",
        "neo4j": neo4j_status,
        "redis": redis_status,
        "version": "2.0.0"
    }
    if not is_healthy:
        raise HTTPException(status_code=503, detail=payload)
    return payload

@app.get("/api/v1/ollama/status")
def check_ollama_status():
    """
    Diagnostic Endpoint: Verifies connectivity to the remote/internal Ollama server,
    retrieves loaded model tags, and validates the Jina embeddings vector generation.
    """
    from app.core.embedder import verify_ollama_status
    return verify_ollama_status()

@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "Enterprise Code GraphRAG API (Automated Ingestion)",
        "version": "2.0.0",
        "security": "API Key authentication active" if os.getenv("API_KEY") else "Dev Mode (Unauthenticated)"
    }

# 1. Automated Git Clone & Ingest
@app.post("/api/v1/ingest/git", dependencies=[Depends(verify_api_key)])
def ingest_from_git(payload: GitIngestRequest):
    """
    Automated Git Pipeline: Clones repository from URL (public or private with token),
    extracts AST, computes vector embeddings, and links knowledge graph in Neo4j.
    """
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None

    if payload.is_async:
        task = async_clone_and_index.delay(
            git_url=payload.git_url,
            repo_name=clean_repo,
            branch=payload.branch,
            token=payload.auth_token
        )
        return {
            "status": "queued",
            "task_id": task.id,
            "message": "Automated Git cloning and indexing started in background worker."
        }
    else:
        try:
            cloned_dir = clone_git_repo(
                git_url=payload.git_url,
                repo_name=clean_repo,
                branch=payload.branch,
                token=payload.auth_token
            )
            actual_name = clean_repo or cloned_dir.name
            result = scan_and_ingest_directory(str(cloned_dir), actual_name)
            return {"status": "success", "result": result}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

# 2. Automated ZIP Upload & Ingest
@app.post("/api/v1/ingest/upload", dependencies=[Depends(verify_api_key)])
async def ingest_from_zip(
    file: UploadFile = File(...),
    repo_name: Optional[str] = Form(None),
    is_async: Optional[bool] = Form(True)
):
    """
    Automated ZIP Upload Pipeline: Upload a project zip file, extracts safely
    with Zip Slip protection, and indexes into the knowledge graph.
    """
    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip archive files are supported.")

    derived_name = repo_name or file.filename.replace(".zip", "")
    clean_repo = sanitize_name(derived_name)

    # Save uploaded file to temp directory
    temp_dir = Path(tempfile.gettempdir())
    temp_zip_path = temp_dir / f"upload_{clean_repo}_{file.filename}"

    with open(temp_zip_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    if is_async:
        task = async_extract_and_index.delay(str(temp_zip_path), clean_repo)
        return {
            "status": "queued",
            "task_id": task.id,
            "message": "ZIP archive uploaded. Extraction and indexing running in background."
        }
    else:
        try:
            extracted_dir = extract_safe_zip(temp_zip_path, clean_repo)
            result = scan_and_ingest_directory(str(extracted_dir), clean_repo)
            temp_zip_path.unlink(missing_ok=True)
            return {"status": "success", "result": result}
        except Exception as e:
            temp_zip_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(e))

# 3. Single Snippet Analysis
@app.post("/api/v1/analyze/code", dependencies=[Depends(verify_api_key)])
def analyze_code_snippet(payload: CodeUpload):
    if ".." in payload.file_path or payload.file_path.startswith("/") or payload.file_path.startswith("\\"):
        raise HTTPException(status_code=400, detail="Invalid file path: Must be a clean relative path.")

    clean_repo = sanitize_name(payload.repo_name)

    try:
        code_bytes = payload.code_content.encode("utf-8")
        parsed_data = parse_source_file(payload.file_path, code_bytes)
        ingest_parsed_data(clean_repo, parsed_data)

        return {
            "status": "success",
            "repo_name": clean_repo,
            "file_path": payload.file_path,
            "language": parsed_data.get("language", "unknown"),
            "summary": {
                "classes": len(parsed_data["classes"]),
                "functions": len(parsed_data["functions"]),
                "imports": len(parsed_data["imports"])
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

# 4. Existing Directory Scan
@app.post("/api/v1/analyze/repo", dependencies=[Depends(verify_api_key)])
def analyze_repository(payload: RepoScanRequest):
    safe_path = validate_safe_path(payload.repo_path)
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else sanitize_name(safe_path.name)

    if payload.is_async:
        task = async_index_repository.delay(str(safe_path), clean_repo)
        return {
            "status": "queued",
            "task_id": task.id,
            "message": "Repository indexing queued in background worker."
        }
    else:
        try:
            result = scan_and_ingest_directory(str(safe_path), clean_repo)
            return {"status": "success", "result": result}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Scanning failed: {str(e)}")

# 5. Task Progress & Status
@app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(verify_api_key)])
def get_task_status(task_id: str):
    """Returns status and metadata for background Celery tasks."""
    if not task_id or len(task_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid task ID format.")
    res = celery_app.AsyncResult(task_id)
    info = res.info if isinstance(res.info, dict) else {}
    return {
        "task_id": task_id,
        "status": res.status,
        "progress": info.get("status", "Processing..."),
        "result": res.result if res.ready() else None
    }

# 6. GraphRAG Query
@app.post("/api/v1/query", dependencies=[Depends(verify_api_key)])
def graphrag_query(payload: QueryRequest):
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    try:
        result = execute_graphrag_query(
            query=payload.query,
            repo_name=clean_repo,
            top_k=payload.top_k
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query failed: {str(e)}")

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=2, max_length=2000)
    repo_name: Optional[str] = Field(None, max_length=100)

@app.post("/api/v1/chat", dependencies=[Depends(verify_api_key)])
def chat_rag_endpoint(payload: ChatRequest):
    """
    Built-in RAG Chat: Conversational interface for the knowledge graph.
    Currently returns structured graph traversal since LLM is offline.
    """
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    try:
        result = execute_graphrag_query(
            query=payload.message,
            repo_name=clean_repo,
            top_k=4
        )
        return {"response": result["answer"], "sources": result["retrieved_nodes"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chat failed: {str(e)}")

# 7. Graph Statistics & Visual Elements
@app.get("/api/v1/graph/stats", dependencies=[Depends(verify_api_key)])
def graph_stats():
    try:
        return get_graph_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/graph/elements", dependencies=[Depends(verify_api_key)])
def graph_elements(repo_name: Optional[str] = None, limit: Optional[int] = 120):
    clean_limit = min(max(limit or 100, 10), 300)
    clean_repo = sanitize_name(repo_name) if repo_name else None
    try:
        return get_graph_elements(repo_name=clean_repo, limit=clean_limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 8. Multi-Repository Management
@app.get("/api/v1/repos", dependencies=[Depends(verify_api_key)])
def list_repositories():
    """Returns all indexed repositories with file counts, classes, functions, and languages."""
    try:
        return list_all_repositories()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/repos/{repo_name}", dependencies=[Depends(verify_api_key)])
def get_repository(repo_name: str):
    """Returns summary statistics and languages for a specific repository."""
    return get_repository_summary(repo_name)

@app.delete("/api/v1/repos/{repo_name}", dependencies=[Depends(verify_api_key)])
def remove_repository(repo_name: str):
    """Deletes repository, cascade deletes graph relationships and disk files."""
    return delete_repository(repo_name)

# 9. Architectural Code Smells & Blast Radius Analytics
@app.get("/api/v1/analytics/code-smells", dependencies=[Depends(verify_api_key)])
def analyze_code_smells(repo_name: Optional[str] = None):
    """
    Analyzes architectural anti-patterns:
    - Circular function call chains
    - High-risk hub functions (God functions)
    - Dead code candidates (0 incoming calls)
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    try:
        return {
            "repo_name": clean_repo or "ALL",
            "circular_dependencies": detect_circular_dependencies(clean_repo),
            "high_risk_hubs": detect_high_risk_hubs(clean_repo),
            "dead_code_candidates": detect_dead_code_candidates(clean_repo)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/analytics/impact/{function_name}", dependencies=[Depends(verify_api_key)])
def analyze_impact(function_name: str, repo_name: Optional[str] = None):
    """Calculates downstream blast radius of modifying a given function."""
    clean_fn = sanitize_name(function_name)
    clean_repo = sanitize_name(repo_name) if repo_name else None
    try:
        return calculate_blast_radius(clean_fn, clean_repo)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 10. Automated Architectural PR / Git Diff Review
@app.post("/api/v1/review/diff", dependencies=[Depends(verify_api_key)])
def review_git_diff(payload: DiffReviewRequest):
    """
    Architectural Review Engine: Analyzes a Git diff against the Knowledge Graph,
    identifying modified symbols, downstream blast radius, and generating an AI review.
    """
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    try:
        return generate_architectural_code_review(payload.diff_text, repo_name=clean_repo)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Review failed: {str(e)}")

# 11. GitHub CI/CD Webhook Trigger
@app.post("/api/v1/webhooks/github")
async def github_webhook(request: Request):
    """
    Automated Webhook: Triggers background clone and ingestion when code is pushed to GitHub.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    repo_info = body.get("repository", {})
    clone_url = repo_info.get("clone_url")
    raw_repo_name = repo_info.get("name")
    ref = body.get("ref", "")
    branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else None

    if not clone_url or not raw_repo_name:
        return {"status": "ignored", "message": "Not a push event or missing repository info."}

    clean_repo = sanitize_name(raw_repo_name)
    task = async_clone_and_index.delay(
        git_url=clone_url,
        repo_name=clean_repo,
        branch=branch
    )

    return {
        "status": "accepted",
        "task_id": task.id,
        "message": f"Webhook accepted for repository '{clean_repo}'. Automated indexing queued."
    }

# 12. LLM Config & Auto-Documentation
from app.core.llm import get_llm_config, set_llm_config
from app.tasks.autodoc import generate_missing_docs_task

@app.get("/api/v1/config/llm", dependencies=[Depends(verify_api_key)])
def get_llm_settings():
    return get_llm_config()

@app.post("/api/v1/config/llm", dependencies=[Depends(verify_api_key)])
def update_llm_settings(payload: LLMConfigRequest):
    set_llm_config(payload.base_url, payload.model, payload.api_key)
    return {"status": "success", "message": "LLM configuration updated successfully."}

@app.post("/api/v1/tasks/autodoc", dependencies=[Depends(verify_api_key)])
def trigger_auto_doc(payload: AutoDocRequest):
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    task = generate_missing_docs_task.delay(repo_name=clean_repo)
    return {
        "status": "queued",
        "task_id": task.id,
        "message": "Auto-Documentation engine started in background worker."
    }

