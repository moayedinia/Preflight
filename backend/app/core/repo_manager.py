import os
import re
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import HTTPException
from app.core.graph import driver
from app.core.security import ALLOWED_REPO_DIR, sanitize_name

def list_all_repositories() -> List[Dict[str, Any]]:
    """
    Returns all indexed repositories with their files count, classes, functions,
    and programming languages breakdown.
    Supports both Neo4j Knowledge Graph data and on-disk fallback discovery.
    Provides standard aliases (file_count, files_count, total_files) so all UI
    and API consumers receive non-zero metrics.
    """
    repo_map: Dict[str, Dict[str, Any]] = {}

    # 1. Query Neo4j Knowledge Graph if accessible
    try:
        with driver.session() as session:
            query = """
            MATCH (r:Repository)
            OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
            OPTIONAL MATCH (f)-[:CONTAINS]->(c:Class)
            OPTIONAL MATCH (f)-[:CONTAINS]->(fn:Function)
            WITH r,
                 count(DISTINCT f) AS file_count,
                 count(DISTINCT c) AS class_count,
                 count(DISTINCT fn) AS func_count,
                 COLLECT(DISTINCT f.language) AS languages
            RETURN r.name AS name,
                   file_count,
                   class_count,
                   func_count,
                   [l in languages WHERE l IS NOT NULL AND l <> 'unknown'] AS languages
            ORDER BY name ASC
            """
            for rec in session.run(query):
                r = dict(rec)
                name = r.get("name")
                if not name:
                    continue
                f_cnt = int(r.get("file_count") or 0)
                c_cnt = int(r.get("class_count") or 0)
                fn_cnt = int(r.get("func_count") or 0)
                langs = r.get("languages", [])

                repo_map[name] = {
                    "name": name,
                    "file_count": f_cnt,
                    "files_count": f_cnt,
                    "total_files": f_cnt,
                    "class_count": c_cnt,
                    "classes_count": c_cnt,
                    "total_classes": c_cnt,
                    "func_count": fn_cnt,
                    "functions_count": fn_cnt,
                    "total_functions": fn_cnt,
                    "languages": langs
                }
    except Exception as e:
        print(f"[RepoManager Notice] Neo4j query skipped or offline: {e}", flush=True)

    # 2. Check disk repositories in ALLOWED_REPO_DIR
    try:
        from app.core.parser import EXTENSION_MAP
        from app.core.scanner import IGNORE_DIRS
        supported_exts = set(EXTENSION_MAP.keys())

        disk_dirs = []
        if ALLOWED_REPO_DIR.exists() and ALLOWED_REPO_DIR.is_dir():
            for child in ALLOWED_REPO_DIR.iterdir():
                if child.is_dir() and not child.name.startswith(".") and child.name not in IGNORE_DIRS:
                    disk_dirs.append(child)

        class_pat = re.compile(r'\b(?:class|interface|record|struct)\s+\w+', re.IGNORECASE)
        func_pat = re.compile(
            r'\b(?:def|function|async\s+def)\s+\w+\s*\(|'
            r'\b(?:public|private|protected|internal|static)?\s*(?:async\s+)?[\w<>[\]]+\s+\w+\s*\(',
            re.IGNORECASE
        )

        for d in disk_dirs:
            name = d.name
            existing = repo_map.get(name)

            # If repo is not in graph OR has 0 files in graph, calculate from disk
            if not existing or existing.get("file_count", 0) == 0:
                disk_files = 0
                disk_classes = 0
                disk_funcs = 0
                detected_langs = set()

                for root, dirs, files in os.walk(d):
                    dirs[:] = [sub for sub in dirs if sub not in IGNORE_DIRS and not sub.startswith(".")]
                    for f in files:
                        ext = Path(f).suffix.lower()
                        if ext in supported_exts:
                            disk_files += 1
                            lang = EXTENSION_MAP.get(ext)
                            if lang and lang != "unknown":
                                detected_langs.add(lang)

                            # Fast sample count for classes and functions
                            if disk_files <= 200:
                                try:
                                    txt = (Path(root) / f).read_text(encoding="utf-8", errors="ignore")
                                    disk_classes += len(class_pat.findall(txt))
                                    disk_funcs += len(func_pat.findall(txt))
                                except Exception:
                                    pass

                if disk_files > 0 or not existing:
                    repo_map[name] = {
                        "name": name,
                        "file_count": disk_files,
                        "files_count": disk_files,
                        "total_files": disk_files,
                        "class_count": disk_classes,
                        "classes_count": disk_classes,
                        "total_classes": disk_classes,
                        "func_count": disk_funcs,
                        "functions_count": disk_funcs,
                        "total_functions": disk_funcs,
                        "languages": sorted(list(detected_langs)) if detected_langs else ["codebase"]
                    }
    except Exception as disk_err:
        print(f"[RepoManager Notice] Disk scan fallback notice: {disk_err}", flush=True)

    return sorted(list(repo_map.values()), key=lambda x: x["name"])


def get_repository_summary(repo_name: str) -> Dict[str, Any]:
    """Returns detailed statistics and language distribution for a single repository."""
    clean_name = sanitize_name(repo_name)
    summary = None

    try:
        with driver.session() as session:
            query = """
            MATCH (r:Repository {name: $repo_name})
            OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
            OPTIONAL MATCH (f)-[:CONTAINS]->(c:Class)
            OPTIONAL MATCH (f)-[:CONTAINS]->(fn:Function)
            WITH r, f, c, fn
            RETURN r.name AS name,
                   count(DISTINCT f) AS total_files,
                   count(DISTINCT c) AS total_classes,
                   count(DISTINCT fn) AS total_functions,
                   COLLECT(DISTINCT f.language) AS languages
            """
            rec = session.run(query, repo_name=clean_name).single()
            if rec and rec.get("name"):
                summary = dict(rec)
    except Exception:
        summary = None

    if not summary or (summary.get("total_files", 0) == 0):
        all_repos = list_all_repositories()
        match = next((r for r in all_repos if r["name"] == clean_name), None)
        if match:
            return match
        if not summary:
            raise HTTPException(status_code=404, detail=f"Repository '{repo_name}' not found.")

    t_f = int(summary.get("total_files") or 0)
    t_c = int(summary.get("total_classes") or 0)
    t_fn = int(summary.get("total_functions") or 0)

    summary["file_count"] = t_f
    summary["files_count"] = t_f
    summary["total_files"] = t_f
    summary["class_count"] = t_c
    summary["classes_count"] = t_c
    summary["total_classes"] = t_c
    summary["func_count"] = t_fn
    summary["functions_count"] = t_fn
    summary["total_functions"] = t_fn
    return summary


def delete_repository(repo_name: str) -> Dict[str, Any]:
    """
    Completely prunes a repository and all its associated files, classes,
    functions, and relationships from the Neo4j Knowledge Graph and disk sandbox.
    """
    clean_name = sanitize_name(repo_name)

    # 1. Prune graph nodes in Neo4j
    try:
        with driver.session() as session:
            query = """
            MATCH (r:Repository {name: $repo_name})
            OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
            OPTIONAL MATCH (f)-[:CONTAINS]->(c:Class)
            OPTIONAL MATCH (f)-[:CONTAINS]->(fn:Function)
            OPTIONAL MATCH (c)-[:HAS_METHOD]->(m:Function)
            DETACH DELETE r, f, c, fn, m
            """
            session.run(query, repo_name=clean_name)
    except Exception as e:
        print(f"[RepoManager Delete Notice] Neo4j detach delete notice: {e}", flush=True)

    # 2. Delete local sandbox folder if exists
    repo_disk_dir = ALLOWED_REPO_DIR / clean_name
    if repo_disk_dir.exists():
        shutil.rmtree(repo_disk_dir, ignore_errors=True)

    return {
        "status": "success",
        "message": f"Repository '{repo_name}' and all its graph relationships have been deleted."
    }
