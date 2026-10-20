import os
import sys
import argparse
from typing import Optional, List
try:
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except (ImportError, ModuleNotFoundError):
        class FastMCP:
            """Fallback FastMCP implementation when mcp package is not installed in local environment."""
            def __init__(self, name="CodeGraph-Brain-MCP", **kwargs):
                self.name = name
                self._tools = {}
                self.settings = None

            def tool(self, name: Optional[str] = None):
                def decorator(fn):
                    tool_name = name or fn.__name__
                    self._tools[tool_name] = fn
                    return fn
                return decorator

            def run(self, transport="stdio"):
                print(f"[Notice] MCP package not installed. Cannot start {transport} listener.")

from app.core.graph import (
    driver,
    vector_search,
    expand_graph_context,
    get_graph_stats,
    ingest_parsed_data
)
from app.core.parser import parse_source_file
from app.core.embedder import get_embedding
from app.core.scanner import scan_and_ingest_directory
from app.core.security import validate_safe_path, sanitize_name
from app.core.analytics import (
    detect_circular_dependencies,
    detect_high_risk_hubs,
    detect_dead_code_candidates,
    calculate_blast_radius,
    get_documentation_coverage_stats,
    get_full_project_code_and_docs_data
)
from app.core.repo_manager import list_all_repositories
from app.core.reviewer import generate_architectural_code_review
from app.core.llm import call_llm

# Initialize FastMCP Server
mcp = FastMCP(
    "CodeGraph-Brain-MCP",
    dependencies=["neo4j", "openai", "tree-sitter"]
)

@mcp.tool()
def search_code_graph(query: str, repo_name: Optional[str] = None, top_k: int = 5) -> str:
    """
    Semantically searches the enterprise codebase knowledge graph for relevant functions and classes.
    Returns matching source code snippets along with structural call graph context (callers, callees, classes).
    
    Args:
        query: Natural language question or code search phrase (e.g. "JWT authentication handling", "parser logic").
        repo_name: Optional specific repository to limit the search scope.
        top_k: Number of relevant code entities to retrieve (1-25).
    """
    clean_top_k = min(max(top_k, 1), 25)
    clean_repo = sanitize_name(repo_name) if repo_name else None

    query_vector = get_embedding(query[:2000])
    hits = vector_search(query_vector, top_k=clean_top_k, repo_name=clean_repo)
    
    if not hits:
        return f"No matching code entities found in the graph for query: '{query}'."

    func_ids = [h["id"] for h in hits if h.get("id")]
    relational_context = expand_graph_context(func_ids)

    results = []
    for hit in hits:
        rel = next((r for r in relational_context if r.get("function_id") == hit.get("id")), {})
        callers = ", ".join(rel.get("called_by", [])) or "None"
        callees = ", ".join(rel.get("calls", [])) or "None"
        parent_class = rel.get("class_name") or "None"
        
        block = (
            f"### Entity: {hit.get('name')}\n"
            f"- **Repository:** {hit.get('repo')}\n"
            f"- **File:** {hit.get('file_path')} (Lines {hit.get('start_line')}-{hit.get('end_line')})\n"
            f"- **Class:** {parent_class}\n"
            f"- **Docstring:** {hit.get('docstring') or 'N/A'}\n"
            f"- **Called By:** {callers}\n"
            f"- **Calls:** {callees}\n"
            f"```python\n{hit.get('code')}\n```"
        )
        results.append(block)

    return "\n\n---\n\n".join(results)

@mcp.tool()
def get_function_details(function_name: str, repo_name: Optional[str] = None) -> str:
    """
    Retrieves full source code, docstrings, and relationships for a specific function by its name.
    
    Args:
        function_name: Name of the function or method.
        repo_name: Optional repository name to disambiguate identical names across repos.
    """
    clean_fn = sanitize_name(function_name)
    clean_repo = sanitize_name(repo_name) if repo_name else None

    with driver.session() as session:
        query = """
        MATCH (fn:Function {name: $function_name})
        WHERE $repo_name IS NULL OR fn.repo = $repo_name
        OPTIONAL MATCH (c:Class)-[:HAS_METHOD]->(fn)
        OPTIONAL MATCH (f:File)-[:CONTAINS]->(fn)
        OPTIONAL MATCH (fn)-[:CALLS]->(callee)
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
        RETURN fn, c.name AS class_name, f.path AS file_path,
               COLLECT(DISTINCT callee.name) AS calls,
               COLLECT(DISTINCT caller.name) AS called_by
        """
        records = list(session.run(query, function_name=clean_fn, repo_name=clean_repo))

    if not records:
        return f"Function '{clean_fn}' was not found in the Code Knowledge Graph."

    output = []
    for r in records:
        fn = r["fn"]
        output.append(
            f"## Function: {fn.get('name')}\n"
            f"- **File:** {r.get('file_path')} (Lines {fn.get('start_line')}-{fn.get('end_line')})\n"
            f"- **Class:** {r.get('class_name') or 'Standalone'}\n"
            f"- **Docstring:** {fn.get('docstring') or 'None'}\n"
            f"- **Who Calls This Function:** {', '.join(r.get('called_by', [])) or 'None'}\n"
            f"- **Functions This Calls:** {', '.join(r.get('calls', [])) or 'None'}\n\n"
            f"### Source Code:\n```python\n{fn.get('code')}\n```"
        )
    return "\n\n---\n\n".join(output)

@mcp.tool()
def find_callers(function_name: str, repo_name: Optional[str] = None) -> str:
    """
    Impact Analysis tool: Finds all functions/classes that call a given function across the enterprise codebase.
    Crucial for checking breaking changes and refactoring safety.
    
    Args:
        function_name: Target function name to trace callers for.
        repo_name: Optional repository name filter.
    """
    clean_fn = sanitize_name(function_name)
    clean_repo = sanitize_name(repo_name) if repo_name else None

    with driver.session() as session:
        query = """
        MATCH (caller:Function)-[:CALLS]->(target)
        WHERE (target.name = $function_name OR target.id ENDS WITH $function_name)
          AND ($repo_name IS NULL OR caller.repo = $repo_name)
        OPTIONAL MATCH (f:File)-[:CONTAINS]->(caller)
        RETURN caller.name AS caller_name, caller.repo AS repo, f.path AS file_path,
               caller.start_line AS line
        """
        records = list(session.run(query, function_name=clean_fn, repo_name=clean_repo))

    if not records:
        return f"No callers found for '{clean_fn}'."

    lines = [f"Found {len(records)} caller(s) for '{clean_fn}':"]
    for r in records:
        lines.append(f"- `{r['caller_name']}` in `{r['file_path']}:{r['line']}` (Repo: {r['repo']})")
    return "\n".join(lines)

@mcp.tool()
def get_codebase_stats() -> str:
    """
    Returns an overview of the entire indexed enterprise knowledge graph:
    counts of repositories, files, classes, functions, and total structural relationships.
    """
    stats = get_graph_stats()
    return (
        "### Enterprise Code Knowledge Graph Overview:\n"
        f"- **Repositories Indexed:** {stats.get('repositories', 0)}\n"
        f"- **Source Files:** {stats.get('files', 0)}\n"
        f"- **Classes:** {stats.get('classes', 0)}\n"
        f"- **Functions & Methods:** {stats.get('functions', 0)}\n"
        f"- **Total Graph Relationships:** {stats.get('relationships', 0)}"
    )

@mcp.tool()
def index_local_repository(directory_path: str, repo_name: Optional[str] = None) -> str:
    """
    Indexes a local directory or repository into the Code Knowledge Graph.
    Path is strictly validated against directory traversal and sandbox boundaries.
    
    Args:
        directory_path: Path to the codebase directory inside allowed sandbox.
        repo_name: Optional identifier for the repository.
    """
    try:
        safe_path = validate_safe_path(directory_path)
        clean_repo = sanitize_name(repo_name) if repo_name else sanitize_name(safe_path.name)
        res = scan_and_ingest_directory(str(safe_path), clean_repo)
        return (
            f"Successfully indexed repository '{res['repo_name']}':\n"
            f"- Files Scanned: {res['total_files_scanned']}\n"
            f"- Files Parsed: {res['parsed_files']}\n"
            f"- Classes Indexed: {res['classes_indexed']}\n"
            f"- Functions Indexed: {res['functions_indexed']}\n"
            f"- Errors Encountered: {len(res['errors'])}"
        )
    except Exception as e:
        return f"Failed to index repository: {str(e)}"

@mcp.tool()
def clone_and_index_git(
    git_url: str,
    repo_name: Optional[str] = None,
    branch: Optional[str] = None,
    token: Optional[str] = None
) -> str:
    """
    Automated tool for AI agents: Clones a remote Git repository, parses its AST structure,
    generates vector embeddings, and builds the Knowledge Graph.
    
    Args:
        git_url: HTTPS or SSH URL of the git repository (e.g. 'https://github.com/fastapi/fastapi.git').
        repo_name: Optional custom name for the repository. If omitted, inferred from the URL.
        branch: Optional branch name to checkout (e.g. 'main', 'master', 'dev').
        token: Optional personal access token (PAT) for private repositories.
    """
    try:
        from app.core.cloner import clone_git_repo
        cloned_path = clone_git_repo(git_url=git_url, repo_name=repo_name, branch=branch, token=token)
        actual_name = repo_name or cloned_path.name
        res = scan_and_ingest_directory(str(cloned_path), actual_name)
        return (
            f"Successfully cloned and indexed Git repository '{res['repo_name']}':\n"
            f"- Source URL: {git_url}\n"
            f"- Branch: {branch or 'default'}\n"
            f"- Total Files Scanned: {res['total_files_scanned']}\n"
            f"- Files Parsed: {res['parsed_files']}\n"
            f"- Classes Indexed: {res['classes_indexed']}\n"
            f"- Functions Indexed: {res['functions_indexed']}\n"
            f"- Errors: {len(res['errors'])}"
        )
    except Exception as e:
        return f"Failed to clone and index repository: {str(e)}"

@mcp.tool()
def clone_and_index_git_repository(
    git_url: str,
    repo_name: Optional[str] = None,
    branch: Optional[str] = None,
    token: Optional[str] = None
) -> str:
    """Alias for clone_and_index_git."""
    return clone_and_index_git(git_url=git_url, repo_name=repo_name, branch=branch, token=token)

@mcp.tool()
def ingest_code_snippet(repo_name: str, file_path: str, code_content: str) -> str:
    """
    Directly ingests a single source code snippet or new file into the Code Knowledge Graph.
    Enables AI agents to immediately index newly written or modified files without full repository rescanning.
    
    Args:
        repo_name: Name of the repository (e.g. 'my-project').
        file_path: Relative file path (e.g. 'services/auth.py').
        code_content: Full source code text of the file.
    """
    try:
        clean_repo = sanitize_name(repo_name)
        code_bytes = code_content.encode("utf-8")
        parsed_data = parse_source_file(file_path, code_bytes)
        ingest_parsed_data(clean_repo, parsed_data)
        return (
            f"Successfully parsed and indexed '{file_path}' into repository '{clean_repo}':\n"
            f"- Language Detected: {parsed_data.get('language', 'unknown')}\n"
            f"- Classes Indexed: {len(parsed_data.get('classes', []))}\n"
            f"- Functions Indexed: {len(parsed_data.get('functions', []))}\n"
            f"- Imports Linked: {len(parsed_data.get('imports', []))}"
        )
    except Exception as e:
        return f"Failed to ingest snippet: {str(e)}"

@mcp.tool()
def list_repositories() -> str:
    """
    Lists all indexed software repositories in the Code Knowledge Graph with file counts,
    class counts, function counts, and detected programming languages.
    """
    repos = list_all_repositories()
    if not repos:
        return "No repositories have been indexed yet in the Knowledge Graph."
    
    lines = ["### Indexed Repositories:"]
    for r in repos:
        langs = ", ".join(r.get("languages", [])) or "universal"
        lines.append(
            f"- **{r['name']}**: {r['file_count']} files, {r['class_count']} classes, "
            f"{r['func_count']} functions (Languages: {langs})"
        )
    return "\n".join(lines)

@mcp.tool()
def detect_code_smells(repo_name: Optional[str] = None) -> str:
    """
    Analyzes architectural anti-patterns in the codebase:
    1. Circular Dependencies (e.g. A -> B -> C -> A)
    2. High-Risk Hub Functions / God Functions (excessive callers or callees)
    3. Dead Code Candidates (functions with 0 callers)
    
    Args:
        repo_name: Optional repository filter.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    cycles = detect_circular_dependencies(clean_repo)
    hubs = detect_high_risk_hubs(clean_repo, limit=8)
    dead = detect_dead_code_candidates(clean_repo, limit=10)

    output = [f"## Architectural Code Smells Report ({clean_repo or 'Entire Enterprise'}):"]

    output.append(f"\n### 1. Circular Dependencies Found: {len(cycles)}")
    if cycles:
        for c in cycles:
            chain = " -> ".join(c["cycle"])
            output.append(f"- Chain ({c['length']} hops): `{chain}`")
    else:
        output.append("- No circular dependency cycles detected.")

    output.append(f"\n### 2. High-Risk Hub / God Functions ({len(hubs)}):")
    if hubs:
        for h in hubs:
            output.append(
                f"- `{h['name']}` ({h['file_path']}): Inbound callers={h['in_degree']}, "
                f"Outbound calls={h['out_degree']} [Risk Score: {h['risk_score']}]"
            )
    else:
        output.append("- No severe bottleneck hub functions found.")

    output.append(f"\n### 3. Dead Code Candidates ({len(dead)}):")
    if dead:
        for d in dead:
            output.append(f"- `{d['name']}` in `{d['file_path']}:{d.get('line', 1)}` (0 callers)")
    else:
        output.append("- No obvious dead code candidates detected.")

    return "\n".join(output)

@mcp.tool()
def analyze_change_impact(function_name: str, repo_name: Optional[str] = None) -> str:
    """
    Calculates downstream Blast Radius before refactoring or modifying a function:
    Reports cascading callers up to 3 degrees away and list of all affected enterprise files.
    
    Args:
        function_name: Name of the function to be modified.
        repo_name: Optional repository name.
    """
    clean_fn = sanitize_name(function_name)
    clean_repo = sanitize_name(repo_name) if repo_name else None

    blast = calculate_blast_radius(clean_fn, clean_repo)
    if not blast["found"]:
        return f"Function '{clean_fn}' was not found in the Knowledge Graph."

    files_preview = ", ".join(blast["affected_files"][:8]) or "Local to current file"
    funcs_preview = ", ".join(blast["affected_functions"][:10]) or "None"

    return (
        f"### Blast Radius Report for `{blast['target_name']}`\n"
        f"- **File:** `{blast['target_file']}`\n"
        f"- **Calculated Risk Level:** `{blast['risk_level']}`\n"
        f"- **Total Affected Callers:** {blast['total_affected_count']}\n"
        f"- **Affected Downstream Functions:** {funcs_preview}\n"
        f"- **Files Requiring Regression Testing:** {files_preview}"
    )

@mcp.tool()
def review_code_diff(diff_text: str, repo_name: Optional[str] = None) -> str:
    """
    Performs an automated architectural review of a Git unified diff or pull request:
    Maps touched symbols to the Knowledge Graph, traces blast radius, and produces an architectural critique.
    
    Args:
        diff_text: Git diff text.
        repo_name: Optional repository name.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    res = generate_architectural_code_review(diff_text, repo_name=clean_repo)

    symbols = ", ".join(res["touched_symbols"]) or "None identified"
    affected_files = ", ".join(res["affected_files"][:6]) or "None"

    return (
        f"## Architectural PR Review\n"
        f"- **Risk Rating:** `{res['risk_rating']}`\n"
        f"- **Touched Symbols:** {symbols}\n"
        f"- **Downstream Affected Functions:** {res['affected_functions_count']}\n"
        f"- **Downstream Affected Files:** {res['affected_files_count']} ({affected_files})\n\n"
        f"### Architectural Review Assessment:\n"
        f"{res['ai_review']}"
    )

@mcp.tool()
def get_documentation_coverage(repo_name: Optional[str] = None) -> str:
    """
    Computes and returns the overall documentation coverage percentage of the codebase.
    Reports total functions, documented functions, undocumented functions, and a list of symbols missing docstrings.
    
    Args:
        repo_name: Optional repository name to filter documentation statistics.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    stats = get_documentation_coverage_stats(clean_repo)

    lines = [
        f"## 📚 Documentation Coverage Report ({stats['repo_name']})",
        f"- **Coverage Percentage:** `{stats['coverage_percent']}%`",
        f"- **Total Functions / Methods:** {stats['total_functions']}",
        f"- **Documented Functions:** {stats['documented_functions']} ({stats['coverage_percent']}%)",
        f"- **Undocumented Functions:** {stats['undocumented_functions']}",
        f"- **Classes:** {stats['documented_classes']} / {stats['total_classes']} documented",
        ""
    ]

    if stats["undocumented_sample"]:
        lines.append("### Functions Missing Docstrings (Sample):")
        for u in stats["undocumented_sample"][:15]:
            lines.append(f"- `{u['name']}` ({u['file_path']}:{u['line']})")
    else:
        lines.append("🎉 All functions are fully documented in this repository!")

    return "\n".join(lines)

@mcp.tool()
def run_auto_documentation(repo_name: Optional[str] = None, limit: int = 25) -> str:
    """
    Runs the automated documentation engine on the codebase:
    Finds functions missing docstrings, generates technical docstrings using the LLM,
    updates the Knowledge Graph, and returns the newly achieved documentation percentage.
    
    Args:
        repo_name: Optional repository name.
        limit: Maximum number of functions to document in this run (1-50).
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    clean_limit = min(max(limit, 1), 50)

    # 1. Fetch undocumented functions
    with driver.session() as session:
        query = """
        MATCH (f:Function)
        WHERE (f.docstring IS NULL OR trim(f.docstring) = '')
        """
        if clean_repo:
            query += " AND f.repo = $repo_name"
        query += " RETURN coalesce(f.id, elementId(f)) AS id, f.name AS name, f.code AS code, f.file_path AS file_path LIMIT $limit"
        records = [dict(r) for r in session.run(query, repo_name=clean_repo, limit=clean_limit)]

    if not records:
        cov = get_documentation_coverage_stats(clean_repo)
        return f"All functions in repository '{clean_repo or 'Entire Enterprise'}' are already documented! (Coverage: {cov['coverage_percent']}%)"

    system_prompt = (
        "You are an expert technical writer and senior software engineer. "
        "Your task is to write a concise, professional docstring for the provided function code. "
        "Do not include the function code or Markdown code blocks in your response. "
        "Return ONLY the raw text of the docstring."
    )

    documented_items = []
    with driver.session() as session:
        for item in records:
            code = (item.get("code") or "")[:2000]
            prompt = f"Function Name: {item['name']}\nCode:\n{code}"
            doc = call_llm(prompt, system_prompt).strip('"""').strip("'''").strip()
            if doc.startswith("```"):
                lines = doc.split("\n")
                doc = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

            session.run("""
                MATCH (f:Function)
                WHERE f.id = $id OR elementId(f) = $id
                SET f.docstring = $doc
            """, id=item["id"], doc=doc)
            documented_items.append(f"- `{item['name']}` ({item.get('file_path', 'unknown')})")

    after_cov = get_documentation_coverage_stats(clean_repo)

    return (
        f"## 🚀 Auto-Documentation Completed Successfully!\n"
        f"- **Newly Documented Functions:** {len(documented_items)}\n"
        f"- **New Project Documentation Coverage:** `{after_cov['coverage_percent']}%` "
        f"({after_cov['documented_functions']}/{after_cov['total_functions']} functions)\n\n"
        f"### Documented Entities:\n" + "\n".join(documented_items)
    )

@mcp.tool()
def get_full_project_code_and_docs(repo_name: Optional[str] = None, max_files: int = 100) -> str:
    """
    Extracts and exports the complete source code, classes, methods, docstrings,
    and architectural structure of the entire project from the Code Knowledge Graph.
    Allows an AI agent to ingest the whole codebase context and documentation in one call.
    
    Args:
        repo_name: Optional repository to export. Defaults to the active repository.
        max_files: Maximum files to include in export (1-200).
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    clean_max = min(max(max_files, 1), 200)
    data = get_full_project_code_and_docs_data(clean_repo, max_files=clean_max)

    cov = data["coverage"]
    lines = [
        f"# 📖 Complete Codebase & Documentation Dossier: {data['repo_name']}",
        f"- **Total Files:** {len(data['files'])}",
        f"- **Total Classes:** {len(data['classes'])}",
        f"- **Total Functions:** {len(data['functions'])}",
        f"- **Documentation Coverage:** `{cov['coverage_percent']}%` ({cov['documented_functions']}/{cov['total_functions']} documented)",
        "",
        "---",
        "## 1. Project Files & Architecture",
        ""
    ]

    for f in data["files"]:
        lines.append(f"- **`{f['path']}`** ({f.get('language') or 'text'}) - Churn: {f.get('churn', 0)}")

    lines.append("\n---\n## 2. Classes & Method Implementations\n")
    if data["classes"]:
        for c in data["classes"]:
            lines.append(f"### Class `{c['class_name']}` ({c.get('file_path', 'unknown')})")
            if c.get("docstring"):
                lines.append(f"> {c['docstring']}\n")
            methods = c.get("methods", [])
            if methods:
                lines.append(f"**Methods ({len(methods)}):**")
                for m in methods:
                    lines.append(f"- `{m.get('name')}`: {m.get('docstring') or 'No docstring'}")
            lines.append("")
    else:
        lines.append("_No classes defined._\n")

    lines.append("---\n## 3. Function Implementations, Docstrings & Full Code\n")
    for fn in data["functions"][:clean_max * 3]:
        callers = ", ".join(fn.get("callers", [])) or "None"
        callees = ", ".join(fn.get("callees", [])) or "None"
        lines.append(f"### Symbol `{fn['name']}`")
        lines.append(f"- **File:** `{fn.get('file_path')}` (Lines {fn.get('start_line', 1)}-{fn.get('end_line', 1)})")
        lines.append(f"- **Callers:** {callers} | **Calls:** {callees}")
        if fn.get("docstring"):
            lines.append(f"- **Documentation:** _{fn['docstring']}_")
        code = fn.get("code") or ""
        if code:
            lines.append(f"```python\n{code[:4000]}\n```")
        lines.append("")

    return "\n".join(lines)


# ==================== MCP TOOL REGISTRY & RUNNER ====================
MCP_TOOLS = {
    "clone_and_index_git": clone_and_index_git,
    "clone_and_index_git_repository": clone_and_index_git_repository,
    "analyze_change_impact": analyze_change_impact,
    "search_code_graph": search_code_graph,
    "get_function_details": get_function_details,
    "find_callers": find_callers,
    "get_codebase_stats": get_codebase_stats,
    "index_local_repository": index_local_repository,
    "ingest_code_snippet": ingest_code_snippet,
    "list_repositories": list_repositories,
    "detect_code_smells": detect_code_smells,
    "review_code_diff": review_code_diff,
    "get_documentation_coverage": get_documentation_coverage,
    "run_auto_documentation": run_auto_documentation,
    "get_full_project_code_and_docs": get_full_project_code_and_docs,
}


def get_mcp_tools_metadata() -> list:
    """Returns interactive schemas and descriptions for all registered MCP tools."""
    return [
        {
            "name": "clone_and_index_git",
            "display_name": "Clone & Index Git Repo",
            "category": "Ingest",
            "description": "Clones a remote Git repository, parses its AST structure, generates vector embeddings, and builds the Knowledge Graph.",
            "color": "sky",
            "parameters": [
                {
                    "name": "git_url",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "placeholder": "https://github.com/fastapi/fastapi.git",
                    "description": "HTTPS or SSH URL of the git repository to clone"
                },
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "fastapi (optional)",
                    "description": "Custom name for the repository in graph"
                },
                {
                    "name": "branch",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "main or master (optional)",
                    "description": "Git branch to checkout"
                },
                {
                    "name": "token",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "ghp_... (optional)",
                    "description": "Personal access token for private repos"
                }
            ]
        },
        {
            "name": "analyze_change_impact",
            "display_name": "Blast Radius & Impact Analysis",
            "category": "Safety",
            "description": "Calculates downstream Blast Radius before modifying a function: reports cascading callers up to 3 degrees away and list of affected enterprise files.",
            "color": "rose",
            "parameters": [
                {
                    "name": "function_name",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "placeholder": "e.g. get_embedding or sanitize_name",
                    "description": "Target function name to evaluate"
                },
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "Repository name filter (optional)",
                    "description": "Filter by repository name"
                }
            ]
        },
        {
            "name": "search_code_graph",
            "display_name": "Semantic Graph Search",
            "category": "Search",
            "description": "Semantically searches the codebase knowledge graph for relevant functions and classes using vector embeddings + graph relations.",
            "color": "emerald",
            "parameters": [
                {
                    "name": "query",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "placeholder": "e.g. JWT token authentication or database session",
                    "description": "Natural language or code query"
                },
                {
                    "name": "top_k",
                    "type": "integer",
                    "required": False,
                    "default": 5,
                    "placeholder": "5",
                    "description": "Number of matching code entities to return (1-25)"
                },
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "Repository filter (optional)",
                    "description": "Repository filter"
                }
            ]
        },
        {
            "name": "get_function_details",
            "display_name": "Function Details & AST",
            "category": "Inspect",
            "description": "Retrieves full source code, docstrings, callers, and callees for a specific function.",
            "color": "emerald",
            "parameters": [
                {
                    "name": "function_name",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "placeholder": "e.g. run_auto_documentation",
                    "description": "Name of function or method"
                },
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "Repository filter (optional)",
                    "description": "Repository filter"
                }
            ]
        },
        {
            "name": "find_callers",
            "display_name": "Trace Inbound Callers",
            "category": "Traverse",
            "description": "Traces all functions and files that call a given symbol across the codebase.",
            "color": "amber",
            "parameters": [
                {
                    "name": "function_name",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "placeholder": "e.g. sanitize_name",
                    "description": "Target function to trace callers for"
                },
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "Repository filter (optional)",
                    "description": "Repository filter"
                }
            ]
        },
        {
            "name": "get_documentation_coverage",
            "display_name": "Documentation Coverage",
            "category": "Docs",
            "description": "Computes documentation percentage of the codebase and lists undocumented functions.",
            "color": "blue",
            "parameters": [
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "Repository filter (optional)",
                    "description": "Repository filter"
                }
            ]
        },
        {
            "name": "run_auto_documentation",
            "display_name": "Run Auto-Docstrings",
            "category": "Docs",
            "description": "Automatically generates missing technical docstrings using LLM and updates graph nodes.",
            "color": "purple",
            "parameters": [
                {
                    "name": "limit",
                    "type": "integer",
                    "required": False,
                    "default": 10,
                    "placeholder": "10",
                    "description": "Max number of functions to document (1-50)"
                },
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "Repository filter (optional)",
                    "description": "Repository filter"
                }
            ]
        },
        {
            "name": "get_full_project_code_and_docs",
            "display_name": "Export Full Project Code & Docs",
            "category": "Docs",
            "description": "Extracts the complete codebase dossier, classes, methods, docstrings, and structure.",
            "color": "indigo",
            "parameters": [
                {
                    "name": "max_files",
                    "type": "integer",
                    "required": False,
                    "default": 50,
                    "placeholder": "50",
                    "description": "Maximum files to export (1-200)"
                },
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "Repository filter (optional)",
                    "description": "Repository filter"
                }
            ]
        },
        {
            "name": "detect_code_smells",
            "display_name": "Detect Code Smells & Cycles",
            "category": "Audit",
            "description": "Detects circular dependencies, bottleneck hub functions, and dead code candidates.",
            "color": "purple",
            "parameters": [
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "Repository filter (optional)",
                    "description": "Repository filter"
                }
            ]
        },
        {
            "name": "review_code_diff",
            "display_name": "Architectural PR Diff Review",
            "category": "Review",
            "description": "Analyzes unified git diff against the Knowledge Graph to produce an architectural risk assessment.",
            "color": "indigo",
            "parameters": [
                {
                    "name": "diff_text",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "placeholder": "diff --git a/app.py b/app.py...",
                    "description": "Unified Git diff text"
                },
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "Repository filter (optional)",
                    "description": "Repository filter"
                }
            ]
        },
        {
            "name": "ingest_code_snippet",
            "display_name": "Ingest Code Snippet",
            "category": "Ingest",
            "description": "Immediately parses and indexes a single source file or snippet into the Knowledge Graph.",
            "color": "sky",
            "parameters": [
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "placeholder": "my-project",
                    "description": "Target repository name"
                },
                {
                    "name": "file_path",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "placeholder": "services/auth.py",
                    "description": "Relative file path"
                },
                {
                    "name": "code_content",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "placeholder": "def login(): ...",
                    "description": "Full source code text"
                }
            ]
        },
        {
            "name": "index_local_repository",
            "display_name": "Index Local Directory",
            "category": "Ingest",
            "description": "Scans and indexes a directory on disk within the sandboxed repository folder.",
            "color": "sky",
            "parameters": [
                {
                    "name": "directory_path",
                    "type": "string",
                    "required": True,
                    "default": "",
                    "placeholder": "repos/my-project",
                    "description": "Directory path inside allowed sandbox"
                },
                {
                    "name": "repo_name",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "placeholder": "my-project (optional)",
                    "description": "Identifier for the repository"
                }
            ]
        },
        {
            "name": "get_codebase_stats",
            "display_name": "Codebase Knowledge Overview",
            "category": "Inspect",
            "description": "Returns counts of repositories, files, classes, functions, and graph relationships.",
            "color": "emerald",
            "parameters": []
        },
        {
            "name": "list_repositories",
            "display_name": "List All Repositories",
            "category": "Inspect",
            "description": "Lists all indexed repositories with their file counts and languages.",
            "color": "emerald",
            "parameters": []
        }
    ]


def execute_mcp_tool(tool_name: str, arguments: dict = None) -> dict:
    """Executes a registered MCP tool dynamically with argument validation."""
    arguments = arguments or {}
    tool_fn = MCP_TOOLS.get(tool_name)
    if not tool_fn:
        raise ValueError(f"MCP tool '{tool_name}' not found. Available tools: {list(MCP_TOOLS.keys())}")

    import inspect
    sig = inspect.signature(tool_fn)
    valid_args = {}
    for p_name, p in sig.parameters.items():
        if p_name in arguments and arguments[p_name] is not None and str(arguments[p_name]).strip() != "":
            val = arguments[p_name]
            if p.annotation == int or p.annotation == Optional[int]:
                try:
                    valid_args[p_name] = int(val)
                except (ValueError, TypeError):
                    valid_args[p_name] = val
            elif p.annotation == bool or p.annotation == Optional[bool]:
                valid_args[p_name] = str(val).lower() in ("true", "1", "yes")
            else:
                valid_args[p_name] = str(val).strip()
        elif p.default is inspect.Parameter.empty:
            raise ValueError(f"Missing required parameter '{p_name}' for MCP tool '{tool_name}'")

    result = tool_fn(**valid_args)
    return {
        "tool_name": tool_name,
        "arguments": valid_args,
        "result": result
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CodeGraph Brain MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio", help="MCP transport mode")
    parser.add_argument("--host", default="0.0.0.0", help="Host for SSE transport")
    parser.add_argument("--port", type=int, default=8002, help="Port for SSE transport")
    args = parser.parse_args()

    if args.transport == "sse":
        print(f"Starting CodeGraph Brain MCP Server on SSE http://{args.host}:{args.port}")
        if hasattr(mcp, "settings"):
            mcp.settings.host = args.host
            mcp.settings.port = args.port
        mcp.run(transport="sse")
    else:
        # Default: stdio for Claude Desktop / Cursor / IDE Agents
        mcp.run(transport="stdio")
