from pathlib import Path
from app.core.celery import celery_app
from app.core.scanner import scan_and_ingest_directory
from app.core.cloner import clone_git_repo, extract_safe_zip

@celery_app.task(bind=True)
def async_index_repository(self, repo_path: str, repo_name: str = None):
    """Celery background task to index an existing repository folder."""
    actual_name = repo_name or Path(repo_path).name
    print(f"[Celery Ingestion] Starting repository indexing for '{actual_name}' at {repo_path} ...", flush=True)
    self.update_state(state="PROGRESS", meta={"status": "Starting repository scan...", "percent": 5, "stage": "scanning", "repo_name": actual_name})
    try:
        def on_progress(current: int, total: int, current_file: str):
            pct = int((current / total) * 100) if total > 0 else 0
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
            if current == 1 or current % 5 == 0 or current == total:
                print(f"[Celery Ingestion] [{actual_name}] {current}/{total} files ({pct}%): {current_file}", flush=True)

        result = scan_and_ingest_directory(repo_path, actual_name, progress_callback=on_progress)
        print(f"[Celery Ingestion] [{actual_name}] Indexing completed: {result.get('parsed_files', 0)} files parsed, {result.get('classes_indexed', 0)} classes, {result.get('functions_indexed', 0)} functions.", flush=True)
        return result
    except Exception as e:
        print(f"[Celery Ingestion Error] async_index_repository failed: {e}", flush=True)
        raise e

@celery_app.task(bind=True)
def async_clone_and_index(self, git_url: str, repo_name: str = None, branch: str = None, token: str = None, full_history: bool = True):
    """
    Automated pipeline:
    1. Clones repository from Git URL.
    2. Parses AST nodes using Tree-sitter.
    3. Builds vector embeddings and Knowledge Graph in Neo4j.
    """
    print(f"[Celery Ingestion] Starting clone & index pipeline for {git_url} ...", flush=True)
    self.update_state(
        state="PROGRESS",
        meta={"status": "Cloning repository from Git...", "percent": 5, "current": 0, "total": 0, "stage": "cloning"}
    )
    try:
        cloned_path = clone_git_repo(git_url=git_url, repo_name=repo_name, branch=branch, token=token, full_history=full_history)
        actual_name = repo_name or cloned_path.name
        print(f"[Celery Ingestion] Cloned '{actual_name}' into {cloned_path}. Scanning files...", flush=True)

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
            if current == 1 or current % 5 == 0 or current == total:
                print(f"[Celery Ingestion] [{actual_name}] {current}/{total} files ({pct}%): {current_file}", flush=True)

        result = scan_and_ingest_directory(str(cloned_path), actual_name, progress_callback=on_progress)
        print(f"[Celery Ingestion] [{actual_name}] Indexing completed: {result.get('parsed_files', 0)} files parsed, {result.get('classes_indexed', 0)} classes, {result.get('functions_indexed', 0)} functions.", flush=True)
        return result
    except Exception as e:
        print(f"[Celery Ingestion Error] async_clone_and_index failed: {e}", flush=True)
        raise e

@celery_app.task(bind=True)
def async_extract_and_index(self, zip_path_str: str, repo_name: str):
    """
    Automated pipeline for uploaded ZIP archives:
    1. Safely extracts ZIP inside sandbox.
    2. Ingests all source files into the graph.
    """
    print(f"[Celery Ingestion] Starting ZIP extract & index for '{repo_name}' ...", flush=True)
    self.update_state(
        state="PROGRESS",
        meta={"status": "Extracting archive safely...", "percent": 5, "current": 0, "total": 0, "stage": "extracting"}
    )
    zip_path = Path(zip_path_str)
    try:
        extracted_dir = extract_safe_zip(zip_path, repo_name)
        print(f"[Celery Ingestion] Archive extracted to {extracted_dir}. Scanning files...", flush=True)

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
            if current == 1 or current % 5 == 0 or current == total:
                print(f"[Celery Ingestion] [{repo_name}] {current}/{total} files ({pct}%): {current_file}", flush=True)

        result = scan_and_ingest_directory(str(extracted_dir), repo_name, progress_callback=on_progress)
        print(f"[Celery Ingestion] [{repo_name}] Indexing completed: {result.get('parsed_files', 0)} files parsed, {result.get('classes_indexed', 0)} classes, {result.get('functions_indexed', 0)} functions.", flush=True)

        # Cleanup temporary zip file
        if zip_path.exists():
            zip_path.unlink(missing_ok=True)

        return result
    except Exception as e:
        print(f"[Celery Ingestion Error] async_extract_and_index failed: {e}", flush=True)
        if zip_path.exists():
            try:
                zip_path.unlink(missing_ok=True)
            except Exception:
                pass
        raise e
