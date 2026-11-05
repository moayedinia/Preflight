import os
import re
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from app.core.embedder import get_embedding
from app.core.graph import vector_search, expand_graph_context

def classify_query_intent(query: str) -> str:
    """
    Classifies user query intent:
    - 'ARCHITECTURE': Structural overview, layering, authentication flow, modules, APIs.
    - 'IMPACT': Impact analysis, dependencies, callers, callees, risk of changing a function.
    - 'IMPLEMENTATION': Algorithmic details, bug fixing, internal implementation of a specific method.
    """
    q = (query or "").lower()

    impl_keywords = [
        "code", "implementation", "implement", "how it works internally",
        "debug", "refactor", "source", "syntax", "write", "algorithm",
        "bug", "error", "trace", "logic"
    ]
    if any(k in q for k in impl_keywords):
        return "IMPLEMENTATION"

    impact_keywords = [
        "blast radius", "impact", "who calls", "caller", "callee",
        "dependency", "depend", "change", "risk", "break", "breaking",
        "downstream", "upstream", "affect", "affected"
    ]
    if any(k in q for k in impact_keywords):
        return "IMPACT"

    return "ARCHITECTURE"

def extract_signature(raw_code: str, default_name: str = "") -> str:
    """
    Extracts a clean, single-line function signature from source code,
    accurately balancing parentheses across multiline signatures.
    """
    if not raw_code:
        return f"def {default_name}()"
    
    lines = raw_code.splitlines()
    sig_parts = []
    paren_depth = 0
    started = False

    for l in lines[:6]:
        s = l.strip()
        if not s:
            continue
        paren_depth += s.count("(") - s.count(")")
        sig_parts.append(s)
        if "(" in s:
            started = True
        if started and paren_depth <= 0 and (":" in s or "{" in s or "=>" in s or len(sig_parts) >= 2):
            break

    sig = " ".join(sig_parts)
    sig = re.sub(r'[\s:\{]+$', '', sig).strip()
    return sig[:220] if sig else f"def {default_name}()"

def serialize_graph_topology(hits: list[dict], relational_context: list[dict], intent: str) -> str:
    """
    Converts relational graph context into an ultra-compact, expressive Call Graph DAG & Contract block.
    Consumes ~100-250 tokens while delivering 100% architectural and topological precision.
    """
    lines = []

    # 1. Call Graph Paths (DAG)
    call_chains = []
    for rel in relational_context:
        fn_name = rel.get("function_name") or "Function"
        callers = rel.get("called_by") or []
        callees = rel.get("calls") or []

        for c in callers[:3]:
            call_chains.append(f"{c} ➔ {fn_name}")
        for c in callees[:3]:
            call_chains.append(f"{fn_name} ➔ {c}")

    if call_chains:
        lines.append("### 1. Call Graph Paths:")
        seen = set()
        for chain in call_chains:
            if chain not in seen:
                seen.add(chain)
                lines.append(f"  • {chain}")
        lines.append("")

    # 2. Entity Contracts (Signatures & Metadata)
    lines.append("### 2. Code Entity Contracts:")
    for i, hit in enumerate(hits[:4]):
        name = hit.get("name")
        file_path = hit.get("file_path", "")
        raw_code = (hit.get("code") or "").strip()
        doc = (hit.get("docstring") or "").strip()

        sig = extract_signature(raw_code, name)

        rel = next((r for r in relational_context if r.get("function_id") == hit.get("id")), {})
        callers_str = ", ".join(rel.get("called_by", [])[:3]) or "None (Root/Entrypoint)"
        callees_str = ", ".join(rel.get("calls", [])[:3]) or "None"
        parent_class = rel.get("class_name")

        lines.append(f"• **{name}** (`{file_path}`)")
        if parent_class:
            lines.append(f"  - Parent Class: `{parent_class}`")
        lines.append(f"  - Signature: `{sig}`")
        if doc:
            lines.append(f"  - Docstring: *{doc[:120]}*")
        lines.append(f"  - Inbound Callers: {callers_str} | Outbound Calls: {callees_str}")

        # Only inject a small focused implementation slice if the user specifically asked for implementation
        if intent == "IMPLEMENTATION" and i == 0 and raw_code:
            lines.append("  - Implementation Slice:")
            code_lines = [line for line in raw_code.splitlines()[:15] if line.strip()]
            code_slice = "\n    ".join(code_lines)
            lines.append(f"    ```\n    {code_slice}\n    ```")
        lines.append("")

    return "\n".join(lines).strip()

def collect_rag_repo_context(repo_name: Optional[str] = None, query: str = "", max_chars: int = 14000) -> str:
    """
    Collects rich, query-grounded codebase context for AI Copilot and GraphRAG:
    1. Resolves repository directory on disk (falling back to first indexed repo if unspecified).
    2. Constructs a concise project file tree (showing file hierarchy and tech stack).
    3. Excerpts key project manifests (package.json, requirements.txt, .csproj, etc.).
    4. Dynamically scores and excerpts the source files most relevant to the user's query.
    """
    from app.core.security import ALLOWED_REPO_DIR, sanitize_name
    from app.core.preflight import PREFLIGHT_IGNORED_DIRS

    target_dir = None
    target_repo_name = None

    if repo_name:
        clean = sanitize_name(repo_name)
        candidate = ALLOWED_REPO_DIR / clean
        if candidate.exists() and candidate.is_dir():
            target_dir = candidate
            target_repo_name = clean
        elif ALLOWED_REPO_DIR.exists():
            for p in ALLOWED_REPO_DIR.iterdir():
                if p.is_dir() and p.name.lower() == clean.lower():
                    target_dir = p
                    target_repo_name = p.name
                    break

    if not target_dir and ALLOWED_REPO_DIR.exists():
        available = [
            p for p in ALLOWED_REPO_DIR.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name not in PREFLIGHT_IGNORED_DIRS
        ]
        if available:
            target_dir = available[0]
            target_repo_name = target_dir.name

    if not target_dir:
        return (
            f"=== Project Context ===\n"
            f"Repository: {repo_name or 'Not specified'}\n"
            "(No local repository files found on disk. Relying on indexed Knowledge Graph topology.)"
        )

    code_extensions = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".cs", ".razor", ".go", ".java",
        ".rs", ".rb", ".php", ".c", ".cpp", ".h", ".swift", ".kt", ".vue",
        ".html", ".css", ".scss", ".json", ".yaml", ".yml", ".toml", ".sql", ".sh"
    }

    all_files = []
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = sorted([d for d in dirs if d not in PREFLIGHT_IGNORED_DIRS and not d.startswith(".")])
        for f in sorted(files):
            if f.startswith("."):
                continue
            ext = Path(f).suffix.lower()
            if ext in code_extensions or f in ("Dockerfile", "Makefile", "Procfile"):
                rel_p = Path(root).relative_to(target_dir) / f
                all_files.append(rel_p)

    # 1. Project File Tree
    file_tree_lines = [f"- {p.as_posix()}" for p in all_files[:70]]
    if len(all_files) > 70:
        file_tree_lines.append(f"- ... ({len(all_files) - 70} more files)")
    file_tree_str = "\n".join(file_tree_lines)
    chars_used = len(file_tree_str)

    # 2. Manifests & Configs (Framework & dependency discovery)
    manifest_names = {
        "package.json", "requirements.txt", "pyproject.toml", "cargo.toml",
        "go.mod", "pom.xml", "docker-compose.yml", "dockerfile", "readme.md"
    }
    manifest_samples = []

    for p in all_files:
        if p.name.lower() in manifest_names or p.name.lower().endswith(".csproj"):
            file_abs = target_dir / p
            try:
                txt = file_abs.read_text(encoding="utf-8", errors="ignore").strip()
                if txt:
                    sample = f"=== Config / Manifest: {p.as_posix()} ===\n{txt[:1200]}\n"
                    manifest_samples.append(sample)
                    chars_used += len(sample)
                    if len(manifest_samples) >= 3:
                        break
            except Exception:
                continue

    # 3. Query Relevance Scoring
    q_lower = (query or "").lower()
    raw_tokens = set(re.findall(r'[a-zA-Z0-9_]{3,}', q_lower))
    stop_words = {
        "how", "does", "work", "here", "this", "that", "what", "where",
        "when", "with", "from", "show", "tell", "explain", "code", "file",
        "files", "project", "system", "please", "help"
    }
    query_tokens = raw_tokens - stop_words

    synonym_map = {
        "auth": ["auth", "login", "jwt", "token", "session", "user", "password", "oauth", "guard", "cookie", "security", "identity", "credential"],
        "authentication": ["auth", "login", "jwt", "token", "session", "user", "password", "oauth", "guard", "cookie", "security", "identity", "credential"],
        "login": ["auth", "login", "session", "token", "user", "password"],
        "user": ["user", "account", "profile", "member", "auth"],
        "database": ["db", "database", "sql", "model", "schema", "entity", "migration", "repository", "dbcontext", "prisma"],
        "db": ["db", "database", "sql", "model", "schema", "entity", "repository", "dbcontext"],
        "api": ["api", "route", "router", "endpoint", "controller", "handler", "service"],
        "route": ["route", "router", "endpoint", "controller", "handler", "url", "path"],
        "proxy": ["proxy", "v2ray", "node", "server", "subscription", "traffic", "ping", "speed"],
        "payment": ["pay", "payment", "checkout", "stripe", "billing", "invoice"]
    }
    expanded_tokens = set(query_tokens)
    for tok in list(query_tokens):
        if tok in synonym_map:
            expanded_tokens.update(synonym_map[tok])

    entrypoint_names = {"program.cs", "startup.cs", "main.py", "app.py", "index.ts", "index.js", "app.tsx", "server.js", "app.jsx", "main.go"}

    scored_files = []
    for p in all_files:
        posix = p.as_posix().lower()
        score = 0
        for tok in expanded_tokens:
            if tok in posix:
                score += 15
        if p.name.lower() in entrypoint_names:
            score += 5
        if p.name.lower() in manifest_names or p.name.lower().endswith(".csproj"):
            continue

        if score > 0 or len(scored_files) < 10:
            file_abs = target_dir / p
            try:
                with open(file_abs, "r", encoding="utf-8", errors="ignore") as f_obj:
                    snippet = f_obj.read(25000)
                content_lower = snippet.lower()
                for tok in expanded_tokens:
                    if tok in content_lower:
                        score += 8
            except Exception:
                pass

        if score > 0:
            scored_files.append((score, p))

    scored_files.sort(key=lambda x: x[0], reverse=True)

    top_candidates = [p for _, p in scored_files[:6]]
    if not top_candidates:
        for p in all_files:
            if p.name.lower() in entrypoint_names or "service" in p.name.lower() or "controller" in p.name.lower():
                top_candidates.append(p)
                if len(top_candidates) >= 4:
                    break
        if not top_candidates and all_files:
            top_candidates = all_files[:4]

    source_snippets = []
    for p in top_candidates:
        file_abs = target_dir / p
        try:
            txt = file_abs.read_text(encoding="utf-8", errors="ignore").strip()
            if txt:
                clipped = txt[:2200]
                sample = f"=== File: {p.as_posix()} ===\n{clipped}\n"
                if chars_used + len(sample) > max_chars:
                    break
                source_snippets.append(sample)
                chars_used += len(sample)
        except Exception:
            continue

    context_blocks = [
        f"### Target Project: `{target_repo_name}`\n",
        f"#### Project File Tree:\n```\n{file_tree_str}\n```\n"
    ]
    if manifest_samples:
        context_blocks.append("#### Project Configurations & Dependencies:\n" + "\n".join(manifest_samples))
    if source_snippets:
        context_blocks.append("#### Relevant Code Snippets & Implementations:\n" + "\n".join(source_snippets))

    return "\n\n".join(context_blocks)

def execute_graphrag_query(query: str, repo_name: str = None, top_k: int = 4) -> dict:
    """
    Executes a high-precision GraphRAG query grounded in real codebase files and topology:
    1. Collects grounded codebase context (file tree, manifests, relevant source code).
    2. Embeds the user query and retrieves top semantic focal points.
    3. Traverses 1-2 hops of relational graph context (Callers, Callees, Classes, Files).
    4. Serializes context into compact Call Graph Paths and AST Contracts.
    5. Synthesizes a deep architectural and code-aware response.
    """
    # Text-to-Cypher (NL2Cypher) Shortcut
    if query.strip().startswith("/cypher "):
        actual_query = query.strip().replace("/cypher ", "").strip()
        return execute_nl2cypher_query(actual_query, repo_name)

    # Gather grounded codebase context (file tree, manifests, relevant source code)
    codebase_context = collect_rag_repo_context(repo_name=repo_name, query=query)

    # 1. Query Embedding & Vector Search (with automatic symbolic fallback)
    hits = []
    try:
        query_vector = get_embedding(query)
        hits = vector_search(query_vector, top_k=top_k, repo_name=repo_name, query_text=query)
    except Exception as emb_err:
        print(f"[GraphRAG] Vector embedding notice ({emb_err}). Using symbolic graph retrieval fallback.")
        try:
            hits = vector_search(None, top_k=top_k, repo_name=repo_name, query_text=query)
        except Exception as fallback_err:
            print(f"[GraphRAG] Symbolic fallback error: {fallback_err}")
            hits = []

    # If no nodes retrieved from graph, synthesize using grounded codebase context
    if not hits:
        from app.core.llm import call_llm
        system_prompt = (
            f"You are CodeGraph Brain AI Assistant, an expert software engineer and enterprise architect.\n"
            f"You have direct access to the codebase '{repo_name or 'current repository'}'.\n"
            "Use the provided Project File Tree, Configurations, and Source Code excerpts to directly, accurately, "
            "and concretely answer the user's question about this codebase. Cite specific file paths, classes, and logic. "
            "Never say you don't have access to the codebase or files."
        )
        user_prompt = f"""Target Project: {repo_name or 'Active Codebase'}
User Question:
{query}

{codebase_context}

Please provide an accurate, clear, and comprehensive answer explaining the system architecture and code logic:"""
        try:
            direct_answer = call_llm(user_prompt, system_prompt)
        except Exception as e:
            direct_answer = (
                "⚠️ **CodeGraph Brain AI System:**\n\n"
                f"No matching nodes found in the knowledge graph, and unable to connect to the language model: `{e}`\n\n"
                "💡 **Help:** Please verify that the repository has been indexed and LLM configuration is valid."
            )
        return {
            "answer": direct_answer,
            "retrieved_nodes": [],
            "graph_context": []
        }

    # 2. Graph Relational Expansion
    func_ids = [h["id"] for h in hits if h.get("id")]
    relational_context = expand_graph_context(func_ids)

    # 3. Classify Intent & Serialize into Compact Graph Topology (No massive raw code dumps!)
    intent = classify_query_intent(query)
    compact_graph_context = serialize_graph_topology(hits, relational_context, intent)

    system_prompt = (
        f"You are CodeGraph Brain AI Assistant, an expert Enterprise Software Architect and Code Reviewer.\n"
        f"You have full access to the codebase '{repo_name or 'current repository'}'.\n"
        "Use the provided Knowledge Graph topology (call chains, AST signatures), Project File Tree, and Source Code excerpts "
        "to answer the user's question accurately, deeply, and concisely in English.\n"
        "Rely strictly on the verified file paths, code logic, and symbol relationships. "
        "Never claim you lack access to the codebase."
    )

    user_prompt = f"""Target Project: {repo_name or 'Active Codebase'}
User Question:
{query}

{codebase_context}

Code Knowledge Graph Topology:
{compact_graph_context}

Please provide an accurate, structured response focusing on code relationships and architecture:
"""

    from app.core.llm import call_llm
    try:
        answer = call_llm(user_prompt, system_prompt)
    except Exception as e:
        print(f"[GraphRAG] Synthesis notice: {e}")
        try:
            fn_names = ", ".join([h.get("name", "") for h in hits if h.get("name")])
            light_prompt = f"{query}\n\n(Relevant symbols in graph: {fn_names})\n\n{codebase_context}"
            answer = call_llm(light_prompt, system_prompt)
        except Exception as e2:
            answer = f"⚠️ LLM communication error: {e2}\n\n{compact_graph_context}"

    return {
        "answer": answer,
        "retrieved_nodes": hits,
        "graph_context": relational_context,
        "intent": intent
    }

# --- Read-only Cypher guard (D2 security fix) ---
# NL2Cypher executes model-generated Cypher against the graph. We enforce a strict
# allowlist so a hallucinated or prompt-injected query cannot mutate or destroy data
# (e.g. `MATCH (n) DETACH DELETE n` or `CALL db.killServer()` / `db.reset()`).
_READONLY_FORBIDDEN = (
    "create", "merge", "set", "delete", "remove", "detach", "drop",
    "call", "load", "use", "commit", "begin", "terminate",
)
_READONLY_PROC_PREFIXES = ("db.", "apoc.", "system.")
_READONLY_ALLOWED_FIRST = {"match", "return", "with", "unwind", "optional"}
_READONLY_MAX_LIMIT = 50
_READONLY_TIMEOUT_MS = 30000

def validate_readonly_cypher(cypher: str) -> str:
    """
    Validate that a Cypher string is a safe read-only query and return the query,
    with a LIMIT appended if none is present. Raises ValueError on anything that
    could mutate, delete, or abuse the database.
    """
    if not cypher or not cypher.strip():
        raise ValueError("Empty Cypher query.")

    q = cypher.strip().rstrip(";").strip()
    if ";" in q:
        raise ValueError("Only a single Cypher statement is allowed.")

    low = q.lower()

    # Block dangerous procedure namespaces (db.*, apoc.*, system.*).
    for prefix in _READONLY_PROC_PREFIXES:
        if prefix in low:
            raise ValueError(f"Procedures in namespace '{prefix}' are not allowed.")

    # Block any mutating / administrative keyword (word-boundary, case-insensitive).
    for tok in _READONLY_FORBIDDEN:
        if re.search(rf"\b{tok}\b", low):
            raise ValueError(f"Keyword '{tok.upper()}' is not allowed in read-only queries.")

    # The query must open with a read-oriented clause.
    first = (low.split() or [""])[0]
    if first not in _READONLY_ALLOWED_FIRST:
        raise ValueError("Query must start with MATCH, RETURN, WITH, UNWIND or OPTIONAL.")

    # Always bound the result set.
    if not re.search(r"\blimit\b", low):
        q = f"{q} LIMIT {_READONLY_MAX_LIMIT}"
    return q

def execute_nl2cypher_query(query: str, repo_name: str = None) -> dict:
    from app.core.llm import call_llm
    from app.core.graph import driver

    schema = """
    Nodes:
    - (f:File {id, path, repo, language, churn})
    - (c:Class {id, name, file_path, repo, language, code})
    - (fn:Function {id, name, file_path, repo, language, code})
    - (api:ApiRoute {path, method, repo})
    - (d:Developer {email, name})

    Relationships:
    - (Repository)-[:CONTAINS]->(File)
    - (File)-[:CONTAINS]->(Class)
    - (File)-[:CONTAINS]->(Function)
    - (Class)-[:HAS_METHOD]->(Function)
    - (Function)-[:CALLS]->(FunctionReference)
    - (File)-[:EXPOSES_API]->(ApiRoute)
    - (File)-[:CALLS_API]->(ApiRoute)
    - (Developer)-[:AUTHORED]->(File)
    - (File)-[:TESTS]->(File)
    """

    system_prompt_cypher = (
        "You are an expert Neo4j Cypher query writer. "
        "Write a READ-ONLY Cypher query (MATCH/RETURN/WITH only; no CREATE/SET/DELETE/MERGE/CALL) "
        "to answer the user's question based on this schema:\n"
        f"{schema}\n"
        "Return ONLY a JSON object with a single key 'cypher'. Do not include markdown blocks or any other text."
    )

    try:
        llm_resp = call_llm(query, system_prompt_cypher)

        # Robustly extract the first JSON object (raw_decode handles braces inside
        # the Cypher string, unlike a greedy regex).
        decoded = None
        start = llm_resp.find("{")
        if start != -1:
            try:
                decoded, _ = json.JSONDecoder().raw_decode(llm_resp[start:])
            except json.JSONDecodeError:
                decoded = None
        if not isinstance(decoded, dict) or "cypher" not in decoded:
            raise ValueError("Failed to extract a 'cypher' object from the LLM response.")

        cypher_query = validate_readonly_cypher(str(decoded["cypher"]).strip())

        with driver.session() as session:
            try:
                result = session.run(cypher_query, timeout=_READONLY_TIMEOUT_MS)
            except TypeError:
                # Older drivers that don't accept a per-query timeout kwarg.
                result = session.run(cypher_query)
            db_results = [dict(r) for r in result]

        explain_prompt = (
            "You are a Senior Software Architect. Explain the result of this graph database query to the user in English.\n\n"
            f"User Question: {query}\n"
            f"Cypher Query Executed: {cypher_query}\n"
            f"Database Result: {db_results[:15]}\n\n"
            "Provide a concise, analytical answer based ONLY on the Database Result."
        )
        final_answer = call_llm("Explain the results.", explain_prompt)

        return {
            "answer": final_answer + f"\n\n*(Executed Cypher Query: `{cypher_query}`)*",
            "retrieved_nodes": [],
            "graph_context": db_results[:10]
        }
    except Exception as e:
        return {
            "answer": f"❌ NL2Cypher Failed: {e}",
            "retrieved_nodes": [],
            "graph_context": []
        }
