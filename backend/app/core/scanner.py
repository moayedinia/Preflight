import os
import hashlib
from pathlib import Path
from app.core.parser import parse_source_file, get_supported_extensions
from app.core.graph import (
    ingest_parsed_data,
    get_repo_file_hashes,
    prune_deleted_files,
)
from app.core.git_history import get_git_history_map

IGNORE_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "env", "node_modules",
    ".idea", ".vscode", "dist", "build", "egg-info", "target", "vendor",
    "bin", "obj", ".gradle", ".mvn"
}

SUPPORTED_EXTENSIONS = get_supported_extensions()


def scan_and_ingest_directory(repo_path: str, repo_name: str = None, progress_callback=None) -> dict:
    """
    Crawls a local repository directory, parses supported source files and indexes
    them into the Neo4j Code Knowledge Graph.

    D6b — git history (author + churn) is gathered with a SINGLE `git log` pass
    (get_git_history_map) instead of two subprocess spawns per file.
    D8  — incremental indexing: files whose content hash already matches the graph
    are skipped (no re-parse / re-embed), and files deleted from disk since the
    last ingest are pruned so the graph does not drift. On a first-ever index this
    is a no-op (no existing hashes, nothing to prune).

    Reports real-time progress via progress_callback(current, total, current_file).
    """
    path = Path(repo_path)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Directory {repo_path} does not exist or is not a directory.")

    if not repo_name:
        repo_name = path.name

    # Pre-collect all matching files to calculate precise progress
    supported_files = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
        for file in files:
            ext = Path(file).suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                full_path = Path(root) / file
                rel_path = full_path.relative_to(path).as_posix()
                supported_files.append((full_path, rel_path))

    total_files = len(supported_files)

    # D6b: single-pass git history for the whole repo (author/email/churn per file).
    history = get_git_history_map(str(path))

    # D8: hashes of files already indexed for this repo ({} on a first index).
    try:
        existing_hashes = get_repo_file_hashes(repo_name)
    except Exception as e:
        print(f"[Scanner] Could not load existing hashes for incremental indexing: {e}", flush=True)
        existing_hashes = {}

    kept_paths = []
    parsed_files = 0
    skipped_files = 0
    total_classes = 0
    total_functions = 0
    languages_found = set()
    errors = []

    for idx, (full_path, rel_path) in enumerate(supported_files):
        if progress_callback:
            try:
                progress_callback(idx + 1, total_files, rel_path)
            except Exception as cb_err:
                print(f"[Scanner] progress_callback error: {cb_err}")

        try:
            with open(full_path, "rb") as f:
                code_bytes = f.read()
            content_hash = hashlib.sha256(code_bytes).hexdigest()

            # D8: skip files unchanged since the last index (avoids re-parse + re-embed).
            if existing_hashes.get(rel_path) == content_hash:
                kept_paths.append(rel_path)
                skipped_files += 1
                continue

            parsed = parse_source_file(rel_path, code_bytes)

            # D6b: attach per-file git history (author, email, churn).
            hist = history.get(rel_path, {})
            parsed["author"] = hist.get("author", "Unknown")
            parsed["author_email"] = hist.get("email", "unknown@example.com")
            parsed["churn"] = hist.get("churn", 0)
            parsed["content_hash"] = content_hash

            lang = parsed.get("language", "unknown")
            languages_found.add(lang)

            ingest_parsed_data(repo_name, parsed)
            kept_paths.append(rel_path)

            parsed_files += 1
            total_classes += len(parsed.get("classes", []))
            total_functions += len(parsed.get("functions", []))
        except Exception as e:
            print(f"[Scanner Warning] Error processing {rel_path}: {e}", flush=True)
            errors.append({"file": rel_path, "error": str(e)})

    # D8: remove graph nodes for files that no longer exist on disk.
    pruned_files = 0
    try:
        pruned_files = prune_deleted_files(repo_name, kept_paths)
    except Exception as e:
        print(f"[Scanner Warning] prune_deleted_files error: {e}", flush=True)

    # Resolve function calls and cross-entity relationships across the entire repository
    try:
        from app.core.graph import resolve_cross_references
        resolve_cross_references(repo_name)
    except Exception as e:
        print(f"[Scanner Warning] resolve_cross_references error: {e}", flush=True)

    return {
        "repo_name": repo_name,
        "repo_path": str(path),
        "languages": list(languages_found),
        "total_files_scanned": total_files,
        "parsed_files": parsed_files,
        "skipped_unchanged": skipped_files,
        "classes_indexed": total_classes,
        "functions_indexed": total_functions,
        "pruned_stale_files": pruned_files,
        "errors": errors
    }
