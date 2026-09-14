import os
import subprocess
from pathlib import Path
from app.core.parser import parse_source_file, get_supported_extensions
from app.core.graph import ingest_parsed_data

def get_git_author(repo_path: str, file_path: str) -> tuple[str, str]:
    """Retrieves the last author of a file using git."""
    try:
        res = subprocess.run(
            ["git", "log", "-1", "--format=%an|%ae", "--", file_path],
            cwd=repo_path, capture_output=True, text=True
        )
        if res.stdout.strip():
            parts = res.stdout.strip().split("|")
            if len(parts) >= 2:
                return parts[0], parts[1]
    except Exception:
        pass
    return "Unknown", "unknown@example.com"

IGNORE_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "env", "node_modules",
    ".idea", ".vscode", "dist", "build", "egg-info", "target", "vendor",
    "bin", "obj", ".gradle", ".mvn"
}

SUPPORTED_EXTENSIONS = get_supported_extensions()

def scan_and_ingest_directory(repo_path: str, repo_name: str = None) -> dict:
    """
    Crawls a local repository directory, parses all supported source code files
    (Python, JS/TS, Go, Java, C/C++, C#, Rust, PHP, Ruby, Kotlin, Swift, Shell, etc.)
    and indexes them into the Neo4j Code Knowledge Graph.
    """
    path = Path(repo_path)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Directory {repo_path} does not exist or is not a directory.")

    if not repo_name:
        repo_name = path.name

    total_files = 0
    parsed_files = 0
    total_classes = 0
    total_functions = 0
    languages_found = set()
    errors = []

    for root, dirs, files in os.walk(path):
        # Skip ignored folders
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]

        for file in files:
            ext = Path(file).suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                total_files += 1
                full_path = Path(root) / file
                rel_path = full_path.relative_to(path).as_posix()

                try:
                    with open(full_path, "rb") as f:
                        code_bytes = f.read()

                    parsed = parse_source_file(rel_path, code_bytes)
                    
                    author, email = get_git_author(str(path), rel_path)
                    parsed["author"] = author
                    parsed["author_email"] = email

                    lang = parsed.get("language", "unknown")
                    languages_found.add(lang)

                    ingest_parsed_data(repo_name, parsed)

                    parsed_files += 1
                    total_classes += len(parsed.get("classes", []))
                    total_functions += len(parsed.get("functions", []))
                except Exception as e:
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
