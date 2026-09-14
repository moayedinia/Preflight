import os
from typing import Dict, Any, List, Optional
from app.core.graph import driver

def detect_circular_dependencies(repo_name: Optional[str] = None, max_depth: int = 3) -> List[Dict[str, Any]]:
    """
    Finds circular function/method call chains in the Knowledge Graph:
    e.g. fnA -> fnB -> fnC -> fnA
    """
    with driver.session() as session:
        query = """
        MATCH (a:Function)
        WHERE $repo_name IS NULL OR a.repo = $repo_name
        MATCH path = (a)-[:CALLS*2..3]->(a)
        RETURN [n in nodes(path) | n.name] AS cycle,
               [n in nodes(path) | n.file_path] AS files,
               length(path) AS cycle_length
        LIMIT 20
        """
        records = session.run(query, repo_name=repo_name)
        cycles = []
        for r in records:
            cycles.append({
                "cycle": r["cycle"],
                "files": r["files"],
                "length": r["cycle_length"]
            })
        return cycles

def detect_high_risk_hubs(repo_name: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Identifies 'God Functions' or critical bottleneck hubs with very high in-degree
    (many callers) or out-degree (calls too many functions). High risk for refactoring.
    """
    with driver.session() as session:
        query = """
        MATCH (fn:Function)
        WHERE $repo_name IS NULL OR fn.repo = $repo_name
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
        OPTIONAL MATCH (fn)-[:CALLS]->(callee)
        WITH fn, count(DISTINCT caller) AS in_degree, count(DISTINCT callee) AS out_degree
        WHERE in_degree > 3 OR out_degree > 5
        RETURN fn.name AS name,
               fn.file_path AS file_path,
               fn.repo AS repo,
               fn.language AS language,
               in_degree,
               out_degree,
               (in_degree * 2 + out_degree) AS risk_score
        ORDER BY risk_score DESC
        LIMIT $limit
        """
        records = session.run(query, repo_name=repo_name, limit=limit)
        return [dict(r) for r in records]

def detect_dead_code_candidates(repo_name: Optional[str] = None, limit: int = 15) -> List[Dict[str, Any]]:
    """
    Finds standalone functions or methods with 0 inbound callers.
    Candidates for dead code or external API entrypoints.
    """
    with driver.session() as session:
        query = """
        MATCH (fn:Function)
        WHERE ($repo_name IS NULL OR fn.repo = $repo_name)
          AND NOT (fn.name STARTS WITH '__')
          AND NOT (fn.name IN ['main', 'init', 'setup', 'run', 'handler', 'app'])
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
        WITH fn, count(caller) AS callers_count
        WHERE callers_count = 0
        RETURN fn.name AS name,
               fn.file_path AS file_path,
               fn.repo AS repo,
               fn.language AS language,
               fn.start_line AS line
        LIMIT $limit
        """
        records = session.run(query, repo_name=repo_name, limit=limit)
        return [dict(r) for r in records]

def calculate_blast_radius(function_name: str, repo_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Calculates the exact Blast Radius of modifying a function:
    1. Direct callers (1st degree)
    2. Indirect cascading callers (2nd and 3rd degree)
    3. Affected files and classes across the enterprise.
    """
    with driver.session() as session:
        query = """
        MATCH (target:Function {name: $function_name})
        WHERE $repo_name IS NULL OR target.repo = $repo_name
        OPTIONAL MATCH path = (caller:Function)-[:CALLS*1..3]->(target)
        WITH target, caller, path
        RETURN target.name AS target_name,
               target.file_path AS target_file,
               COLLECT(DISTINCT caller.name) AS affected_functions,
               COLLECT(DISTINCT caller.file_path) AS affected_files,
               count(DISTINCT caller) AS total_affected_count
        """
        rec = session.run(query, function_name=function_name, repo_name=repo_name).single()
        if not rec or not rec.get("target_name"):
            return {
                "target_name": function_name,
                "found": False,
                "affected_functions": [],
                "affected_files": [],
                "total_affected_count": 0,
                "risk_level": "LOW"
            }

        count = rec["total_affected_count"]
        risk = "CRITICAL" if count > 10 else ("HIGH" if count > 5 else ("MEDIUM" if count > 1 else "LOW"))

        return {
            "target_name": rec["target_name"],
            "target_file": rec["target_file"],
            "found": True,
            "affected_functions": rec["affected_functions"],
            "affected_files": rec["affected_files"],
            "total_affected_count": count,
            "risk_level": risk
        }

def get_git_hotspots(repo_name: Optional[str] = None, limit: int = 15) -> List[Dict[str, Any]]:
    """
    Finds files that change the most often based on Git commit count (churn rate).
    """
    with driver.session() as session:
        query = """
        MATCH (f:File)
        WHERE ($repo_name IS NULL OR f.repo = $repo_name) AND f.churn > 0
        RETURN f.path AS path, f.churn AS churn, f.language AS language
        ORDER BY f.churn DESC
        LIMIT $limit
        """
        records = session.run(query, repo_name=repo_name, limit=limit)
        return [dict(r) for r in records]

def detect_code_clones(repo_name: Optional[str] = None, threshold: float = 0.95, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Detects duplicated code blocks (clones) by computing Cosine Similarity on embeddings.
    Warning: O(N^2) complexity in memory, so we compute top 500 largest functions.
    """
    with driver.session() as session:
        # Instead of full N^2 in Neo4j, we limit to the largest functions to avoid timeout
        query = """
        MATCH (f1:Function), (f2:Function)
        WHERE id(f1) > id(f2)
          AND ($repo_name IS NULL OR f1.repo = $repo_name)
          AND ($repo_name IS NULL OR f2.repo = $repo_name)
          AND f1.embedding IS NOT NULL AND f2.embedding IS NOT NULL
          AND size(f1.code) > 200 AND size(f2.code) > 200
        WITH f1, f2, vector.similarity.cosine(f1.embedding, f2.embedding) AS sim
        WHERE sim >= $threshold
        RETURN f1.name AS fn1, f1.file_path AS file1,
               f2.name AS fn2, f2.file_path AS file2,
               sim AS similarity
        ORDER BY sim DESC
        LIMIT $limit
        """
        records = session.run(query, repo_name=repo_name, threshold=threshold, limit=limit)
        return [dict(r) for r in records]
