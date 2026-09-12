import os
import shutil
import tempfile
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Security, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional

from app.core.parser import parse_python_file
from app.core.graph import (
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
from app.core.celery import celery_app
from app.core.security import (
    verify_api_key,
    validate_safe_path,
    sanitize_name,
    ALLOWED_REPO_DIR
)
from app.core.cloner import clone_git_repo, extract_safe_zip

app = FastAPI(
    title="Enterprise Code Knowledge Graph & GraphRAG API",
    description="Automated Git cloning, ZIP extraction, AST ingestion, vector embeddings, and GraphRAG retrieval.",
    version="1.2.0"
)

# Configure CORS securely from environment
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
allowed_origins = [orig.strip() for orig in allowed_origins_env.split(",") if orig.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins else ["*"],
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

@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "Enterprise Code GraphRAG API (Automated Ingestion)",
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
        parsed_data = parse_python_file(payload.file_path, code_bytes)
        ingest_parsed_data(clean_repo, parsed_data)

        return {
            "status": "success",
            "repo_name": clean_repo,
            "file_path": payload.file_path,
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
