import os
import json
import shutil
import tempfile
import hashlib
import hmac
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
    get_graph_elements,
    get_node_details,
    get_vector_database_stats,
    get_vector_database_records,
    get_node_vector
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
from app.core import analytics
from app.core.analytics import (
    detect_circular_dependencies,
    detect_high_risk_hubs,
    detect_dead_code_candidates,
    calculate_blast_radius,
    detect_code_clones,
    get_git_hotspots
)
from app.core.repo_manager import (
    list_all_repositories,
    get_repository_summary,
    delete_repository
)
from app.core.reviewer import generate_architectural_code_review
from app.core.preflight import run_3tier_preflight_evaluation, compile_pure_english_markdown_report, audit_single_tier, apply_preflight_bug_fix


try:
    import orjson
    from fastapi.responses import ORJSONResponse
except ImportError:
    orjson = None
    ORJSONResponse = None

app_kwargs = {
    "title": "Enterprise Code Knowledge Graph & GraphRAG API",
    "description": "Automated Git cloning, ZIP extraction, AST ingestion, vector embeddings, GraphRAG retrieval, Code Smells analysis, and PR Review.",
    "version": "2.0.0"
}
if orjson is not None and ORJSONResponse is not None:
    app_kwargs["default_response_class"] = ORJSONResponse

app = FastAPI(**app_kwargs)

# Configure CORS securely from environment
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
allowed_origins = [orig.strip() for orig in allowed_origins_env.split(",") if orig.strip()]
if "*" in allowed_origins or not allowed_origins:
    allowed_origins = ["*"]

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True if allowed_origins != ["*"] else False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount frontend vendor scripts if directory exists
_vendor_dir = Path("/app/frontend/vendor")
if not _vendor_dir.exists():
    _vendor_dir = Path(__file__).resolve().parent.parent.parent / "frontend" / "vendor"
if _vendor_dir.exists():
    app.mount("/vendor", StaticFiles(directory=str(_vendor_dir)), name="vendor")

@app.on_event("startup")
def on_startup():
    import threading
    def _run_init():
        try:
            init_graph_schema()
        except Exception as e:
            print(f"[Schema initialization warning] {e}", flush=True)

    thread = threading.Thread(target=_run_init, daemon=True, name="schema_init_thread")
    thread.start()

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
    full_history: Optional[bool] = Field(True, description="Clone full git history (required for accurate Git Hotspots/churn). Set false for very large repos where git analysis is not needed.")
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
    base_url: Optional[str] = Field(None, max_length=500)
    model: Optional[str] = Field(None, max_length=100)
    api_key: Optional[str] = Field("", max_length=2000)
    test_connection: Optional[bool] = Field(False)

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

@app.get("/api/v1/vllm/status")
@app.get("/api/v1/embedding/status")
def check_vllm_status():
    """
    Diagnostic Endpoint: Verifies connectivity to the vLLM server,
    retrieves loaded model tags, and validates the Jina embeddings vector generation.
    """
    from app.core.embedder import verify_vllm_status
    return verify_vllm_status()

@app.get("/")
def read_root():
    frontend_index = Path("/app/frontend/index.html")
    if not frontend_index.exists():
        frontend_index = Path(__file__).resolve().parent.parent.parent / "frontend" / "index.html"
    if frontend_index.exists():
        return FileResponse(frontend_index)
    return {
        "status": "online",
        "service": "Preflight Code Quality API",
        "version": "2.0.0",
        "security": "API Key authentication active" if os.getenv("API_KEY") else "Dev Mode (Unauthenticated)",
        "docs": "/docs"
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
            token=payload.auth_token,
            full_history=payload.full_history
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
                token=payload.auth_token,
                full_history=payload.full_history
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

    # Save uploaded file to shared repository directory so worker container can access it
    uploads_dir = ALLOWED_REPO_DIR / ".uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    clean_filename = Path(file.filename).name
    temp_zip_path = uploads_dir / f"upload_{clean_repo}_{clean_filename}"

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
    
    try:
        status = res.status
    except Exception:
        status = "UNKNOWN"

    info = {}
    try:
        raw_info = res.info
        if isinstance(raw_info, dict):
            info = raw_info
        elif isinstance(raw_info, Exception):
            info = {"error": str(raw_info), "status": str(raw_info)}
        elif raw_info is not None:
            info = {"status": str(raw_info)}
    except Exception as e:
        info = {"error": str(e), "status": "Task failed or result unreadable"}

    result = None
    ready = False
    try:
        ready = res.ready()
        if ready and status != "FAILURE":
            result = res.result
    except Exception as e:
        if status == "FAILURE":
            info["error"] = info.get("error") or str(e)

    if status in ["REVOKED", "STOPPED"] or info.get("status") == "stopped":
        status = "REVOKED"
        ready = True
    percent = 100 if status == "SUCCESS" else info.get("percent", 0)

    return {
        "task_id": task_id,
        "status": status,
        "state": status,
        "progress": info.get("status", "Processing..." if not ready else ("Finished" if status == "SUCCESS" else "Failed")),
        "percent": percent,
        "current": info.get("current", 0),
        "total": info.get("total", 0),
        "current_file": info.get("current_file", ""),
        "stage": info.get("stage", ""),
        "repo_name": info.get("repo_name", (result or {}).get("repo_name", "") if isinstance(result, dict) else ""),
        "meta": info,
        "result": result
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
    message: str = Field(..., min_length=1, max_length=500_000)
    repo_name: Optional[str] = Field(None, max_length=100)
    mode: Optional[str] = Field("graphrag", description="Mode: 'graphrag', 'direct', or 'nl2cypher'")

class ExecuteSkillRequest(BaseModel):
    skill_name: str = Field(..., max_length=100)
    repo_name: Optional[str] = Field(None, max_length=100)
    params: Optional[Dict[str, Any]] = None

class GenerateAssetRequest(BaseModel):
    repo_name: str = Field(..., max_length=100)
    asset_type: str = Field(..., max_length=50)

@app.post("/api/v1/chat", dependencies=[Depends(verify_api_key)])
def chat_rag_endpoint(payload: ChatRequest):
    """
    Built-in RAG Chat & Conversational AI:
    - 'graphrag': Retrieves AST nodes and call graph, then synthesizes with LLM.
    - 'direct': Direct conversational LLM chat for coding & architecture questions.
    - 'nl2cypher': Converts natural language to Neo4j Cypher and runs it.
    """
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None

    # 1. Interactive Skills & Slash Command Dispatch
    try:
        from app.core.skills import dispatch_skill_or_command
        skill_res = dispatch_skill_or_command(payload.message, repo_name=clean_repo)
        if skill_res:
            return {
                "response": skill_res.get("markdown") or skill_res.get("error", "Skill executed."),
                "sources": [],
                "mode": "skill",
                "skill": skill_res
            }
    except Exception as skill_err:
        print(f"[Skill Dispatch Note] {skill_err}")

    mode = (payload.mode or "graphrag").strip().lower()

    if mode == "direct":
        from app.core.llm import call_llm
        context_prefix = ""
        if clean_repo:
            from app.core.preflight import collect_repo_code_samples
            code_ctx = collect_repo_code_samples(repo_name=clean_repo, max_total_chars=6000)
            context_prefix = (
                f"\nYou are currently assisting the developer with the target codebase: '{clean_repo}'.\n"
                f"Here are representative code and structure samples from '{clean_repo}':\n"
                f"{code_ctx}\n"
                f"You MUST focus your answers, code reviews, and explanations specifically on this project '{clean_repo}'.\n"
            )
        system_prompt = (
            "You are CodeGraph Brain AI Assistant, an expert software engineer and software architect. "
            "Help the user with programming, system architecture, debugging, dead code analysis, and code reviews. "
            "Provide clean, well-formatted code snippets and practical explanations in English."
            + context_prefix
        )
        try:
            answer = call_llm(payload.message, system_prompt)
            return {"response": answer, "sources": [], "mode": "direct"}
        except Exception as e:
            fallback_msg = (
                "⚠️ **Language Model (LLM) service is currently unavailable.**\n\n"
                f"Connection details: `{str(e)}`\n\n"
                "💡 **Help:** Please verify that your LLM API key and base URL are configured properly in **Settings**."
            )
            return {"response": fallback_msg, "sources": [], "mode": "direct", "warning": str(e)}

    elif mode == "nl2cypher" or payload.message.strip().startswith("/cypher "):
        from app.core.rag import execute_nl2cypher_query
        actual_query = payload.message.replace("/cypher ", "").strip()
        try:
            result = execute_nl2cypher_query(actual_query, clean_repo)
            return {"response": result["answer"], "sources": result.get("retrieved_nodes", []), "mode": "nl2cypher"}
        except Exception as e:
            fallback_msg = (
                "⚠️ **Error converting and executing Graph Query (NL2Cypher):**\n\n"
                f"Details: `{str(e)}`\n\n"
                "💡 Please rephrase your query clearly or verify the Neo4j database server status."
            )
            return {"response": fallback_msg, "sources": [], "mode": "nl2cypher", "warning": str(e)}

    else:  # graphrag
        try:
            result = execute_graphrag_query(
                query=payload.message,
                repo_name=clean_repo,
                top_k=5
            )
            return {"response": result["answer"], "sources": result.get("retrieved_nodes", []), "mode": "graphrag"}
        except Exception as e:
            fallback_msg = (
                "⚠️ **Error executing GraphRAG retrieval:**\n\n"
                f"Details: `{str(e)}`\n\n"
                "💡 Please check repository indexing status, Neo4j connectivity, and LLM configuration."
            )
            return {"response": fallback_msg, "sources": [], "mode": "graphrag", "warning": str(e)}

# 7. Graph Statistics & Visual Elements
@app.get("/api/v1/graph/stats", dependencies=[Depends(verify_api_key)])
def graph_stats():
    try:
        return get_graph_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/graph/elements", dependencies=[Depends(verify_api_key)])
def graph_elements(repo_name: Optional[str] = None, limit: Optional[int] = 1000):
    clean_limit = min(max(limit or 1000, 10), 2000)
    clean_repo = sanitize_name(repo_name) if repo_name else None
    try:
        return get_graph_elements(repo_name=clean_repo, limit=clean_limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/graph/node/{node_id:path}", dependencies=[Depends(verify_api_key)])
def graph_node_details(node_id: str):
    """Returns detailed information, source code, and connections for a specific graph node."""
    try:
        details = get_node_details(node_id)
        if not details:
            raise HTTPException(status_code=404, detail="Node not found")
        return details
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/vector-db/stats", dependencies=[Depends(verify_api_key)])
def vector_db_stats(repo_name: Optional[str] = None):
    """Returns vector database statistics, dimensions, and model configuration."""
    clean_repo = sanitize_name(repo_name) if repo_name else None
    try:
        return get_vector_database_stats(clean_repo)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/vector-db/records", dependencies=[Depends(verify_api_key)])
def vector_db_records(repo_name: Optional[str] = None, search: Optional[str] = None, limit: Optional[int] = 100):
    """Returns indexed vector records for a repository."""
    clean_repo = sanitize_name(repo_name) if repo_name else None
    clean_limit = min(max(limit or 50, 1), 500)
    try:
        return get_vector_database_records(clean_repo, search=search, limit=clean_limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/vector-db/vector/{node_id:path}", dependencies=[Depends(verify_api_key)])
def vector_db_node_vector(node_id: str):
    """Returns full float array vector for a specific entity."""
    try:
        data = get_node_vector(node_id)
        if not data:
            raise HTTPException(status_code=404, detail="Vector embedding not found for this node")
        return data
    except HTTPException:
        raise
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

@app.post("/api/v1/repos/{repo_name}/reindex", dependencies=[Depends(verify_api_key)])
def reindex_repository(repo_name: str):
    """Reindexes an existing repository on disk using the active embedding model and AST parser."""
    clean_repo = sanitize_name(repo_name)
    repo_dir = ALLOWED_REPO_DIR / clean_repo
    if not repo_dir.exists() or not repo_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Repository '{clean_repo}' not found on disk at {repo_dir}."
        )

    try:
        task = async_index_repository.delay(str(repo_dir), clean_repo)
        return {
            "status": "queued",
            "task_id": task.id,
            "repo_name": clean_repo,
            "message": f"Re-indexing queued for repository '{clean_repo}'."
        }
    except Exception as e:
        # Fallback to synchronous indexing if Celery worker is unavailable
        try:
            result = scan_and_ingest_directory(str(repo_dir), clean_repo)
            return {
                "status": "success",
                "repo_name": clean_repo,
                "result": result,
                "message": f"Repository '{clean_repo}' reindexed successfully."
            }
        except Exception as sync_e:
            raise HTTPException(status_code=500, detail=f"Reindexing failed: {str(sync_e)}")

# 9. Architectural Code Smells & Blast Radius Analytics
@app.get("/api/v1/analytics/dead-code", dependencies=[Depends(verify_api_key)])
def get_dead_code(repo_name: Optional[str] = None):
    return analytics.detect_dead_code_candidates(repo_name=repo_name)

@app.get("/api/v1/analytics/clones", dependencies=[Depends(verify_api_key)])
def get_code_clones(repo_name: Optional[str] = None):
    return analytics.detect_code_clones(repo_name=repo_name)

@app.get("/api/v1/analytics/hotspots", dependencies=[Depends(verify_api_key)])
def get_hotspots(repo_name: Optional[str] = None):
    return analytics.get_git_hotspots(repo_name=repo_name)

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
# 10. Interactive Skills & Commands API
@app.get("/api/v1/skills", dependencies=[Depends(verify_api_key)])
def list_skills_endpoint():
    """Returns available code reading and reporting skills for interactive Copilot execution."""
    from app.core.skills import get_available_skills_metadata
    return {"skills": get_available_skills_metadata()}

@app.post("/api/v1/skills/execute", dependencies=[Depends(verify_api_key)])
def execute_skill_endpoint(payload: ExecuteSkillRequest):
    """Executes a specific code reading or reporting skill dynamically."""
    from app.core.skills import dispatch_skill_or_command
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    result = dispatch_skill_or_command(payload.skill_name, repo_name=clean_repo, params=payload.params)
    if not result:
        raise HTTPException(status_code=404, detail=f"Skill '{payload.skill_name}' not found.")
    return result

@app.post("/api/v1/assets/generate", dependencies=[Depends(verify_api_key)])
def generate_asset_endpoint(payload: GenerateAssetRequest):
    """
    Generates tailored, high-grade project deliverables (README.md or .env.example)
    specifically for the inspected target repository.
    """
    from app.core.assets import generate_project_asset
    from app.core.llm import verify_llm_connection
    is_conn, reason = verify_llm_connection()
    if not is_conn:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "LLM_NOT_CONNECTED",
                "message": reason
            }
        )
    return generate_project_asset(repo_name=payload.repo_name, asset_type=payload.asset_type)

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

class PreflightReportRequest(BaseModel):
    repo_name: Optional[str] = Field(None, max_length=100)
    custom_code: Optional[str] = Field(None, max_length=500_000)
    analytics_data: Optional[Dict[str, Any]] = None

@app.post("/api/v1/review/preflight-report", dependencies=[Depends(verify_api_key)])
def preflight_report_endpoint(payload: PreflightReportRequest):
    """
    Executes 3-Tier Preflight LLM code evaluation (Red, Orange, Yellow) with strict JSON output,
    combines with Knowledge Graph architectural smells, and produces a 100% pure English Markdown report.
    """
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    try:
        eval_results = run_3tier_preflight_evaluation(
            repo_name=clean_repo,
            custom_code=payload.custom_code
        )
        analytics = payload.analytics_data
        if not analytics:
            try:
                from app.core.analytics import detect_code_smells
                analytics = detect_code_smells(clean_repo)
            except Exception:
                analytics = {}
        markdown_content = compile_pure_english_markdown_report(eval_results, analytics)
        return {
            "status": "success",
            "repository": clean_repo or "All Repositories",
            "evaluations": eval_results,
            "markdown_report": markdown_content
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preflight evaluation failed: {str(e)}")

@app.get("/api/v1/review/preflight-status", dependencies=[Depends(verify_api_key)])
def get_preflight_status(repo_name: Optional[str] = None):
    """Returns 3-Tier Preflight evaluation status (Red, Orange, Yellow) with rules and repair prompts."""
    clean_repo = sanitize_name(repo_name) if repo_name else None
    eval_results = run_3tier_preflight_evaluation(repo_name=clean_repo)
    return eval_results

@app.get("/api/v1/review/preflight-report-download", dependencies=[Depends(verify_api_key)])
def download_preflight_report(repo_name: Optional[str] = None):
    """Directly compiles and returns the full Architecture Health and 3-Tier Preflight Markdown report."""
    clean_repo = sanitize_name(repo_name) if repo_name else None
    eval_results = run_3tier_preflight_evaluation(repo_name=clean_repo)
    try:
        from app.core.analytics import detect_code_smells
        analytics = detect_code_smells(clean_repo)
    except Exception:
        analytics = {}
    markdown_content = compile_pure_english_markdown_report(eval_results, analytics)
    return {
        "status": "success",
        "repository": clean_repo or "All Repositories",
        "markdown_report": markdown_content
    }

@app.get("/api/v1/review/tier/{tier_name}", dependencies=[Depends(verify_api_key)])
def audit_single_tier_get(tier_name: str, repo_name: Optional[str] = None):
    """Executes an on-demand, deep project scan for defects in the specified tier (red, orange, or yellow)."""
    from app.core.llm import verify_llm_connection
    is_connected, reason = verify_llm_connection()
    if not is_connected:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "LLM_NOT_CONNECTED",
                "message": reason
            }
        )
    clean_repo = sanitize_name(repo_name) if repo_name else None
    return audit_single_tier(tier=tier_name, repo_name=clean_repo)

@app.post("/api/v1/review/tier/{tier_name}", dependencies=[Depends(verify_api_key)])
def audit_single_tier_post(tier_name: str, payload: Optional[AutoDocRequest] = None):
    """Executes an on-demand, deep project scan for defects in the specified tier (red, orange, or yellow)."""
    from app.core.llm import verify_llm_connection
    is_connected, reason = verify_llm_connection()
    if not is_connected:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "LLM_NOT_CONNECTED",
                "message": reason
            }
        )
    repo = payload.repo_name if payload else None
    clean_repo = sanitize_name(repo) if repo else None
    return audit_single_tier(tier=tier_name, repo_name=clean_repo)


class PreflightFixBugRequest(BaseModel):
    repo_name: Optional[str] = Field(None, max_length=100)
    file_path: str = Field(..., max_length=500)
    repair_prompt: str = Field(..., max_length=10_000)
    rule: Optional[str] = Field(None, max_length=200)
    detail: Optional[str] = Field(None, max_length=1000)
    business_consequence: Optional[str] = Field(None, max_length=1000)
    snippet: Optional[str] = Field(None, max_length=5000)
    line_number: Optional[int] = Field(None, ge=1)


@app.post("/api/v1/review/fix-bug", dependencies=[Depends(verify_api_key)])
def preflight_fix_bug_endpoint(payload: PreflightFixBugRequest):
    """
    Autonomously fixes a detected Preflight defect using the generated AI repair prompt,
    updates the source file on disk, and saves the changes.
    """
    from app.core.llm import verify_llm_connection
    is_connected, reason = verify_llm_connection()
    if not is_connected and os.getenv("TESTING") != "1":
        raise HTTPException(
            status_code=503,
            detail={
                "error": "LLM_NOT_CONNECTED",
                "message": reason
            }
        )
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    return apply_preflight_bug_fix(
        repo_name=clean_repo,
        file_path=payload.file_path,
        repair_prompt=payload.repair_prompt,
        rule=payload.rule,
        detail=payload.detail,
        business_consequence=payload.business_consequence,
        snippet=payload.snippet,
        line_number=payload.line_number
    )


# --- ISOLATED SANDBOX EXECUTION ENGINE ---
class SandboxStartRequest(BaseModel):
    repo_name: Optional[str] = Field(None, max_length=100)
    command: Optional[str] = Field(None, max_length=500)
    port: Optional[int] = Field(None, ge=1024, le=65535)

class SandboxStopRequest(BaseModel):
    repo_name: Optional[str] = Field(None, max_length=100)


@app.get("/api/v1/sandbox/plan", dependencies=[Depends(verify_api_key)])
def get_sandbox_launch_plan_endpoint(repo_name: Optional[str] = None):
    """Deeply inspects the repository and README to discover how to launch the project."""
    from app.core.sandbox import discover_project_launch_plan
    clean_repo = sanitize_name(repo_name) if repo_name else None
    return discover_project_launch_plan(clean_repo)


@app.post("/api/v1/sandbox/start", dependencies=[Depends(verify_api_key)])
def start_sandbox_endpoint(payload: SandboxStartRequest):
    """Launches the project in an isolated execution sandbox."""
    from app.core.sandbox import start_sandbox_session
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    return start_sandbox_session(
        repo_name=clean_repo,
        custom_command=payload.command,
        custom_port=payload.port
    )


@app.post("/api/v1/sandbox/stop", dependencies=[Depends(verify_api_key)])
def stop_sandbox_endpoint(payload: SandboxStopRequest):
    """Terminates the running isolated sandbox session."""
    from app.core.sandbox import stop_sandbox_session
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    return stop_sandbox_session(repo_name=clean_repo)


@app.get("/api/v1/sandbox/status", dependencies=[Depends(verify_api_key)])
def get_sandbox_status_endpoint(repo_name: Optional[str] = None):
    """Retrieves live status, port, uptime, and terminal output of the sandbox."""
    from app.core.sandbox import get_sandbox_session_status
    clean_repo = sanitize_name(repo_name) if repo_name else None
    return get_sandbox_session_status(repo_name=clean_repo)


@app.api_route("/api/v1/sandbox/proxy/{repo_name}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@app.api_route("/api/v1/sandbox/proxy/{repo_name}/{subpath:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def sandbox_proxy_endpoint(repo_name: str, subpath: str = "", request: Request = None):
    """Reverse proxies browser HTTP traffic to the isolated sandbox process inside the container."""
    from app.core.sandbox import proxy_sandbox_http_request
    return await proxy_sandbox_http_request(repo_name, subpath, request)


# 11. GitHub CI/CD Webhook Trigger
@app.post("/api/v1/webhooks/github")
async def github_webhook(request: Request):
    """
    Automated Webhook: Triggers background clone and ingestion when code is pushed to GitHub.

    Security (D1 fix): the request is only processed if its HMAC-SHA256 signature
    (`X-Hub-Signature-256`) verifies against the raw body using WEBHOOK_SECRET. This
    stops arbitrary callers from triggering unauthenticated `git clone` on any URL
    (SSRF / resource abuse / unauthorized ingestion).

    Configure the secret to match the "Webhook > Secret" value in the GitHub repo.
    For development only, set WEBHOOK_SECRET=dev-insecure-webhook to disable signing.
    """
    signature = request.headers.get("X-Hub-Signature-256", "")
    event = request.headers.get("X-GitHub-Event", "")
    raw_body = await request.body()

    secret = os.getenv("WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(
            status_code=401,
            detail="WEBHOOK_SECRET is not configured. Set the environment variable and the matching 'Webhook > Secret' in GitHub.",
        )

    # Explicit, well-understood insecure development default (never used in production).
    if secret == "dev-insecure-webhook":
        if event != "push":
            return {"status": "ignored", "message": "Only 'push' events are processed."}
        # fall through to processing below
    else:
        if not signature.startswith("sha256="):
            raise HTTPException(status_code=401, detail="Missing or malformed X-Hub-Signature-256 header.")
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Webhook signature verification failed.")

    try:
        body = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    repo_info = body.get("repository", {})
    clone_url = repo_info.get("clone_url")
    raw_repo_name = repo_info.get("name")
    ref = body.get("ref", "")
    branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else None

    if not clone_url or not raw_repo_name:
        return {"status": "ignored", "message": "Not a push event or missing repository info."}

    # Only allow HTTP(S) clone URLs (never file:// or ssh).
    if not (clone_url.startswith("https://") or clone_url.startswith("http://")):
        raise HTTPException(status_code=400, detail="clone_url must be an http(s) URL.")

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
from app.core.llm import get_llm_config, set_llm_config, execute_llm_http
from app.tasks.autodoc import generate_missing_docs_task

@app.get("/api/v1/config/llm", dependencies=[Depends(verify_api_key)])
def get_llm_settings():
    return get_llm_config()

_LLM_STATUS_CACHE = {"timestamp": 0, "result": None}

def clear_llm_status_cache():
    global _LLM_STATUS_CACHE
    _LLM_STATUS_CACHE = {"timestamp": 0, "result": None}

@app.post("/api/v1/config/llm", dependencies=[Depends(verify_api_key)])
def update_llm_settings(payload: LLMConfigRequest):
    current = get_llm_config()
    base_url = (payload.base_url or "").strip() or current.get("base_url") or "https://api.openai.com/v1"
    model = (payload.model or "").strip() or current.get("model") or "gpt-4o-mini"
    api_key = payload.api_key if payload.api_key is not None else current.get("api_key", "")
    try:
        set_llm_config(base_url, model, api_key)
        clear_llm_status_cache()
        if getattr(payload, "test_connection", False):
            from app.core.llm import verify_llm_connection
            is_connected, reason = verify_llm_connection(model=model)
            return {
                "status": "success" if is_connected else "failed",
                "connected": is_connected,
                "message": reason
            }
        return {"status": "success", "message": "LLM configuration updated successfully."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/config/llm/test", dependencies=[Depends(verify_api_key)])
def test_llm_connection_get():
    from app.core.llm import verify_llm_connection
    is_connected, reason = verify_llm_connection()
    return {
        "status": "success" if is_connected else "failed",
        "connected": is_connected,
        "message": reason
    }

# --- DEDICATED EMBEDDING CONFIGURATION & ON-DEMAND ENGINE ---
class EmbeddingConfigRequest(BaseModel):
    vllm_host: Optional[str] = Field(None, description="API Base URL (e.g. https://api.openai.com/v1)")
    api_url: Optional[str] = Field(None, description="API Base URL (e.g. https://api.openai.com/v1)")
    api_key: Optional[str] = Field(None, description="Provider API key")
    model: Optional[str] = Field("text-embedding-3-small", description="Embedding model name")
    dimension: Optional[int] = Field(1536, description="Vector embedding dimension (default 1536)")
    allow_fallback: Optional[bool] = Field(True, description="Whether to allow deterministic fallback vectors")

class EmbeddingTestRequest(BaseModel):
    vllm_host: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None

class EmbeddingReembedRequest(BaseModel):
    repo_name: Optional[str] = None
    force: Optional[bool] = False

@app.get("/api/v1/config/embedding", dependencies=[Depends(verify_api_key)])
def get_embedding_settings():
    """Returns dedicated embedding settings and live status."""
    from app.core.embedder import get_embedding_config, verify_vllm_status
    cfg = get_embedding_config()
    status = verify_vllm_status()
    return {
        "config": cfg,
        "status": status
    }

@app.post("/api/v1/config/embedding", dependencies=[Depends(verify_api_key)])
def update_embedding_settings(payload: EmbeddingConfigRequest):
    """Updates API URL and embedding model."""
    from app.core.embedder import set_embedding_config, test_vllm_embedding_connection
    target_url = payload.api_url or payload.vllm_host
    set_embedding_config(
        vllm_host=target_url,
        api_url=target_url,
        model=payload.model,
        api_key=payload.api_key,
        dimension=payload.dimension,
        allow_fallback=payload.allow_fallback
    )
    test_res = test_vllm_embedding_connection(target_url, payload.model, api_key=payload.api_key)
    return {
        "status": "success",
        "message": "Embedding configuration saved successfully.",
        "test_result": test_res
    }

@app.post("/api/v1/embedding/test", dependencies=[Depends(verify_api_key)])
def test_embedding_endpoint(payload: EmbeddingTestRequest):
    """Tests connectivity to a specified API endpoint and embedding model without saving."""
    from app.core.embedder import test_vllm_embedding_connection
    target_url = payload.api_url or payload.vllm_host
    try:
        return test_vllm_embedding_connection(target_url, payload.model, api_key=payload.api_key)
    except Exception as e:
        return {
            "status": "error",
            "host": target_url,
            "target_model": payload.model,
            "message": f"Connection test failed: {str(e)}"
        }

@app.post("/api/v1/embedding/reembed", dependencies=[Depends(verify_api_key)])
def reembed_codebase_endpoint(payload: EmbeddingReembedRequest):
    """Generates / updates embeddings for all entities in the graph on-demand (at any stage)."""
    from app.core.embedder import reembed_graph_entities
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    return reembed_graph_entities(repo_name=clean_repo, force=payload.force or False)

@app.get("/api/v1/embedding/status", dependencies=[Depends(verify_api_key)])
def get_embedding_status_endpoint():
    """Returns real-time status of the dedicated vLLM embedding engine."""
    from app.core.embedder import verify_vllm_status
    return verify_vllm_status()


class AutoDocRequest(BaseModel):
    repo_name: Optional[str] = None

@app.post("/api/v1/tasks/autodoc", dependencies=[Depends(verify_api_key)])
def trigger_auto_doc(payload: AutoDocRequest):
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    task = generate_missing_docs_task.delay(repo_name=clean_repo)
    return {
        "status": "queued",
        "task_id": task.id,
        "message": "Auto-Documentation engine started in background worker."
    }

class StopTaskRequest(BaseModel):
    task_id: Optional[str] = None
    repo_name: Optional[str] = None

@app.post("/api/v1/tasks/autodoc/stop", dependencies=[Depends(verify_api_key)])
def stop_autodoc_endpoint(payload: StopTaskRequest):
    """Halts an in-flight Auto-Doc Celery task gracefully and cancels further executions."""
    from app.tasks.autodoc import request_stop_autodoc
    clean_repo = sanitize_name(payload.repo_name) if payload.repo_name else None
    request_stop_autodoc(task_id=payload.task_id, repo_name=clean_repo)
    if payload.task_id:
        try:
            celery_app.control.revoke(payload.task_id, terminate=True, signal="SIGTERM")
        except Exception:
            pass
    return {
        "status": "stopped",
        "task_id": payload.task_id,
        "repo_name": clean_repo,
        "message": "Stop signal broadcasted to Auto-Doc workers."
    }

@app.get("/api/v1/analytics/documentation-coverage", dependencies=[Depends(verify_api_key)])
def get_documentation_coverage_endpoint(repo_name: Optional[str] = None):
    """Returns documentation coverage percentage and breakdown for codebase."""
    clean_repo = sanitize_name(repo_name) if repo_name else None
    from app.core.analytics import get_documentation_coverage_stats
    return get_documentation_coverage_stats(clean_repo)

@app.get("/api/v1/analytics/full-code-docs", dependencies=[Depends(verify_api_key)])
def get_full_code_and_docs_endpoint(repo_name: Optional[str] = None):
    """Returns the complete codebase source code and documentation dossier."""
    clean_repo = sanitize_name(repo_name) if repo_name else None
    from app.core.analytics import get_full_project_code_and_docs_data
    return get_full_project_code_and_docs_data(clean_repo)

@app.get("/api/v1/llm/status", dependencies=[Depends(verify_api_key)])
def check_llm_status():
    """
    Checks the status of the configured LLM endpoint and retrieves available models.
    Caches the result for 15 seconds to keep the UI snappy and prevent blocking.
    """
    import time
    global _LLM_STATUS_CACHE
    now = time.time()
    if _LLM_STATUS_CACHE.get("result") and (now - _LLM_STATUS_CACHE.get("timestamp", 0)) < 15:
        return _LLM_STATUS_CACHE["result"]

    from app.core.llm import get_llm_config, normalize_llm_base_url
    import requests
    config = get_llm_config()
    base_url = config.get("base_url", "")
    target_model = config.get("model", "")
    
    status_info = {
        "status": "unknown",
        "base_url": base_url,
        "model": target_model,
        "available_models": [],
        "message": ""
    }
    
    if not base_url:
        status_info["status"] = "not_configured"
        status_info["message"] = "LLM base URL is not configured."
        _LLM_STATUS_CACHE = {"timestamp": now, "result": status_info}
        return status_info

    try:
        norm_base = normalize_llm_base_url(base_url)
        models = []
        headers = {}
        if config.get("api_key"):
            headers["Authorization"] = f"Bearer {config['api_key']}"

        is_connected = False
        try:
            resp = execute_llm_http("get", f"{norm_base}/models", headers=headers, timeout=2.5)
            if resp.ok:
                is_connected = True
                data = resp.json()
                models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        except Exception:
            pass
            
        if not models:
            try:
                root_url = base_url.replace("/v1", "").rstrip("/")
                resp2 = execute_llm_http("get", f"{root_url}/api/tags", timeout=2.5)
                if resp2.ok:
                    is_connected = True
                    data2 = resp2.json()
                    models = [m.get("name") for m in data2.get("models", []) if m.get("name")]
            except Exception:
                pass

        status_info["available_models"] = models
        if is_connected or models:
            status_info["status"] = "connected"
            status_info["message"] = f"Connected to {base_url}. Active Model: {target_model}"
        else:
            status_info["status"] = "offline"
            status_info["message"] = (
                f"Could not connect to {base_url}. "
                "If running inside Docker on Windows/Mac, make sure port 8007 is mapped on the host or use your host LAN IP."
            )
    except Exception as e:
        status_info["status"] = "error"
        status_info["message"] = str(e)

    _LLM_STATUS_CACHE = {"timestamp": now, "result": status_info}
    return status_info


# ==================== MCP AGENT HUB & INTERACTIVE RUNNER ====================
class McpToolExecuteRequest(BaseModel):
    tool_name: str
    arguments: Optional[Dict[str, Any]] = None


@app.get("/api/v1/mcp/tools", tags=["MCP"])
async def list_mcp_tools():
    """Returns all available MCP tools with their argument schemas, types, and descriptions."""
    try:
        from app.mcp_server import get_mcp_tools_metadata
        return {"tools": get_mcp_tools_metadata()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load MCP tools: {str(e)}")


@app.post("/api/v1/mcp/execute", tags=["MCP"])
async def run_mcp_tool_endpoint(payload: McpToolExecuteRequest):
    """Executes any registered MCP tool on-demand with validated arguments."""
    try:
        from app.mcp_server import execute_mcp_tool
        res = execute_mcp_tool(payload.tool_name, payload.arguments or {})
        return {"status": "success", **res}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MCP tool execution failed: {str(e)}")




