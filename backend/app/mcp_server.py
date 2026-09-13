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
        raise ImportError("FastMCP is required to run CodeGraph Brain MCP Server. Ensure 'mcp<2' is installed.")

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
    calculate_blast_radius
)
from app.core.repo_manager import list_all_repositories
from app.core.reviewer import generate_architectural_code_review

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
def clone_and_index_git_repository(
    git_url: str,
    repo_name: Optional[str] = None,
    branch: Optional[str] = None
) -> str:
    """
    Automated tool for AI agents: Clones a remote Git repository, parses its AST structure,
    generates vector embeddings, and builds the Knowledge Graph.
    
    Args:
        git_url: HTTPS or SSH URL of the git repository (e.g. 'https://github.com/fastapi/fastapi.git').
        repo_name: Optional name for the project.
        branch: Optional branch name (defaults to default branch).
    """
    try:
        from app.core.cloner import clone_git_repo
        cloned_path = clone_git_repo(git_url=git_url, repo_name=repo_name, branch=branch)
        actual_name = repo_name or cloned_path.name
        res = scan_and_ingest_directory(str(cloned_path), actual_name)
        return (
            f"Successfully cloned and indexed Git repository '{res['repo_name']}':\n"
            f"- Source URL: {git_url}\n"
            f"- Total Files Scanned: {res['total_files_scanned']}\n"
            f"- Files Parsed: {res['parsed_files']}\n"
            f"- Classes Indexed: {res['classes_indexed']}\n"
            f"- Functions Indexed: {res['functions_indexed']}\n"
            f"- Errors: {len(res['errors'])}"
        )
    except Exception as e:
        return f"Failed to clone and index repository: {str(e)}"

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
