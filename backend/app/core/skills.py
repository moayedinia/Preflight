import os
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

from app.core.security import ALLOWED_REPO_DIR, sanitize_name, validate_safe_path
from app.core.preflight import (
    get_candidate_directories,
    audit_single_tier,
    run_3tier_preflight_evaluation,
    compile_pure_english_markdown_report,
    RED_SYSTEM_PROMPT,
    ORANGE_SYSTEM_PROMPT,
    YELLOW_SYSTEM_PROMPT,
)

# Supported source file extensions for code reading skills
CODE_EXTENSIONS = {
    ".cs", ".csx", ".razor", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".html", ".vue", ".go", ".java", ".php", ".rb", ".rs",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".swift", ".kt", ".scala",
    ".json", ".yaml", ".yml", ".md", ".sh", ".bash", ".sql"
}

IGNORED_DIRECTORIES = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
    ".idea", ".vscode", "dist", "build", ".gemini", ".system_generated",
    ".cache", "tmp", "logs"
}


# ==================== 1. CODE READING SKILLS ====================

def skill_read_file(
    repo_name: Optional[str],
    file_path: str,
    start_line: int = 1,
    end_line: int = 150
) -> Dict[str, Any]:
    """
    Code Reading Skill: Safely reads a file or line range from the target repository.
    Enforces security boundaries against directory traversal.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    clean_path = (file_path or "").strip().replace("\\", "/")
    
    if not clean_path:
        return {
            "success": False,
            "error": "File path is required. Example: /read Program.cs",
            "markdown": "⚠️ **Error:** Please specify a relative file path to read (e.g. `/read Program.cs` or `/read app/main.py`)."
        }

    candidates = get_candidate_directories(clean_repo)
    target_file: Optional[Path] = None

    for base_dir in candidates:
        candidate_path = (base_dir / clean_path).resolve()
        try:
            # Ensure path does not escape candidate directory
            candidate_path.relative_to(base_dir.resolve())
            if candidate_path.exists() and candidate_path.is_file():
                target_file = candidate_path
                break
        except (ValueError, Exception):
            continue

    # Fallback search by filename if exact relative path wasn't found
    if not target_file:
        query_fname = Path(clean_path).name.lower()
        for base_dir in candidates:
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if d not in IGNORED_DIRECTORIES and not d.startswith(".")]
                for f in files:
                    if f.lower() == query_fname:
                        found_path = (Path(root) / f).resolve()
                        try:
                            found_path.relative_to(base_dir.resolve())
                            target_file = found_path
                            break
                        except Exception:
                            pass
                if target_file:
                    break
            if target_file:
                break

    if not target_file:
        return {
            "success": False,
            "error": f"File '{clean_path}' was not found in repository.",
            "markdown": f"⚠️ **File not found:** `{clean_path}` was not located in the target project.\n\n"
                        f"Tip: Try specifying the full relative path, or use `/search {Path(clean_path).name}` to locate it."
        }

    try:
        content = target_file.read_text(encoding="utf-8", errors="ignore")
        all_lines = content.splitlines()
        total_lines = len(all_lines)

        s_line = max(1, int(start_line or 1))
        e_line = min(total_lines, max(s_line, int(end_line or (s_line + 150))))
        selected_lines = all_lines[s_line - 1 : e_line]

        # Determine syntax highlighting language
        ext = target_file.suffix.lower()
        lang_map = {
            ".py": "python", ".cs": "csharp", ".js": "javascript",
            ".ts": "typescript", ".jsx": "jsx", ".tsx": "tsx",
            ".json": "json", ".html": "html", ".razor": "html",
            ".go": "go", ".rs": "rust", ".java": "java",
            ".md": "markdown", ".sql": "sql", ".yml": "yaml", ".yaml": "yaml"
        }
        lang = lang_map.get(ext, "")

        numbered_code = "\n".join([f"{idx:4d} | {line}" for idx, line in enumerate(selected_lines, start=s_line)])

        rel_name = clean_path
        for base_dir in candidates:
            try:
                rel_name = target_file.relative_to(base_dir).as_posix()
                break
            except Exception:
                pass

        md = f"""### 📄 Source File: `{rel_name}`
- **Lines Displayed:** `{s_line}` to `{e_line}` of `{total_lines}`
- **Extension:** `{ext}`

```{lang}
{numbered_code}
```
"""
        return {
            "success": True,
            "file_path": rel_name,
            "start_line": s_line,
            "end_line": e_line,
            "total_lines": total_lines,
            "code": "\n".join(selected_lines),
            "markdown": md
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "markdown": f"⚠️ **Error reading file `{clean_path}`:** {str(e)}"
        }


def skill_find_symbol(
    repo_name: Optional[str],
    symbol_name: str
) -> Dict[str, Any]:
    """
    Code Reading Skill: Finds definition, docstring, and occurrences of a function,
    class, or interface symbol across repository source code and Knowledge Graph.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    clean_symbol = (symbol_name or "").strip()

    if not clean_symbol:
        return {
            "success": False,
            "error": "Symbol name is required. Example: /symbol AuthController",
            "markdown": "⚠️ **Error:** Please provide a symbol name (e.g. `/symbol processPayment` or `/symbol UserStore`)."
        }

    # 1. Try Neo4j Knowledge Graph first if available
    graph_records = []
    try:
        from app.core.graph import driver
        with driver.session() as session:
            q = """
            MATCH (fn:Function)
            WHERE (fn.name = $name OR fn.name ENDS WITH $name)
              AND ($repo IS NULL OR fn.repo = $repo)
            OPTIONAL MATCH (c:Class)-[:HAS_METHOD]->(fn)
            OPTIONAL MATCH (f:File)-[:CONTAINS]->(fn)
            OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
            RETURN fn, c.name AS class_name, f.path AS file_path,
                   COLLECT(DISTINCT caller.name) AS callers
            LIMIT 5
            """
            graph_records = list(session.run(q, name=clean_symbol, repo=clean_repo))
    except Exception:
        graph_records = []

    if graph_records:
        md_parts = [f"### 🔍 Symbol Definition for `{clean_symbol}` (From Knowledge Graph)\n"]
        for r in graph_records:
            fn = r["fn"]
            callers_str = ", ".join(r.get("callers", [])) or "None"
            md_parts.append(
                f"- **Function:** `{fn.get('name')}`\n"
                f"- **File:** `{r.get('file_path')}` (Lines {fn.get('start_line', 1)}-{fn.get('end_line', 1)})\n"
                f"- **Class:** `{r.get('class_name') or 'Standalone'}`\n"
                f"- **Inbound Callers:** {callers_str}\n"
                f"```text\n{str(fn.get('code', ''))[:1500]}\n```\n"
            )
        return {
            "success": True,
            "symbol": clean_symbol,
            "source": "knowledge_graph",
            "matches": len(graph_records),
            "markdown": "\n".join(md_parts)
        }

    # 2. Disk AST & Regex Search fallback
    candidates = get_candidate_directories(clean_repo)
    symbol_pat = re.compile(
        rf'\b(?:def|class|function|interface|record|enum|async\s+function)\s+{re.escape(clean_symbol)}\b|'
        rf'\b(?:public|private|protected|internal|static)?\s*(?:async\s+)?[\w<>[\]]+\s+{re.escape(clean_symbol)}\s*\(',
        re.IGNORECASE
    )

    matches = []
    for base_dir in candidates:
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRECTORIES and not d.startswith(".")]
            for f in files:
                ext = Path(f).suffix.lower()
                if ext not in CODE_EXTENSIONS:
                    continue
                file_path = Path(root) / f
                try:
                    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    continue

                for line_idx, line in enumerate(lines, start=1):
                    if symbol_pat.search(line):
                        try:
                            rel_p = file_path.relative_to(base_dir).as_posix()
                        except Exception:
                            rel_p = f
                        snippet = "\n".join(lines[max(0, line_idx - 1) : min(len(lines), line_idx + 15)])
                        matches.append({
                            "file": rel_p,
                            "line": line_idx,
                            "declaration": line.strip(),
                            "snippet": snippet
                        })
                        if len(matches) >= 5:
                            break
                if len(matches) >= 5:
                    break
        if len(matches) >= 5:
            break

    if not matches:
        return {
            "success": False,
            "error": f"Symbol '{clean_symbol}' not found in codebase.",
            "markdown": f"ℹ️ **Symbol `{clean_symbol}` was not found** in scanned project files.\n\n"
                        f"Tip: Check the exact casing or use `/search {clean_symbol}` to scan for references."
        }

    md_lines = [f"### 🔍 Found {len(matches)} Declaration(s) for `{clean_symbol}`:\n"]
    for m in matches:
        md_lines.append(f"#### `{m['file']}:{m['line']}`")
        md_lines.append(f"```text\n{m['snippet']}\n```\n")

    return {
        "success": True,
        "symbol": clean_symbol,
        "source": "ast_code_scan",
        "matches": len(matches),
        "data": matches,
        "markdown": "\n".join(md_lines)
    }


def skill_search_code(
    repo_name: Optional[str],
    query: str,
    is_regex: bool = False,
    max_matches: int = 15
) -> Dict[str, Any]:
    """
    Code Reading Skill: Greps across project files for occurrences of text or regex patterns.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    q = (query or "").strip()

    if not q:
        return {
            "success": False,
            "error": "Query pattern is required. Example: /search execute",
            "markdown": "⚠️ **Error:** Please provide a search pattern (e.g. `/search api_key` or `/search TODO`)."
        }

    candidates = get_candidate_directories(clean_repo)
    flags = re.IGNORECASE
    try:
        pat = re.compile(q if is_regex else re.escape(q), flags)
    except Exception as err:
        return {
            "success": False,
            "error": f"Invalid regular expression: {str(err)}",
            "markdown": f"⚠️ **Invalid Search Pattern:** {str(err)}"
        }

    results = []
    total_files_scanned = 0

    for base_dir in candidates:
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRECTORIES and not d.startswith(".")]
            for f in files:
                ext = Path(f).suffix.lower()
                if ext not in CODE_EXTENSIONS:
                    continue
                file_path = Path(root) / f
                total_files_scanned += 1
                try:
                    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    continue

                for line_idx, line in enumerate(lines, start=1):
                    if pat.search(line):
                        try:
                            rel_p = file_path.relative_to(base_dir).as_posix()
                        except Exception:
                            rel_p = f
                        results.append({
                            "file": rel_p,
                            "line": line_idx,
                            "match": line.strip()[:140]
                        })
                        if len(results) >= max_matches:
                            break
                if len(results) >= max_matches:
                    break
        if len(results) >= max_matches:
            break

    if not results:
        return {
            "success": True,
            "query": q,
            "matches_count": 0,
            "markdown": f"ℹ️ No occurrences of `{q}` found across `{total_files_scanned}` scanned source files."
        }

    md_lines = [
        f"### 🔎 Search Results for `{q}`",
        f"- **Files Scanned:** `{total_files_scanned}`",
        f"- **Matches Found:** `{len(results)}`\n"
    ]
    for r in results:
        md_lines.append(f"- **`{r['file']}:{r['line']}`**: `{r['match']}`")

    return {
        "success": True,
        "query": q,
        "matches_count": len(results),
        "results": results,
        "markdown": "\n".join(md_lines)
    }


def skill_inspect_routes(repo_name: Optional[str]) -> Dict[str, Any]:
    """
    Code Reading Skill: Scans codebase for all API route handlers, HTTP methods,
    and inspects whether server-side authentication guards and try/catch boundaries are present.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    candidates = get_candidate_directories(clean_repo)

    # Patterns for various frameworks
    re_py_route = re.compile(r'@(?:app|router)\.(get|post|put|delete|patch|options|head)\s*\(\s*([\'"][^\'"]*[\'"])', re.IGNORECASE)
    re_dotnet_route = re.compile(r'\[(HttpGet|HttpPost|HttpPut|HttpDelete|HttpPatch|Route)\s*(?:\(\s*([\'"][^\'"]*[\'"])?\s*\))?\]', re.IGNORECASE)
    re_dotnet_minimal = re.compile(r'app\.Map(Get|Post|Put|Delete)\s*\(\s*([\'"][^\'"]*[\'"])', re.IGNORECASE)
    re_js_route = re.compile(r'(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*([\'"][^\'"]*[\'"])', re.IGNORECASE)

    endpoints = []

    for base_dir in candidates:
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRECTORIES and not d.startswith(".")]
            for f in files:
                file_path = Path(root) / f
                ext = file_path.suffix.lower()
                if ext not in {".py", ".cs", ".js", ".ts", ".jsx", ".tsx", ".go"}:
                    continue
                try:
                    rel_p = file_path.relative_to(base_dir).as_posix()
                except Exception:
                    rel_p = f
                try:
                    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    continue

                for idx, line in enumerate(lines, start=1):
                    line_str = line.strip()
                    if not line_str or line_str.startswith("//") or line_str.startswith("#"):
                        continue

                    # Python routes
                    m_py = re_py_route.search(line_str)
                    if m_py:
                        method = m_py.group(1).upper()
                        path = m_py.group(2).strip("'\"")
                        context = "\n".join(lines[idx:idx + 15])
                        has_auth = bool(re.search(r'Depends\s*\([^)]*auth|verify_api_key|Security\(', context, re.IGNORECASE))
                        has_try = "try:" in context
                        endpoints.append({
                            "method": method,
                            "path": path,
                            "file": rel_p,
                            "line": idx,
                            "has_auth": has_auth,
                            "has_try_catch": has_try
                        })

                    # .NET / C# routes
                    m_cs = re_dotnet_route.search(line_str)
                    if m_cs:
                        method = m_cs.group(1).upper().replace("HTTP", "")
                        path = (m_cs.group(2) or "").strip("'\"") or "/"
                        context = "\n".join(lines[max(0, idx - 5):idx + 15])
                        has_auth = "[Authorize" in context
                        has_try = "try" in context and "catch" in context
                        endpoints.append({
                            "method": method,
                            "path": path,
                            "file": rel_p,
                            "line": idx,
                            "has_auth": has_auth,
                            "has_try_catch": has_try
                        })

                    # .NET Minimal API
                    m_min = re_dotnet_minimal.search(line_str)
                    if m_min:
                        method = m_min.group(1).upper()
                        path = m_min.group(2).strip("'\"")
                        context = "\n".join(lines[idx:idx + 10])
                        has_auth = "RequireAuthorization" in context
                        has_try = "try" in context
                        endpoints.append({
                            "method": method,
                            "path": path,
                            "file": rel_p,
                            "line": idx,
                            "has_auth": has_auth,
                            "has_try_catch": has_try
                        })

                    # Express / Fastify / Node routes
                    m_js = re_js_route.search(line_str)
                    if m_js:
                        method = m_js.group(1).upper()
                        path = m_js.group(2).strip("'\"")
                        context = "\n".join(lines[idx:idx + 15])
                        has_auth = bool(re.search(r'auth|jwt|bearer|passport|requireAuth', context, re.IGNORECASE))
                        has_try = "try" in context and "catch" in context
                        endpoints.append({
                            "method": method,
                            "path": path,
                            "file": rel_p,
                            "line": idx,
                            "has_auth": has_auth,
                            "has_try_catch": has_try
                        })

    if not endpoints:
        return {
            "success": True,
            "endpoints_count": 0,
            "markdown": "ℹ️ No API route annotations (`@app.get`, `[HttpGet]`, `app.MapGet`) detected in scanned files."
        }

    md_lines = [
        f"### 🌐 Discovered API Route Endpoints ({len(endpoints)})",
        "",
        "| Method | Route Path | File & Line | Auth Guard | Try/Catch |",
        "| :--- | :--- | :--- | :--- | :--- |"
    ]
    for ep in endpoints[:35]:
        auth_badge = "🛡️ Yes" if ep["has_auth"] else "⚠️ Unprotected"
        try_badge = "✅ Protected" if ep["has_try_catch"] else "⚠️ Missing"
        md_lines.append(f"| `{ep['method']}` | `{ep['path']}` | `{ep['file']}:{ep['line']}` | {auth_badge} | {try_badge} |")

    return {
        "success": True,
        "endpoints_count": len(endpoints),
        "endpoints": endpoints,
        "markdown": "\n".join(md_lines)
    }


# ==================== 2. REPORTING & AUDIT SKILLS ====================

def skill_audit_tier(
    tier: str,
    repo_name: Optional[str]
) -> Dict[str, Any]:
    """
    Arbiter Audit Skill: Executes the exact user-defined system prompt and rules
    for RED, ORANGE, or YELLOW preflight tiers.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    tier_clean = (tier or "red").lower()

    eval_result = audit_single_tier(tier=tier_clean, repo_name=clean_repo)
    return {
        "success": True,
        "tier": tier_clean,
        "title": eval_result.get("title", f"{tier_clean.upper()} Audit"),
        "is_defect": eval_result.get("is_defect", False),
        "defect_count": eval_result.get("defect_count", 0),
        "plain_english_explanation": eval_result.get("plain_english_explanation", ""),
        "business_consequence": eval_result.get("business_consequence", ""),
        "repair_prompt": eval_result.get("repair_prompt", ""),
        "defects": eval_result.get("defects", []),
        "markdown": eval_result.get("markdown_report", "")
    }


def skill_executive_report(repo_name: Optional[str]) -> Dict[str, Any]:
    """
    Reporting Skill: Compiles the 100% pure English Executive Preflight & Architecture Report.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    eval_results = run_3tier_preflight_evaluation(repo_name=clean_repo)

    analytics = {}
    try:
        from app.core.analytics import detect_code_smells
        analytics = detect_code_smells(clean_repo)
    except Exception:
        analytics = {}

    md = compile_pure_english_markdown_report(eval_results, analytics)
    return {
        "success": True,
        "repository": clean_repo or "Current Project",
        "evaluations": eval_results,
        "markdown": md
    }


def skill_arch_diagram(repo_name: Optional[str]) -> Dict[str, Any]:
    """
    Reporting Skill: Generates a Mermaid architecture flowchart showing high-risk hubs,
    circular call chains, and module dependencies.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    
    analytics = {}
    try:
        from app.core.analytics import detect_code_smells
        analytics = detect_code_smells(clean_repo)
    except Exception:
        analytics = {}

    cycles = analytics.get("circular_dependencies") or []
    hubs = analytics.get("high_risk_hubs") or analytics.get("critical_hubs") or []
    dead = analytics.get("dead_code_candidates") or []

    mermaid_lines = [
        "```mermaid",
        "flowchart TD",
        "    classDef hub fill:#f59e0b,stroke:#b45309,stroke-width:2px,color:#000",
        "    classDef cycle fill:#ef4444,stroke:#991b1b,stroke-width:2px,color:#fff",
        "    classDef clean fill:#10b981,stroke:#047857,stroke-width:1px,color:#fff",
        "    classDef dead fill:#64748b,stroke:#334155,stroke-width:1px,color:#fff"
    ]

    # Add repository root
    repo_label = clean_repo or "App Root"
    mermaid_lines.append(f'    Root["🏛️ {repo_label}"]:::clean')

    if hubs:
        for idx, h in enumerate(hubs[:6], start=1):
            name = h.get("name", f"Hub{idx}")
            risk = h.get("risk_score", 0)
            mermaid_lines.append(f'    H{idx}["⚠️ God Function: {name}\\nRisk: {risk}"]:::hub')
            mermaid_lines.append(f'    Root --> H{idx}')
    else:
        mermaid_lines.append('    NoHubs["✅ No Bottleneck Hubs"]:::clean')
        mermaid_lines.append('    Root --> NoHubs')

    if cycles:
        for cidx, c in enumerate(cycles[:4], start=1):
            chain = c.get("cycle", [])
            if len(chain) >= 2:
                n1 = f"C{cidx}_1"
                n2 = f"C{cidx}_2"
                mermaid_lines.append(f'    {n1}["🔴 {chain[0]}"]:::cycle')
                mermaid_lines.append(f'    {n2}["🔴 {chain[1]}"]:::cycle')
                mermaid_lines.append(f'    {n1} <--> {n2}')
                mermaid_lines.append(f'    Root --> {n1}')
    else:
        mermaid_lines.append('    NoCycles["🟢 Zero Dependency Cycles"]:::clean')
        mermaid_lines.append('    Root --> NoCycles')

    if dead:
        mermaid_lines.append(f'    DeadBox["💤 Dead Code: {len(dead)} Unused Functions"]:::dead')
        mermaid_lines.append('    Root --> DeadBox')

    mermaid_lines.append("```")
    mermaid_str = "\n".join(mermaid_lines)

    md = f"""### 🏛️ Architectural Graph & Risk Topology: `{clean_repo or 'Project'}`

{mermaid_str}

**Summary:**
- **Circular Chains:** `{len(cycles)}` detected
- **High-Risk Hubs (God Functions):** `{len(hubs)}` detected
- **Dead Code Candidates:** `{len(dead)}` detected
"""
    return {
        "success": True,
        "repository": clean_repo or "Current Project",
        "mermaid": mermaid_str,
        "markdown": md
    }


def skill_repair_dossier(repo_name: Optional[str]) -> Dict[str, Any]:
    """
    Reporting Skill: Compiles copy-ready AI Fix Prompts tailored for Cursor,
    Claude, Lovable, and GitHub Copilot for all detected defects across tiers.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    eval_results = run_3tier_preflight_evaluation(repo_name=clean_repo)

    red = eval_results.get("red_defects", {})
    orange = eval_results.get("orange_defects", {})
    yellow = eval_results.get("yellow_defects", {})

    dossier_lines = [
        f"# 🛠️ Preflight AI Repair Prompts Dossier: `{clean_repo or 'Project'}`",
        "",
        "Copy and paste any of the tool-tailored prompt blocks below directly into **Cursor Composer**, **Claude**, **Lovable**, or **GitHub Copilot** to safely repair detected defects.",
        "",
        "---",
        ""
    ]

    # Tier 1: Red
    dossier_lines.append("## 🔴 1. Critical Blocker Repair Prompt (Security & Crashes)")
    dossier_lines.append(f"> **Issue:** {red.get('plain_english_explanation', 'No blockers')}")
    dossier_lines.append(f"> **Consequence:** {red.get('business_consequence', 'None')}")
    dossier_lines.append("")
    dossier_lines.append("```text")
    dossier_lines.append(red.get("repair_prompt", "No red repairs required."))
    dossier_lines.append("```")
    dossier_lines.append("")

    # Tier 2: Orange
    dossier_lines.append("## 🟠 2. Operational UX & Reliability Repair Prompt")
    dossier_lines.append(f"> **Issue:** {orange.get('plain_english_explanation', 'No operational hazards')}")
    dossier_lines.append(f"> **Consequence:** {orange.get('business_consequence', 'None')}")
    dossier_lines.append("")
    dossier_lines.append("```text")
    dossier_lines.append(orange.get("repair_prompt", "No orange repairs required."))
    dossier_lines.append("```")
    dossier_lines.append("")

    # Tier 3: Yellow
    dossier_lines.append("## 🟡 3. Code Hygiene & Polish Repair Prompt")
    dossier_lines.append(f"> **Issue:** {yellow.get('plain_english_explanation', 'Code is clean')}")
    dossier_lines.append(f"> **Consequence:** {yellow.get('business_consequence', 'None')}")
    dossier_lines.append("")
    dossier_lines.append("```text")
    dossier_lines.append(yellow.get("repair_prompt", "No yellow repairs required."))
    dossier_lines.append("```")

    md = "\n".join(dossier_lines)
    return {
        "success": True,
        "repository": clean_repo or "Current Project",
        "markdown": md
    }


# ==================== 3. SKILLS REGISTRY & DISPATCHER ====================

SKILLS_REGISTRY = {
    "read_file": {
        "id": "read_file",
        "name": "Read File / Code Slice",
        "category": "Code Reading",
        "icon": "file-code",
        "description": "Read any file or line range with line numbers and syntax highlighting",
        "command": "/read",
        "placeholder": "e.g. Program.cs or app/main.py",
        "quick_run": False,
        "handler": skill_read_file
    },
    "find_symbol": {
        "id": "find_symbol",
        "name": "Find Symbol & AST",
        "category": "Code Reading",
        "icon": "search-code",
        "description": "Locate function/class definitions, docstrings, callers, and callees",
        "command": "/symbol",
        "placeholder": "e.g. AuthController or processPayment",
        "quick_run": False,
        "handler": skill_find_symbol
    },
    "search_code": {
        "id": "search_code",
        "name": "Search Codebase",
        "category": "Code Reading",
        "icon": "file-search",
        "description": "Grep across project files for patterns, raw queries, or secrets",
        "command": "/search",
        "placeholder": "e.g. api_key, execute, or TODO",
        "quick_run": False,
        "handler": skill_search_code
    },
    "inspect_routes": {
        "id": "inspect_routes",
        "name": "Inspect API Routes",
        "category": "Code Reading",
        "icon": "network",
        "description": "Scan all API endpoints, controllers, HTTP methods, and auth guards",
        "command": "/routes",
        "placeholder": "",
        "quick_run": True,
        "handler": lambda repo, **kwargs: skill_inspect_routes(repo)
    },
    "audit_red": {
        "id": "audit_red",
        "name": "🔴 Audit Red Blockers",
        "category": "Arbiter Audits",
        "icon": "shield-alert",
        "description": "Security & Crash Arbiter evaluation (leaked keys, unprotected admin, SQL injection, 500 crashes)",
        "command": "/audit-red",
        "placeholder": "",
        "quick_run": True,
        "handler": lambda repo, **kwargs: skill_audit_tier("red", repo)
    },
    "audit_orange": {
        "id": "audit_orange",
        "name": "🟠 Audit Orange UX",
        "category": "Arbiter Audits",
        "icon": "alert-triangle",
        "description": "UX & Reliability Arbiter evaluation (missing loading states, unbounded inputs, un-debounced search)",
        "command": "/audit-orange",
        "placeholder": "",
        "quick_run": True,
        "handler": lambda repo, **kwargs: skill_audit_tier("orange", repo)
    },
    "audit_yellow": {
        "id": "audit_yellow",
        "name": "🟡 Audit Yellow Quality",
        "category": "Arbiter Audits",
        "icon": "sparkles",
        "description": "Code Quality Arbiter evaluation (stray console.log, contrast/a11y, README completeness)",
        "command": "/audit-yellow",
        "placeholder": "",
        "quick_run": True,
        "handler": lambda repo, **kwargs: skill_audit_tier("yellow", repo)
    },
    "executive_report": {
        "id": "executive_report",
        "name": "📊 Executive Audit Report",
        "category": "Reporting",
        "icon": "file-bar-chart-2",
        "description": "Comprehensive 3-Tier Preflight + Architecture Health report in Markdown",
        "command": "/report",
        "placeholder": "",
        "quick_run": True,
        "handler": lambda repo, **kwargs: skill_executive_report(repo)
    },
    "arch_diagram": {
        "id": "arch_diagram",
        "name": "🏛️ Architecture Diagram",
        "category": "Reporting",
        "icon": "workflow",
        "description": "Generate Mermaid architecture graph with dependency chains & hubs",
        "command": "/diagram",
        "placeholder": "",
        "quick_run": True,
        "handler": lambda repo, **kwargs: skill_arch_diagram(repo)
    },
    "repair_dossier": {
        "id": "repair_dossier",
        "name": "🛠️ AI Fix Prompts Dossier",
        "category": "Reporting",
        "icon": "wrench",
        "description": "Generate copy-ready repair prompts tailored for Cursor, Claude, Lovable",
        "command": "/dossier",
        "placeholder": "",
        "quick_run": True,
        "handler": lambda repo, **kwargs: skill_repair_dossier(repo)
    }
}


def get_available_skills_metadata() -> List[Dict[str, Any]]:
    """Returns list of skill schemas without the callable handler for API / UI clients."""
    return [
        {k: v for k, v in meta.items() if k != "handler"}
        for meta in SKILLS_REGISTRY.values()
    ]


def dispatch_skill_or_command(
    raw_input: str,
    repo_name: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """
    Parses a user chat message or slash command. If it matches a skill, executes it.
    Returns None if the message is a standard conversational prompt.
    """
    text = (raw_input or "").strip()
    params = params or {}

    # 1. Match by slash command or direct skill ID
    skill_key = None
    args_str = ""

    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        args_str = parts[1].strip() if len(parts) > 1 else ""
        for s_id, meta in SKILLS_REGISTRY.items():
            if meta["command"] == cmd or f"/{s_id}" == cmd:
                skill_key = s_id
                break
    elif text in SKILLS_REGISTRY:
        skill_key = text
    else:
        # Check natural language triggers
        lower = text.lower()
        if lower in ("inspect routes", "show routes", "list routes", "get routes", "api routes"):
            skill_key = "inspect_routes"
        elif lower in ("audit red", "red audit", "check red blockers", "security audit"):
            skill_key = "audit_red"
        elif lower in ("audit orange", "orange audit", "check orange", "ux audit"):
            skill_key = "audit_orange"
        elif lower in ("audit yellow", "yellow audit", "check yellow", "hygiene audit"):
            skill_key = "audit_yellow"
        elif lower in ("generate report", "executive report", "full report", "download report", "preflight report"):
            skill_key = "executive_report"
        elif lower in ("architecture diagram", "show diagram", "arch diagram", "mermaid diagram"):
            skill_key = "arch_diagram"
        elif lower in ("fix dossier", "repair prompts", "fix prompts", "repair dossier"):
            skill_key = "repair_dossier"

    if not skill_key:
        return None

    meta = SKILLS_REGISTRY.get(skill_key)
    if not meta:
        return None

    handler = meta["handler"]

    # Parameter extraction
    if skill_key == "read_file":
        file_arg = args_str or params.get("file_path", "")
        # Check if line numbers provided (e.g. Program.cs 1-50 or Program.cs:1:50)
        s_line = params.get("start_line", 1)
        e_line = params.get("end_line", 150)
        if ":" in file_arg:
            sub = file_arg.split(":")
            file_arg = sub[0].strip()
            if len(sub) > 1 and sub[1].isdigit():
                s_line = int(sub[1])
            if len(sub) > 2 and sub[2].isdigit():
                e_line = int(sub[2])
        elif " " in file_arg:
            sub = file_arg.split()
            file_arg = sub[0].strip()
            if len(sub) > 1 and "-" in sub[1]:
                lr = sub[1].split("-")
                if lr[0].isdigit():
                    s_line = int(lr[0])
                if len(lr) > 1 and lr[1].isdigit():
                    e_line = int(lr[1])
        return handler(repo_name=repo_name, file_path=file_arg, start_line=s_line, end_line=e_line)

    elif skill_key == "find_symbol":
        sym_arg = args_str or params.get("symbol_name", "")
        return handler(repo_name=repo_name, symbol_name=sym_arg)

    elif skill_key == "search_code":
        q_arg = args_str or params.get("query", "")
        return handler(repo_name=repo_name, query=q_arg, is_regex=params.get("is_regex", False))

    else:
        return handler(repo=repo_name, **params)
