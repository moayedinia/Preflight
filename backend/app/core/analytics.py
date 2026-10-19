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

def detect_high_risk_hubs(repo_name: Optional[str] = None, limit: int = 15) -> List[Dict[str, Any]]:
    """
    Identifies high-risk architectural hubs and classifies them into distinct code smell categories:
    - God Function / God Method (high fan-out + large LOC)
    - Critical Bottleneck Hub (high fan-in)
    - Spaghetti Coupling Hub (high bidirectional fan-in and fan-out)
    - High-Churn Risky Hub (frequent git commits + high degree)
    - Brain Method / Complex Coordinator (high subsystem orchestration)
    """
    with driver.session() as session:
        query = """
        MATCH (fn:Function)
        WHERE $repo_name IS NULL OR fn.repo = $repo_name
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
        OPTIONAL MATCH (fn)-[:CALLS]->(callee)
        OPTIONAL MATCH (f:File {path: fn.file_path})
        WITH fn, count(DISTINCT caller) AS in_degree, count(DISTINCT callee) AS out_degree, max(f.churn) AS churn
        WHERE in_degree > 2 OR out_degree > 4
        RETURN coalesce(fn.id, elementId(fn)) AS id,
               fn.name AS name,
               fn.file_path AS file_path,
               fn.repo AS repo,
               fn.language AS language,
               fn.start_line AS line,
               fn.end_line AS end_line,
               fn.code AS code,
               fn.docstring AS docstring,
               in_degree,
               out_degree,
               coalesce(churn, 0) AS churn,
               (in_degree * 2 + out_degree) AS risk_score
        ORDER BY risk_score DESC
        LIMIT $limit
        """
        records = session.run(query, repo_name=repo_name, limit=limit)
        hubs = []
        for r in records:
            item = dict(r)
            in_deg = item.get("in_degree", 0)
            out_deg = item.get("out_degree", 0)
            churn = item.get("churn") or 0
            line = item.get("line") or 1
            end_line = item.get("end_line") or line
            code_lines = len((item.get("code") or "").splitlines())
            loc = max(end_line - line + 1, code_lines, 1)

            # Categorize into distinct architectural smells
            if (out_deg >= 5 and loc >= 35) or out_deg >= 7:
                smell_type = "God Function"
                smell_type_fa = "God Function"
                severity = "CRITICAL"
                badge_color = "rose"
                desc = f"This function has {out_deg} outbound calls and {loc} lines of code, violating the Single Responsibility Principle (SRP) by accumulating too many duties."
                rec = "Break into smaller helper methods (Extract Method) and separate concerns using object-oriented design patterns."
            elif in_deg >= 4 and out_deg <= 3:
                smell_type = "Critical Bottleneck Hub"
                smell_type_fa = "Critical Bottleneck Hub"
                severity = "CRITICAL" if in_deg >= 6 else "HIGH"
                badge_color = "amber"
                desc = f"This function is invoked by {in_deg} different functions across the project, acting as a high-risk central dependency and bottleneck."
                rec = "Introduce an abstraction interface or apply the Facade pattern to reduce direct coupling."
            elif in_deg >= 3 and out_deg >= 3:
                smell_type = "Spaghetti Coupling Hub"
                smell_type_fa = "Spaghetti Coupling Hub"
                severity = "CRITICAL"
                badge_color = "purple"
                desc = f"This function has {in_deg} callers and {out_deg} outgoing calls, residing within an entangled network with bidirectional coupling."
                rec = "Decouple inbound and outbound flows using the Mediator pattern or event-driven architecture."
            elif churn >= 3 and (in_deg + out_deg) >= 3:
                smell_type = "High-Churn Risky Hub"
                smell_type_fa = "High-Churn Risky Hub"
                severity = "HIGH"
                badge_color = "orange"
                desc = f"This file exhibits high git churn ({churn} commits) while maintaining significant architectural connections."
                rec = "Stabilize method contracts and implement regression tests before introducing further modifications."
            elif out_deg >= 4:
                smell_type = "Brain Method / Coordinator"
                smell_type_fa = "Brain Method / Coordinator"
                severity = "HIGH"
                badge_color = "sky"
                desc = f"This method orchestrates complex control flow by invoking {out_deg} distinct subroutines."
                rec = "Apply the Chain of Responsibility or State pattern to split conditional logic branches."
            else:
                smell_type = "High-Fanout Hub"
                smell_type_fa = "High-Fanout Hub"
                severity = "MEDIUM"
                badge_color = "yellow"
                desc = f"This function has high connectivity in the code graph (In: {in_deg}, Out: {out_deg})."
                rec = "Review architectural boundaries and minimize unnecessary inter-module connections."

            item["smell_type"] = smell_type
            item["smell_type_fa"] = smell_type_fa
            item["smell_severity"] = severity
            item["smell_badge_color"] = badge_color
            item["smell_description"] = desc
            item["recommendation"] = rec
            item["loc"] = loc
            hubs.append(item)
        return hubs

def detect_dead_code_candidates(repo_name: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """
    Finds standalone functions or methods with 0 inbound callers.
    Candidates for dead code or external API entrypoints.
    """
    with driver.session() as session:
        query = """
        MATCH (fn:Function)
        WHERE ($repo_name IS NULL OR fn.repo = $repo_name)
          AND NOT (fn.name STARTS WITH '__')
          AND NOT (fn.name IN ['main', 'init', 'setup', 'run', 'handler', 'app', 'index'])
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
        WITH fn, count(caller) AS callers_count
        WHERE callers_count = 0
        RETURN coalesce(fn.id, elementId(fn)) AS id,
               fn.name AS name,
               fn.file_path AS file_path,
               fn.repo AS repo,
               fn.language AS language,
               fn.start_line AS line,
               fn.end_line AS end_line,
               fn.code AS code,
               fn.docstring AS docstring,
               callers_count,
               'This function is not invoked by any other function or method across the repository (inbound callers: 0).' AS reason
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

def detect_code_clones(repo_name: Optional[str] = None, threshold: float = 0.95, limit: int = 20,
                       max_candidates: int = 4000) -> List[Dict[str, Any]]:
    """
    Detects duplicated code blocks (clones) by computing Cosine Similarity on
    function embeddings.

    D5 fix: the old implementation ran a full `MATCH (f1),(f2)` cross-join with
    `vector.similarity.cosine` INSIDE Neo4j — O(N^2) per-pair vector calls executed
    by the database, which timed out / OOM'd on real repos (the old docstring even
    admitted it). This version fetches the candidate embeddings once, stacks them
    into a float32 matrix, and computes the full similarity matrix in a single BLAS
    GEMM (normed @ normed.T). Work is still O(N^2) FLOPs, but it runs as one vectorized
    matmul in Python RAM instead of N^2 Cypher function calls, so it is far faster and
    does not hold the database. The candidate set is capped to `max_candidates`
    (default 4000 -> a fixed ~64MB matrix), and to functions with a real body
    (code > 200 chars); a final pure helper does the de-duplication and ranking.
    """
    with driver.session() as session:
        # 1. Fetch candidate functions (largest bodies first, so the cap keeps the
        #    most meaningful code) that have embeddings.
        candidates = session.run(
            """
            MATCH (f:Function)
            WHERE ($repo_name IS NULL OR f.repo = $repo_name)
              AND f.embedding IS NOT NULL
              AND size(f.code) > 200
            RETURN coalesce(f.id, elementId(f)) AS id,
                   f.name AS name,
                   f.file_path AS file_path,
                   f.embedding AS embedding
            ORDER BY size(f.code) DESC
            LIMIT $max_candidates
            """,
            repo_name=repo_name,
            max_candidates=max_candidates,
        ).data()

    if len(candidates) < 2:
        return []

    import numpy as np
    from app.core.resolve import dedupe_rank_clone_pairs

    # 2. Stack into a float matrix. Drop any candidate whose vector is empty/mis-typed.
    vecs = []
    valid = []
    for c in candidates:
        e = c.get("embedding")
        if isinstance(e, (list, tuple)) and len(e) > 0:
            try:
                vecs.append(np.asarray(e, dtype=np.float32))
                valid.append(c)
            except (TypeError, ValueError):
                continue
    if len(vecs) < 2:
        return []

    mat = np.vstack(vecs)
    # 3. Full cosine matrix in one BLAS pass: (M @ M.T) / (||m|| * ||m||)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    normed = mat / norms[:, None]
    sim = normed @ normed.T  # shape (N, N)

    # 4. Collect candidate pairs above threshold (upper triangle, i<j).
    iu = np.triu_indices_from(sim, k=1)
    pairs = sim[iu]
    i, j = iu
    mask = pairs >= threshold
    i, j, pairs = i[mask], j[mask], pairs[mask]

    if len(i) == 0:
        return []

    # 5. Build rows; de-dup + rank via the pure, unit-tested helper.
    rows = []
    for a, b, s in zip(i.tolist(), j.tolist(), pairs.tolist()):
        ca, cb = valid[a], valid[b]
        key = "||".join(sorted([ca["id"], cb["id"]]))
        rows.append((ca["name"], cb["name"], float(s), key))

    return dedupe_rank_clone_pairs(rows, threshold=threshold, limit=limit)

def get_documentation_coverage_stats(repo_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Computes overall project documentation coverage statistics:
    - total functions / methods
    - documented functions
    - undocumented functions
    - documentation coverage percentage
    - total classes
    - documented classes
    - undocumented classes
    - sample of undocumented functions
    """
    with driver.session() as session:
        query = """
        MATCH (f:Function)
        WHERE $repo_name IS NULL OR f.repo = $repo_name
        WITH count(f) AS total_functions,
             count(CASE WHEN f.docstring IS NOT NULL AND trim(f.docstring) <> '' THEN 1 END) AS documented_functions
        OPTIONAL MATCH (c:Class)
        WHERE $repo_name IS NULL OR c.repo = $repo_name
        WITH total_functions, documented_functions,
             count(c) AS total_classes,
             count(CASE WHEN c.docstring IS NOT NULL AND trim(c.docstring) <> '' THEN 1 END) AS documented_classes
        RETURN total_functions, documented_functions, total_classes, documented_classes
        """
        record = session.run(query, repo_name=repo_name).single()
        
        tot_fn = record["total_functions"] if record else 0
        doc_fn = record["documented_functions"] if record else 0
        undoc_fn = max(0, tot_fn - doc_fn)
        
        tot_cls = record["total_classes"] if record else 0
        doc_cls = record["documented_classes"] if record else 0
        undoc_cls = max(0, tot_cls - doc_cls)
        
        coverage_pct = round((doc_fn * 100.0) / max(tot_fn, 1), 1) if tot_fn > 0 else 100.0
        
        undoc_query = """
        MATCH (f:Function)
        WHERE ($repo_name IS NULL OR f.repo = $repo_name)
          AND (f.docstring IS NULL OR trim(f.docstring) = '')
        RETURN coalesce(f.id, elementId(f)) AS id, f.name AS name, f.file_path AS file_path, coalesce(f.start_line, 1) AS line
        LIMIT 30
        """
        undoc_records = [dict(r) for r in session.run(undoc_query, repo_name=repo_name)]

        return {
            "repo_name": repo_name or "All Repositories",
            "total_functions": tot_fn,
            "documented_functions": doc_fn,
            "undocumented_functions": undoc_fn,
            "coverage_percent": coverage_pct,
            "total_classes": tot_cls,
            "documented_classes": doc_cls,
            "undocumented_classes": undoc_cls,
            "undocumented_sample": undoc_records
        }

def get_full_project_code_and_docs_data(repo_name: Optional[str] = None, max_files: int = 150) -> Dict[str, Any]:
    """
    Extracts complete codebase structure, full source codes, docstrings, classes,
    methods, and function relationships across the entire project from Neo4j.
    """
    with driver.session() as session:
        file_query = """
        MATCH (f:File)
        WHERE $repo_name IS NULL OR f.repo = $repo_name
        RETURN f.id AS id, f.path AS path, f.repo AS repo, f.language AS language, coalesce(f.churn, 0) AS churn
        ORDER BY f.path ASC
        LIMIT $max_files
        """
        files = [dict(r) for r in session.run(file_query, repo_name=repo_name, max_files=max_files)]

        class_query = """
        MATCH (c:Class)
        WHERE $repo_name IS NULL OR c.repo = $repo_name
        OPTIONAL MATCH (c)-[:HAS_METHOD]->(m:Function)
        RETURN c.name AS class_name, c.file_path AS file_path, c.docstring AS docstring,
               c.code AS code, c.start_line AS start_line, c.end_line AS end_line,
               collect({
                   name: m.name,
                   docstring: m.docstring,
                   code: m.code,
                   start_line: m.start_line,
                   end_line: m.end_line
               }) AS methods
        LIMIT 200
        """
        classes = [dict(r) for r in session.run(class_query, repo_name=repo_name)]

        func_query = """
        MATCH (fn:Function)
        WHERE $repo_name IS NULL OR fn.repo = $repo_name
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
        OPTIONAL MATCH (fn)-[:CALLS]->(callee:Function)
        RETURN coalesce(fn.id, elementId(fn)) AS id, fn.name AS name, fn.file_path AS file_path,
               fn.docstring AS docstring, fn.code AS code, fn.start_line AS start_line,
               fn.end_line AS end_line,
               collect(DISTINCT caller.name) AS callers,
               collect(DISTINCT callee.name) AS callees
        LIMIT 500
        """
        functions = [dict(r) for r in session.run(func_query, repo_name=repo_name)]

        coverage = get_documentation_coverage_stats(repo_name)

        data = {
            "repo_name": repo_name or "All Repositories",
            "coverage": coverage,
            "files": files,
            "classes": classes,
            "functions": functions
        }
        data["markdown_report"] = build_codebase_markdown_export(data)
        return data

def build_codebase_markdown_export(data: Dict[str, Any]) -> str:
    """Formats full project files, classes, methods, docstrings and code into a structured Markdown document."""
    cov = data.get("coverage", {})
    repo = data.get("repo_name", "All Repositories")
    files = data.get("files", [])
    classes = data.get("classes", [])
    functions = data.get("functions", [])

    lines = [
        f"# Complete Codebase & Documentation Dossier: {repo}",
        f"- **Total Files:** {len(files)}",
        f"- **Total Classes:** {len(classes)}",
        f"- **Total Functions:** {len(functions)}",
        f"- **Documentation Coverage:** `{cov.get('coverage_percent', 0)}%` ({cov.get('documented_functions', 0)}/{cov.get('total_functions', 0)} documented)",
        "",
        "---",
        "## 1. Project Files & Architecture",
        ""
    ]

    for f in files:
        lines.append(f"- **`{f.get('path')}`** ({f.get('language') or 'text'}) - Churn: {f.get('churn', 0)}")

    lines.append("\n---\n## 2. Classes & Method Implementations\n")
    if classes:
        for c in classes:
            lines.append(f"### Class `{c.get('class_name')}` ({c.get('file_path', 'unknown')})")
            if c.get("docstring"):
                lines.append(f"> {c['docstring']}\n")
            methods = c.get("methods", [])
            if methods:
                lines.append(f"**Methods ({len(methods)}):**")
                for m in methods:
                    if isinstance(m, dict):
                        lines.append(f"- `{m.get('name')}`: {m.get('docstring') or 'No docstring'}")
            lines.append("")
    else:
        lines.append("_No classes defined._\n")

    lines.append("---\n## 3. Function Implementations, Docstrings & Full Code\n")
    for fn in functions:
        callers = ", ".join(fn.get("callers", [])) or "None"
        callees = ", ".join(fn.get("callees", [])) or "None"
        lines.append(f"### Symbol `{fn.get('name')}`")
        lines.append(f"- **File:** `{fn.get('file_path')}` (Lines {fn.get('start_line', 1)}-{fn.get('end_line', 1)})")
        lines.append(f"- **Callers:** {callers} | **Calls:** {callees}")
        if fn.get("docstring"):
            lines.append(f"- **Documentation:** _{fn['docstring']}_")
        code = fn.get("code") or ""
        if code:
            lines.append(f"```python\n{code[:4000]}\n```")
        lines.append("")

    return "\n".join(lines)
