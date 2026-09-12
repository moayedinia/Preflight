import os
from pathlib import Path
from app.core.parser import parse_python_file
from app.core.graph import ingest_parsed_data

IGNORE_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "env", "node_modules",
    ".idea", ".vscode", "dist", "build", "egg-info"
}

def scan_and_ingest_directory(repo_path: str, repo_name: str = None) -> dict:
    """
    Crawls a local repository directory, parses all supported source files,
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
    errors = []

    for root, dirs, files in os.walk(path):
        # Skip ignored folders
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]

        for file in files:
            if file.endswith(".py"):
                total_files += 1
                full_path = Path(root) / file
                rel_path = full_path.relative_to(path).as_posix()

                try:
                    with open(full_path, "rb") as f:
                        code_bytes = f.read()

                    parsed = parse_python_file(rel_path, code_bytes)
                    ingest_parsed_data(repo_name, parsed)

                    parsed_files += 1
                    total_classes += len(parsed.get("classes", []))
                    total_functions += len(parsed.get("functions", []))
                except Exception as e:
                    errors.append({"file": rel_path, "error": str(e)})

    return {
        "repo_name": repo_name,
        "repo_path": str(path),
        "total_files_scanned": total_files,
        "parsed_files": parsed_files,
        "classes_indexed": total_classes,
        "functions_indexed": total_functions,
        "errors": errors
    }
