import os
import sys
import argparse
from typing import Optional, List
from mcp.server.fastmcp import FastMCP

from app.core.graph import (
    driver,
    vector_search,
    expand_graph_context,
    get_graph_stats
)
from app.core.embedder import get_embedding
from app.core.scanner import scan_and_ingest_directory
from app.core.security import validate_safe_path, sanitize_name

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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CodeGraph Brain MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio", help="MCP transport mode")
    parser.add_argument("--host", default="0.0.0.0", help="Host for SSE transport")
    parser.add_argument("--port", type=int, default=8001, help="Port for SSE transport")
    args = parser.parse_args()

    if args.transport == "sse":
        print(f"Starting CodeGraph Brain MCP Server on SSE http://{args.host}:{args.port}")
        mcp.run(transport="sse")
    else:
        # Default: stdio for Claude Desktop / Cursor / IDE Agents
        mcp.run(transport="stdio")
