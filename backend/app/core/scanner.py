import os
import subprocess
from pathlib import Path
from app.core.parser import parse_source_file, get_supported_extensions
from app.core.graph import ingest_parsed_data

def get_git_author_and_churn(repo_path: str, file_path: str) -> tuple[str, str, int]:
    """Retrieves the last author and the total number of commits (churn) of a file using git."""
    author = "Unknown"
    email = "unknown@example.com"
    churn = 0
    try:
        # Get author
        res_author = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-c", "i18n.logoutputencoding=utf-8", "log", "-1", "--format=%an|%ae", "--", file_path],
            cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if res_author.stdout.strip():
            parts = res_author.stdout.strip().split("|")
            if len(parts) >= 2:
                author, email = parts[0], parts[1]
        
        # Get churn (commit count)
        res_churn = subprocess.run(
            ["git", "-c", "core.quotepath=false", "rev-list", "--count", "HEAD", "--", file_path],
            cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if res_churn.stdout.strip():
            churn = int(res_churn.stdout.strip())
    except Exception:
        pass
    return author, email, churn

IGNORE_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "env", "node_modules",
    ".idea", ".vscode", "dist", "build", "egg-info", "target", "vendor",
    "bin", "obj", ".gradle", ".mvn"
}

SUPPORTED_EXTENSIONS = get_supported_extensions()

def scan_and_ingest_directory(repo_path: str, repo_name: str = None, progress_callback = None) -> dict:
    """
    Crawls a local repository directory, parses all supported source code files
    (Python, JS/TS, Go, Java, C/C++, C#, Rust, PHP, Ruby, Kotlin, Swift, Shell, etc.)
    and indexes them into the Neo4j Code Knowledge Graph.
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
    parsed_files = 0
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

            parsed = parse_source_file(rel_path, code_bytes)
            
            author, email, churn = get_git_author_and_churn(str(path), rel_path)
            parsed["author"] = author
            parsed["author_email"] = email
            parsed["churn"] = churn

            lang = parsed.get("language", "unknown")
            languages_found.add(lang)

            ingest_parsed_data(repo_name, parsed)

            parsed_files += 1
            total_classes += len(parsed.get("classes", []))
            total_functions += len(parsed.get("functions", []))
        except Exception as e:
            print(f"[Scanner Warning] Error processing {rel_path}: {e}", flush=True)
            errors.append({"file": rel_path, "error": str(e)})

    return {
        "repo_name": repo_name,
        "repo_path": str(path),
        "languages": list(languages_found),
        "total_files_scanned": total_files,
        "parsed_files": parsed_files,
        "classes_indexed": total_classes,
        "functions_indexed": total_functions,
        "errors": errors
    }
