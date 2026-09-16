from pathlib import Path
from app.core.celery import celery_app
from app.core.scanner import scan_and_ingest_directory
from app.core.cloner import clone_git_repo, extract_safe_zip

@celery_app.task(bind=True)
def async_index_repository(self, repo_path: str, repo_name: str = None):
    """Celery background task to index an existing repository folder."""
    self.update_state(state="PROGRESS", meta={"status": "Starting repository scan..."})
    try:
        result = scan_and_ingest_directory(repo_path, repo_name)
        return result
    except Exception as e:
        self.update_state(state="FAILURE", meta={"exc": str(e)})
        raise e

@celery_app.task(bind=True)
def async_clone_and_index(self, git_url: str, repo_name: str = None, branch: str = None, token: str = None):
    """
    Automated pipeline:
    1. Clones repository from Git URL.
    2. Parses AST nodes using Tree-sitter.
    3. Builds vector embeddings and Knowledge Graph in Neo4j.
    """
    self.update_state(
        state="PROGRESS",
        meta={"status": "Cloning repository from Git...", "percent": 5, "current": 0, "total": 0, "stage": "cloning"}
    )
    try:
        cloned_path = clone_git_repo(git_url=git_url, repo_name=repo_name, branch=branch, token=token)
        actual_name = repo_name or cloned_path.name

        self.update_state(
            state="PROGRESS",
            meta={"status": f"Cloned repository '{actual_name}'. Scanning files...", "percent": 10, "current": 0, "total": 0, "stage": "scanning"}
        )

        def on_progress(current: int, total: int, current_file: str):
            pct = 10 + int((current / total) * 88) if total > 0 else 10
            self.update_state(
                state="PROGRESS",
                meta={
                    "status": f"Indexing {current}/{total} files ({pct}%)",
                    "percent": pct,
                    "current": current,
                    "total": total,
                    "current_file": current_file,
                    "stage": "indexing",
                    "repo_name": actual_name
                }
            )

        result = scan_and_ingest_directory(str(cloned_path), actual_name, progress_callback=on_progress)
        return result
    except Exception as e:
        self.update_state(state="FAILURE", meta={"exc": str(e), "error": str(e)})
        raise e

@celery_app.task(bind=True)
def async_extract_and_index(self, zip_path_str: str, repo_name: str):
    """
    Automated pipeline for uploaded ZIP archives:
    1. Safely extracts ZIP inside sandbox.
    2. Ingests all source files into the graph.
    """
    self.update_state(
        state="PROGRESS",
        meta={"status": "Extracting archive safely...", "percent": 5, "current": 0, "total": 0, "stage": "extracting"}
    )
    try:
        zip_path = Path(zip_path_str)
        extracted_dir = extract_safe_zip(zip_path, repo_name)

        self.update_state(
            state="PROGRESS",
            meta={"status": f"Extracted '{repo_name}'. Scanning files...", "percent": 10, "current": 0, "total": 0, "stage": "scanning"}
        )

        def on_progress(current: int, total: int, current_file: str):
            pct = 10 + int((current / total) * 88) if total > 0 else 10
            self.update_state(
                state="PROGRESS",
                meta={
                    "status": f"Indexing {current}/{total} files ({pct}%)",
                    "percent": pct,
                    "current": current,
                    "total": total,
                    "current_file": current_file,
                    "stage": "indexing",
                    "repo_name": repo_name
                }
            )

        result = scan_and_ingest_directory(str(extracted_dir), repo_name, progress_callback=on_progress)

        # Cleanup temporary zip file
        if zip_path.exists():
            zip_path.unlink()

        return result
    except Exception as e:
        self.update_state(state="FAILURE", meta={"exc": str(e), "error": str(e)})
        raise e
