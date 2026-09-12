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
    """
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
        records = session.run(query)
        return [dict(r) for r in records]

def get_repository_summary(repo_name: str) -> Dict[str, Any]:
    """Returns detailed statistics and language distribution for a single repository."""
    clean_name = sanitize_name(repo_name)
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
        if not rec or not rec.get("name"):
            raise HTTPException(status_code=404, detail=f"Repository '{repo_name}' not found.")
        return dict(rec)

def delete_repository(repo_name: str) -> Dict[str, Any]:
    """
    Completely prunes a repository and all its associated files, classes,
    functions, and relationships from the Neo4j Knowledge Graph and disk sandbox.
    """
    clean_name = sanitize_name(repo_name)

    # 1. Prune graph nodes in Neo4j
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

    # 2. Delete local sandbox folder if exists
    repo_disk_dir = ALLOWED_REPO_DIR / clean_name
    if repo_disk_dir.exists():
        shutil.rmtree(repo_disk_dir, ignore_errors=True)

    return {
        "status": "success",
        "message": f"Repository '{repo_name}' and all its graph relationships have been deleted."
    }
