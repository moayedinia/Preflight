import os
import time
import re
from neo4j import GraphDatabase
from app.core.embedder import get_embedding, validate_embedding_configuration, EMBEDDING_DIM

KEYWORDS_FN = {
    'def', 'func', 'fn', 'function', 'sub', 'fun', 'void', 'int', 'string', 'bool',
    'float', 'double', 'char', 'auto', 'async', 'public', 'private', 'protected',
    'internal', 'static', 'virtual', 'override', 'abstract', 'sealed', 'inline', 'extern',
    'return', 'class', 'struct', 'interface', 'let', 'const', 'var', 'val', 'new'
}

CONTROL_FLOW_FN = {'if', 'while', 'for', 'switch', 'catch', 'sizeof', 'typeof', 'using', 'lock'}

def _short_call_name(ref_name: str) -> str:
    """Reduce a raw call name to its short identifier: 'utils.parse' -> 'parse', 'parse(' -> 'parse'."""
    s = (ref_name or "").strip()
    s = s.split(".")[-1]
    s = s.replace("(", "").replace(")", "").strip()
    return s

def resolve_call_targets(call_name: str, name_to_ids: dict[str, list[str]]) -> "str | None":
    """
    D4 fix — precise function-call target selection.

    Given a raw call name and a map of {short function name -> [function ids]},
    return the single target function id IF the name resolves unambiguously
    (exactly one target carries that short name), otherwise None.

    Rationale: a bare `MATCH ... WHERE target.name = short_name` links a call to
    EVERY function with that name across the repo (or across languages), which
    corrupts the call graph, blast radius and dead-code analysis. We only resolve
    the ambiguity with the name itself; an ambiguous name resolves to nothing
    (precision over recall). Import/scope-aware resolution (SCIP) is the follow-on.
    """
    short = _short_call_name(call_name)
    if not short:
        return None
    ids = name_to_ids.get(short) or []
    if len(ids) == 1:
        return ids[0]
    return None

def clean_function_name(name_str: str, node_id: str = None) -> str:
    """
    Extracts strictly the clean function/method name from any raw code snippet, signature,
    or composite ID. Never returns full function code or multiline signatures.
    """
    if not name_str:
        if node_id and ":" in node_id:
            cand = node_id.split(":")[-1].strip()
            if cand and cand.lower() not in KEYWORDS_FN:
                return cand
        return "Function"

    stripped = str(name_str).strip()
    if re.match(r'^[A-Za-z0-9_]+$', stripped) and stripped.lower() not in KEYWORDS_FN:
        return stripped

    first_line = stripped.splitlines()[0].strip()

    # 1. Match function name right before parenthesis: e.g. ProcessOrder(...) or calculate_total(...)
    m = re.search(r'([A-Za-z0-9_]+)\s*\(', first_line)
    if m:
        candidate = m.group(1)
        if candidate.lower() not in CONTROL_FLOW_FN and candidate.lower() not in KEYWORDS_FN:
            return candidate

    # 2. Composite ID check: repo:file.py:func_name
    if ":" in first_line:
        parts = first_line.split(":")
        candidate = parts[-1].strip()
        if candidate and re.match(r'^[A-Za-z0-9_]+$', candidate) and candidate.lower() not in KEYWORDS_FN:
            return candidate

    # 3. Arrow function or assignment: const myFunc = ... or let foo = ...
    m = re.search(r'(?:const|let|var|val)\s+([A-Za-z0-9_]+)\s*=', first_line)
    if m:
        return m.group(1)

    # 4. Filter words to find first non-keyword identifier
    words = re.findall(r'[A-Za-z0-9_]+', first_line)
    non_kw = [w for w in words if w.lower() not in KEYWORDS_FN and w.lower() not in CONTROL_FLOW_FN]
    if non_kw:
        return non_kw[0]

    return "Function"

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password123")
NEO4J_AUTH = os.getenv("NEO4J_AUTH", "").strip().lower()

driver_kwargs = {
    "connection_timeout": 5.0,
    "max_connection_lifetime": 300,
}

if NEO4J_AUTH == "none" or (NEO4J_PASSWORD and NEO4J_PASSWORD.lower() == "none") or not NEO4J_PASSWORD:
    driver = GraphDatabase.driver(NEO4J_URI, **driver_kwargs)
else:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD), **driver_kwargs)

def wait_for_neo4j(max_retries: int = 30, retry_delay: float = 2.0) -> bool:
    """
    Waits for Neo4j to become responsive and ready to accept Cypher queries.
    Retries up to max_retries times with retry_delay seconds between attempts.
    """
    for attempt in range(1, max_retries + 1):
        try:
            with driver.session() as session:
                session.run("RETURN 1").consume()
            print(f"[Neo4j] Connected successfully to {NEO4J_URI} (ready after attempt {attempt}).", flush=True)
            return True
        except Exception as e:
            if attempt < max_retries:
                print(f"[Neo4j] Waiting for database to become available (attempt {attempt}/{max_retries}): {e} ...", flush=True)
                time.sleep(retry_delay)
            else:
                print(f"[Neo4j] Failed to connect to {NEO4J_URI} after {max_retries} attempts: {e}", flush=True)
                return False
    return False

def init_graph_schema(max_retries: int = 30, retry_delay: float = 2.0):
    """Initializes constraints and vector search indexes in Neo4j with connection retry."""
    if not wait_for_neo4j(max_retries=max_retries, retry_delay=retry_delay):
        print("[Neo4j Warning] Neo4j is not reachable. Schema initialization deferred.", flush=True)
        return

    # 1. First ensure unique constraints exist in Neo4j
    try:
        with driver.session() as session:
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (r:Repository) REQUIRE r.name IS UNIQUE")
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (f:File) REQUIRE f.id IS UNIQUE")
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (c:Class) REQUIRE c.id IS UNIQUE")
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (fn:Function) REQUIRE fn.id IS UNIQUE")
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (d:Developer) REQUIRE d.email IS UNIQUE")

            # Property indexes to accelerate cross-reference and relationship resolution
            session.run("CREATE INDEX IF NOT EXISTS FOR (fn:Function) ON (fn.name)")
            session.run("CREATE INDEX IF NOT EXISTS FOR (fn:Function) ON (fn.repo)")
            session.run("CREATE INDEX IF NOT EXISTS FOR (f:File) ON (f.repo)")
            session.run("CREATE INDEX IF NOT EXISTS FOR (c:Class) ON (c.name)")
            session.run("CREATE INDEX IF NOT EXISTS FOR (ref:FunctionReference) ON (ref.name)")
            session.run("CREATE INDEX IF NOT EXISTS FOR (m:Module) ON (m.name)")
        print("[Neo4j] Unique constraints and property indexes verified successfully.", flush=True)
    except Exception as const_err:
        print(f"[Neo4j Warning] Could not ensure constraints: {const_err}", flush=True)

    # 2. Determine embedding dimensions safely with fast timeout
    try:
        embedding_dim = validate_embedding_configuration(timeout=5)
    except Exception as emb_err:
        print(f"[Neo4j Schema Notice] Embedding configuration validation deferred ({emb_err}). Using configured default EMBEDDING_DIM={EMBEDDING_DIM}.", flush=True)
        embedding_dim = EMBEDDING_DIM

    with driver.session() as session:
        # Vector Index for Functions (Supports both Neo4j 5.15+ DDL and Neo4j 5.11-5.14 procedure)
        try:
            index_exists = False
            current_dim = None
            try:
                records = session.run("SHOW INDEXES YIELD name, type, options WHERE name = 'function_embeddings'").data()
                if records:
                    index_exists = True
                    cfg = records[0].get("options", {}).get("indexConfig", {})
                    current_dim = cfg.get("vector.dimensions")
            except Exception:
                try:
                    records = session.run("SHOW INDEXES YIELD name WHERE name = 'function_embeddings'").data()
                    if records:
                        index_exists = True
                except Exception:
                    pass

            if index_exists and current_dim and current_dim != embedding_dim:
                print(f"Adapting vector index dimension: {current_dim} -> {embedding_dim}")
                try:
                    session.run("DROP INDEX function_embeddings IF EXISTS")
                except Exception:
                    try:
                        session.run("DROP INDEX function_embeddings")
                    except Exception:
                        pass
                index_exists = False

            if not index_exists:
                # 1. Try modern Neo4j 5.15+ Cypher DDL
                try:
                    session.run(f"""
                    CREATE VECTOR INDEX function_embeddings IF NOT EXISTS
                    FOR (fn:Function) ON (fn.embedding)
                    OPTIONS {{indexConfig: {{
                        `vector.dimensions`: {embedding_dim},
                        `vector.similarity_function`: 'cosine'
                    }}}}
                    """)
                    print(f"Vector index 'function_embeddings' initialized via Cypher DDL (dim={embedding_dim})")
                except Exception as ddl_err:
                    # 2. Fallback to Neo4j 5.11 - 5.14 procedure
                    print(f"Notice: DDL vector index syntax not supported on this Neo4j version ({ddl_err}). Falling back to db.index.vector.createNodeIndex...")
                    try:
                        session.run(f"""
                        CALL db.index.vector.createNodeIndex(
                            'function_embeddings',
                            'Function',
                            'embedding',
                            {embedding_dim},
                            'cosine'
                        )
                        """)
                        print(f"Vector index 'function_embeddings' initialized via procedure (dim={embedding_dim})")
                    except Exception as proc_err:
                        if "already exists" in str(proc_err).lower():
                            print("Vector index 'function_embeddings' already exists.")
                        else:
                            raise proc_err
        except Exception as e:
            print(f"Notice regarding vector index: {e}")

def ingest_parsed_data(repo_name: str, parsed_data: dict):
    """
    Ingests parsed entities, extracts embeddings for functions and classes,
    and links structural relationships (CONTAINS, HAS_METHOD, CALLS, IMPORTS).
    Preserves programming language on all nodes.
    """
    file_path = parsed_data["file"]
    file_id = f"{repo_name}:{file_path}"
    lang = parsed_data.get("language", "unknown")
    author = parsed_data.get("author", "Unknown")
    author_email = parsed_data.get("author_email", "unknown@example.com")
    churn = parsed_data.get("churn", 0)

    with driver.session() as session:
        # 1. Repo, File, and Developer nodes
        session.run("""
            MERGE (r:Repository {name: $repo_name})
            MERGE (f:File {id: $file_id})
            ON CREATE SET f.path = $file_path, f.repo = $repo_name, f.language = $lang, f.churn = $churn
            ON MATCH SET f.language = $lang, f.churn = $churn
            MERGE (r)-[:CONTAINS]->(f)
            
            MERGE (d:Developer {email: $author_email})
            ON CREATE SET d.name = $author
            MERGE (d)-[:AUTHORED]->(f)
        """, repo_name=repo_name, file_id=file_id, file_path=file_path, lang=lang, author=author, author_email=author_email, churn=churn)

        # 2. Imports
        for imp in parsed_data.get("imports", []):
            session.run("""
                MATCH (f:File {id: $file_id})
                MERGE (m:Module {name: $imp})
                MERGE (f)-[:IMPORTS]->(m)
            """, file_id=file_id, imp=imp)

        # 3. Classes and their methods
        for cls in parsed_data.get("classes", []):
            cls_name = cls["name"]
            cls_id = f"{file_id}:{cls_name}"
            cls_embedding = get_embedding(cls.get("code", "") or cls_name)

            session.run("""
                MATCH (f:File {id: $file_id})
                MERGE (c:Class {id: $cls_id})
                SET c.name = $cls_name,
                    c.file_path = $file_path,
                    c.repo = $repo_name,
                    c.language = $lang,
                    c.start_line = $start_line,
                    c.end_line = $end_line,
                    c.docstring = $docstring,
                    c.code = $code,
                    c.embedding = $embedding
                MERGE (f)-[:CONTAINS]->(c)
            """, file_id=file_id, cls_id=cls_id, cls_name=cls_name,
               file_path=file_path, repo_name=repo_name, lang=lang,
               start_line=cls["start_line"], end_line=cls["end_line"],
               docstring=cls.get("docstring", ""), code=cls.get("code", "")[:4000],
               embedding=cls_embedding)

            # Class inheritance (extends / implements / superclass)
            for base_name in cls.get("bases", []):
                clean_base = clean_function_name(base_name)
                if clean_base:
                    session.run("""
                        MATCH (c:Class {id: $cls_id})
                        MERGE (base:ClassReference {name: $clean_base})
                        MERGE (c)-[:INHERITS_FROM]->(base)
                    """, cls_id=cls_id, clean_base=clean_base)

            # Class methods
            for method in cls.get("methods", []):
                m_name = clean_function_name(method["name"], method.get("code"))
                m_id = f"{cls_id}:{m_name}"
                m_embedding = get_embedding(method.get("code", "") or m_name)

                session.run("""
                    MATCH (c:Class {id: $cls_id})
                    MERGE (fn:Function {id: $m_id})
                    SET fn.name = $m_name,
                        fn.file_path = $file_path,
                        fn.repo = $repo_name,
                        fn.language = $lang,
                        fn.start_line = $start_line,
                        fn.end_line = $end_line,
                        fn.docstring = $docstring,
                        fn.code = $code,
                        fn.embedding = $embedding
                    MERGE (c)-[:HAS_METHOD]->(fn)
                """, cls_id=cls_id, m_id=m_id, m_name=m_name,
                   file_path=file_path, repo_name=repo_name, lang=lang,
                   start_line=method["start_line"], end_line=method["end_line"],
                   docstring=method.get("docstring", ""), code=method.get("code", "")[:4000],
                   embedding=m_embedding)

                # Track function calls
                for call_name in method.get("calls", []):
                    session.run("""
                        MATCH (fn:Function {id: $m_id})
                        MERGE (callee:FunctionReference {name: $call_name})
                        MERGE (fn)-[:CALLS]->(callee)
                    """, m_id=m_id, call_name=call_name)

        # 4. Top-level standalone functions
        for func in parsed_data.get("functions", []):
            func_name = clean_function_name(func["name"], func.get("code"))
            func_id = f"{file_id}:{func_name}"
            func_embedding = get_embedding(func.get("code", "") or func_name)

            session.run("""
                MATCH (f:File {id: $file_id})
                MERGE (fn:Function {id: $func_id})
                SET fn.name = $func_name,
                    fn.file_path = $file_path,
                    fn.repo = $repo_name,
                    fn.language = $lang,
                    fn.start_line = $start_line,
                    fn.end_line = $end_line,
                    fn.docstring = $docstring,
                    fn.code = $code,
                    fn.embedding = $embedding
                MERGE (f)-[:CONTAINS]->(fn)
            """, file_id=file_id, func_id=func_id, func_name=func_name,
               file_path=file_path, repo_name=repo_name, lang=lang,
               start_line=func["start_line"], end_line=func["end_line"],
               docstring=func.get("docstring", ""), code=func.get("code", "")[:4000],
               embedding=func_embedding)

            for call_name in func.get("calls", []):
                session.run("""
                    MATCH (fn:Function {id: $func_id})
                    MERGE (callee:FunctionReference {name: $call_name})
                    MERGE (fn)-[:CALLS]->(callee)
                """, func_id=func_id, call_name=call_name)

        # 5. API Routes & Clients (Cross-Service Traceability)
        for route in parsed_data.get("api_routes", []):
            session.run("""
                MATCH (f:File {id: $file_id})
                MERGE (api:ApiRoute {path: $path, method: $method})
                ON CREATE SET api.repo = $repo_name
                MERGE (f)-[:EXPOSES_API]->(api)
            """, file_id=file_id, path=route["path"], method=route["method"], repo_name=repo_name)

        for client_req in parsed_data.get("api_clients", []):
            session.run("""
                MATCH (f:File {id: $file_id})
                MERGE (api:ApiRoute {path: $path})
                MERGE (f)-[:CALLS_API]->(api)
            """, file_id=file_id, path=client_req["path"])

        # 6. Test Coverage Link
        if parsed_data.get("is_test"):
            # Simple heuristic: link test file to files with similar names
            # e.g. test_auth.py -> auth.py
            base_name = file_path.split("/")[-1].replace("test_", "").replace(".test", "").replace("_test", "")
            if base_name:
                session.run("""
                    MATCH (test:File {id: $file_id})
                    MATCH (target:File {repo: $repo_name})
                    WHERE target.path ENDS WITH $base_name AND target.id <> $file_id
                    MERGE (test)-[:TESTS]->(target)
                """, file_id=file_id, repo_name=repo_name, base_name=base_name)

def vector_search(query_embedding: list[float] = None, top_k: int = 5, repo_name: str = None, query_text: str = None) -> list[dict]:
    """
    Finds nearest functions/code elements using Neo4j Vector Index or symbolic graph fallback.
    If query_embedding is None or vector index query fails, falls back to keyword matching
    or topological connectivity (caller degree) ranking in Neo4j.
    """
    with driver.session() as session:
        if query_embedding:
            try:
                # Query Neo4j vector index
                query = """
                CALL db.index.vector.queryNodes('function_embeddings', $top_k, $embedding)
                YIELD node, score
                WHERE $repo_name IS NULL OR node.repo = $repo_name
                RETURN node.id AS id, node.name AS name, node.file_path AS file_path,
                       node.repo AS repo, node.code AS code, node.docstring AS docstring,
                       node.start_line AS start_line, node.end_line AS end_line, score
                """
                result = session.run(query, top_k=top_k, embedding=query_embedding, repo_name=repo_name)
                hits = [dict(record) for record in result]
                if hits:
                    return hits
            except Exception as e:
                print(f"Vector index query error: {e}. Falling back to node traversal.")

        # Symbolic Fallback: Try extracting significant identifiers/keywords from query_text
        keyword = None
        if query_text:
            # Extract identifier-like tokens
            tokens = [w for w in re.findall(r"[A-Za-z0-9_\u0600-\u06FF]{3,}", query_text) if len(w) >= 3]
            # Ignore general Persian or English chat words
            stop_words = {"what", "where", "which", "function", "class", "tell", "show", "code", "file", "repo", "چطور", "کجا", "توابع", "کلاس", "کد", "مخزن", "معماری", "توضیح"}
            meaningful = [t for t in tokens if t.lower() not in stop_words]
            if meaningful:
                keyword = meaningful[0]

        if keyword:
            try:
                kw_query = """
                MATCH (fn:Function)
                WHERE ($repo_name IS NULL OR fn.repo = $repo_name)
                  AND (toLower(fn.name) CONTAINS toLower($keyword) OR toLower(fn.file_path) CONTAINS toLower($keyword))
                OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
                WITH fn, COUNT(caller) AS degree
                RETURN fn.id AS id, fn.name AS name, fn.file_path AS file_path,
                       fn.repo AS repo, fn.code AS code, fn.docstring AS docstring,
                       fn.start_line AS start_line, fn.end_line AS end_line, 1.0 AS score
                ORDER BY degree DESC
                LIMIT $top_k
                """
                result = session.run(kw_query, top_k=top_k, repo_name=repo_name, keyword=keyword)
                hits = [dict(record) for record in result]
                if hits:
                    return hits
            except Exception as kw_err:
                print(f"Keyword search fallback error: {kw_err}")

        # Final Fallback: Return the top architectural functions ranked by caller degree (most connected)
        fallback_query = """
        MATCH (fn:Function)
        WHERE $repo_name IS NULL OR fn.repo = $repo_name
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
        WITH fn, COUNT(caller) AS degree
        RETURN fn.id AS id, fn.name AS name, fn.file_path AS file_path,
               fn.repo AS repo, fn.code AS code, fn.docstring AS docstring,
               fn.start_line AS start_line, fn.end_line AS end_line, 1.0 AS score
        ORDER BY degree DESC
        LIMIT $top_k
        """
        result = session.run(fallback_query, top_k=top_k, repo_name=repo_name)
        return [dict(record) for record in result]

def expand_graph_context(function_ids: list[str]) -> list[dict]:
    """
    Expands 1-2 hops around retrieved functions to uncover structural relationships:
    - Callers: who calls this function?
    - Callees: what does this function call?
    - Parent Class & Containing File.
    """
    if not function_ids:
        return []

    with driver.session() as session:
        query = """
        MATCH (fn:Function)
        WHERE fn.id IN $function_ids
        OPTIONAL MATCH (c:Class)-[:HAS_METHOD]->(fn)
        OPTIONAL MATCH (f:File)-[:CONTAINS]->(fn)
        OPTIONAL MATCH (fn)-[:CALLS]->(callee)
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
        RETURN fn.id AS function_id,
               fn.name AS function_name,
               c.name AS class_name,
               f.path AS file_path,
               COLLECT(DISTINCT callee.name) AS calls,
               COLLECT(DISTINCT caller.name) AS called_by
        """
        result = session.run(query, function_ids=function_ids)
        return [dict(record) for record in result]

def get_graph_stats() -> dict:
    """Returns total counts for dashboard overview."""
    with driver.session() as session:
        result = session.run("""
        RETURN
          COUNT { MATCH (r:Repository) } AS repositories,
          COUNT { MATCH (f:File) } AS files,
          COUNT { MATCH (c:Class) } AS classes,
          COUNT { MATCH (fn:Function) } AS functions,
          COUNT { MATCH ()-[rel]->() } AS relationships
        """)
        rec = result.single()
        if rec:
            return dict(rec)
        return {"repositories": 0, "files": 0, "classes": 0, "functions": 0, "relationships": 0}

def resolve_cross_references(repo_name: str = None):
    """
    Resolves cross-references and extracts explicit architectural relationships:
    1. Sanitizes legacy/dirty function names first.
    2. Ensures all files belong to their Repository via CONTAINS.
    3. Ensures Class methods are connected to their containing File and have repo set.
    4. Resolves Function -> Function direct CALLS across the codebase.
    5. Resolves File -> File internal module import dependencies (DEPENDS_ON).
    6. Resolves Class -> Class inheritance hierarchies (INHERITS_FROM).
    """
    with driver.session() as session:
        try:
            # 1. Sanitize dirty/legacy function names first so subsequent matches succeed
            dirty_fns = session.run("""
                MATCH (fn:Function)
                WHERE ($repo_name IS NULL OR fn.repo = $repo_name)
                  AND (fn.name CONTAINS '\n' OR fn.name CONTAINS ' ' OR fn.name CONTAINS '(')
                RETURN coalesce(fn.id, elementId(fn)) AS id, fn.name AS raw_name
            """, repo_name=repo_name).data()
            for item in dirty_fns:
                clean_name = clean_function_name(item.get("raw_name"), item.get("id"))
                if clean_name and clean_name != item.get("raw_name"):
                    session.run("""
                        MATCH (fn:Function)
                        WHERE fn.id = $id OR elementId(fn) = $id
                        SET fn.name = $clean_name
                    """, id=item["id"], clean_name=clean_name)

            # 2. Ensure all Files are linked to their Repository
            session.run("""
                MATCH (f:File)
                WHERE f.repo IS NOT NULL AND ($repo_name IS NULL OR f.repo = $repo_name)
                MERGE (r:Repository {name: f.repo})
                MERGE (r)-[:CONTAINS]->(f)
            """, repo_name=repo_name)

            # 3. Ensure Class methods have repo and file_path properties, and are linked
            session.run("""
                MATCH (f:File)-[:CONTAINS]->(c:Class)-[:HAS_METHOD]->(fn:Function)
                WHERE $repo_name IS NULL OR f.repo = $repo_name
                SET fn.repo = f.repo, fn.file_path = f.path, c.repo = f.repo
                MERGE (f)-[:CONTAINS]->(fn)
            """, repo_name=repo_name)

            # 4. Resolve Function -> Function direct CALLS (precise, batched) — D4 fix.
            # Collect the caller->callee reference edges, resolve each call name in
            # Python to at most ONE function id (see resolve_call_targets), then merge
            # only unambiguous links in a single batch. This replaces the old per-name
            # MATCH that linked a call to EVERY same-named function in the repo.
            refs = session.run(
                """
                MATCH (caller:Function)-[cr:CALLS]->(ref:FunctionReference)
                WHERE ($repo_name IS NULL OR caller.repo = $repo_name)
                RETURN coalesce(caller.id, elementId(caller)) AS caller_id,
                       ref.name AS call_name
                """,
                repo_name=repo_name,
            ).data()

            fn_rows = session.run(
                """
                MATCH (t:Function)
                WHERE $repo_name IS NULL OR t.repo = $repo_name
                RETURN t.name AS name, coalesce(t.id, elementId(t)) AS id
                """,
                repo_name=repo_name,
            ).data()
            name_to_ids: dict[str, list[str]] = {}
            for row in fn_rows:
                nm = row["name"]
                if nm:
                    name_to_ids.setdefault(nm, []).append(row["id"])

            merge_rows = []
            for rec in refs:
                caller_id = rec["caller_id"]
                if not caller_id:
                    continue
                target_id = resolve_call_targets(rec["call_name"], name_to_ids)
                if target_id and target_id != caller_id:
                    merge_rows.append({"caller_id": caller_id, "target_id": target_id})

            if merge_rows:
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (caller:Function), (target:Function)
                    WHERE (caller.id = row.caller_id OR elementId(caller) = row.caller_id)
                      AND (target.id = row.target_id OR elementId(target) = row.target_id)
                      AND caller <> target
                    MERGE (caller)-[:CALLS]->(target)
                    """,
                    rows=merge_rows,
                )

            # 5. Resolve internal File -> File dependencies (DEPENDS_ON) based on imports
            session.run("""
                MATCH (f1:File)-[:IMPORTS]->(m:Module)
                WHERE ($repo_name IS NULL OR f1.repo = $repo_name)
                WITH f1, m,
                     trim(split(replace(replace(m.name, 'from ', ''), 'import ', ''), ' ')[0]) AS imp_path
                WITH f1, imp_path, split(imp_path, '.')[-1] AS imp_base
                WHERE imp_base <> '' AND len(imp_base) >= 2
                MATCH (f2:File)
                WHERE ($repo_name IS NULL OR f2.repo = $repo_name)
                  AND f1 <> f2
                  AND (
                    f2.path ENDS WITH ('/' + imp_base + '.py')
                    OR f2.path ENDS WITH ('/' + imp_base + '.ts')
                    OR f2.path ENDS WITH ('/' + imp_base + '.js')
                    OR f2.path ENDS WITH ('/' + imp_base + '.go')
                    OR f2.path ENDS WITH ('/' + imp_base + '.java')
                    OR f2.path ENDS WITH ('/' + imp_base + '.rs')
                    OR f2.path = (imp_base + '.py')
                    OR f2.path CONTAINS ('/' + replace(imp_path, '.', '/') + '.')
                  )
                MERGE (f1)-[:DEPENDS_ON]->(f2)
            """, repo_name=repo_name)

            # 6. Resolve Class -> Class inheritance (INHERITS_FROM)
            session.run("""
                MATCH (sub:Class)-[:INHERITS_FROM]->(base:ClassReference)
                WHERE ($repo_name IS NULL OR sub.repo = $repo_name)
                MATCH (super:Class)
                WHERE ($repo_name IS NULL OR super.repo = $repo_name)
                  AND (super.name = base.name OR super.name = split(base.name, '.')[-1])
                  AND sub <> super
                MERGE (sub)-[:INHERITS_FROM]->(super)
            """, repo_name=repo_name)

        except Exception as e:
            print(f"[Graph] resolve_cross_references warning: {e}", flush=True)

def get_graph_elements(repo_name: str = None, limit: int = 800) -> dict:
    """
    Exports nodes and edges formatted for Vis.js Network and Cytoscape.js visual rendering.
    Strips large 768-dim float embeddings to maintain light network payloads and high FPS.
    Prioritizes true code relationships: CALLS, DEPENDS_ON, HAS_METHOD, INHERITS_FROM, CONTAINS.
    """
    # Fast resolution of calls and links
    resolve_cross_references(repo_name)

    with driver.session() as session:
        # Prioritize rich semantic code relationships over raw external module import lists
        query = """
        MATCH (n)-[r:CALLS|DEPENDS_ON|HAS_METHOD|INHERITS_FROM|EXPOSES_API|CALLS_API|CONTAINS|IMPORTS|AUTHORED]->(m)
        WHERE ($repo_name IS NULL OR n.repo = $repo_name OR n.name = $repo_name OR m.repo = $repo_name)
          AND (n:Function OR n:File OR n:Class OR n:Repository OR n:Developer OR n:Module)
          AND (m:Function OR m:File OR m:Class OR m:Repository OR m:Developer OR m:Module OR m:ApiRoute)
          AND NOT m:FunctionReference
          AND NOT m:ClassReference
        RETURN n, r, m, startNode(r) AS r_start, endNode(r) AS r_end
        ORDER BY CASE type(r)
          WHEN 'CALLS' THEN 1
          WHEN 'DEPENDS_ON' THEN 2
          WHEN 'HAS_METHOD' THEN 3
          WHEN 'INHERITS_FROM' THEN 4
          WHEN 'EXPOSES_API' THEN 5
          WHEN 'CALLS_API' THEN 6
          WHEN 'CONTAINS' THEN 7
          ELSE 8
        END ASC
        LIMIT $limit
        """
        records = list(session.run(query, repo_name=repo_name, limit=limit))

        # Fallback if repository has nodes without relationships yet
        if not records:
            fallback_query = """
            MATCH (n)
            WHERE ($repo_name IS NULL OR n.repo = $repo_name OR n.name = $repo_name)
              AND (n:Function OR n:File OR n:Class OR n:Repository)
            RETURN n, null AS r, null AS m, null AS r_start, null AS r_end
            LIMIT $limit
            """
            records = list(session.run(fallback_query, repo_name=repo_name, limit=limit))
        
        nodes = {}
        edges = []
        edge_keys = set()

        def format_node(node_obj):
            n_labels = list(node_obj.labels)
            label = n_labels[0] if n_labels else "Node"
            
            raw_props = dict(node_obj)
            has_embedding = "embedding" in raw_props and raw_props["embedding"] is not None
            embedding_dim = len(raw_props["embedding"]) if has_embedding and isinstance(raw_props["embedding"], list) else 0
            if "embedding" in raw_props:
                del raw_props["embedding"]
            raw_props["has_embedding"] = has_embedding
            raw_props["embedding_dim"] = embedding_dim

            # Label formatting: clean identifiers only
            if label == "File":
                file_p = raw_props.get("path") or raw_props.get("file_path") or "file"
                name = os.path.basename(file_p) or file_p
            elif label == "Function":
                name = clean_function_name(raw_props.get("name"), raw_props.get("id"))
            elif label == "Class":
                raw_name = raw_props.get("name") or raw_props.get("id") or "Class"
                name = clean_function_name(raw_name, raw_props.get("id"))
            elif label == "Repository":
                name = raw_props.get("name") or "Repository"
            else:
                name = raw_props.get("name") or raw_props.get("path") or raw_props.get("id") or "Node"
            
            node_id = str(node_obj.get("id") or (node_obj.element_id if hasattr(node_obj, "element_id") else node_obj.id))

            return {
                "id": node_id,
                "label": name,
                "type": label,
                "group": label,
                "properties": raw_props,
                "data": {
                    "id": node_id,
                    "label": name,
                    "type": label,
                    "properties": raw_props
                }
            }

        for record in records:
            n = record.get("n")
            r = record.get("r")
            m = record.get("m")
            r_start = record.get("r_start")
            r_end = record.get("r_end")

            if n:
                n_id = str(n.get("id") or (n.element_id if hasattr(n, "element_id") else n.id))
                if n_id not in nodes:
                    nodes[n_id] = format_node(n)

            if m:
                m_id = str(m.get("id") or (m.element_id if hasattr(m, "element_id") else m.id))
                if m_id not in nodes:
                    nodes[m_id] = format_node(m)

            if r and r_start and r_end:
                src_id = str(r_start.get("id") or (r_start.element_id if hasattr(r_start, "element_id") else r_start.id))
                tgt_id = str(r_end.get("id") or (r_end.element_id if hasattr(r_end, "element_id") else r_end.id))
                edge_id = str(r.element_id if hasattr(r, "element_id") else r.id)
                edge_key = f"{src_id}->{tgt_id}:{r.type}"

                if edge_key not in edge_keys:
                    edge_keys.add(edge_key)
                    edges.append({
                        "id": edge_id,
                        "from": src_id,
                        "to": tgt_id,
                        "source": src_id,
                        "target": tgt_id,
                        "label": r.type,
                        "type": r.type,
                        "title": r.type,
                        "data": {
                            "id": edge_id,
                            "source": src_id,
                            "target": tgt_id,
                            "label": r.type,
                            "type": r.type
                        }
                    })

        return {
            "nodes": list(nodes.values()),
            "edges": edges
        }

def get_node_details(node_id: str) -> dict:
    """
    Fetches comprehensive node details including properties, full code,
    and all incoming and outgoing connections for popup inspection.
    """
    with driver.session() as session:
        query = """
        MATCH (n)
        WHERE n.id = $node_id OR elementId(n) = $node_id
        OPTIONAL MATCH (n)-[out_r]->(target)
        OPTIONAL MATCH (source)-[in_r]->(n)
        RETURN n,
               labels(n) AS labels,
               COLLECT(DISTINCT {
                   type: type(out_r),
                   target_id: coalesce(target.id, elementId(target)),
                   target_name: coalesce(target.name, target.path, target.id),
                   target_label: head(labels(target))
               }) AS outgoing,
               COLLECT(DISTINCT {
                   type: type(in_r),
                   source_id: coalesce(source.id, elementId(source)),
                   source_name: coalesce(source.name, source.path, source.id),
                   source_label: head(labels(source))
               }) AS incoming
        """
        result = session.run(query, node_id=node_id).single()
        if not result:
            return {}

        n = result.get("n")
        labels = list(result.get("labels") or [])
        primary_label = labels[0] if labels else "Node"

        props = dict(n)
        has_vector = "embedding" in props and props["embedding"] is not None
        dim = len(props["embedding"]) if has_vector and isinstance(props["embedding"], list) else 0
        sample_vec = props["embedding"][:5] if has_vector and isinstance(props["embedding"], list) else []
        if "embedding" in props:
            del props["embedding"]

        # Filter out empty entries from OPTIONAL MATCH
        outgoing = [o for o in (result.get("outgoing") or []) if o.get("type")]
        incoming = [i for i in (result.get("incoming") or []) if i.get("type")]

        if primary_label == "Function":
            clean_label = clean_function_name(props.get("name") or n.get("name"), props.get("id") or n.get("id"))
        elif primary_label == "File":
            clean_label = os.path.basename(props.get("path") or props.get("file_path") or "") or "File"
        elif primary_label == "Class":
            clean_label = clean_function_name(props.get("name") or n.get("name"), props.get("id") or n.get("id"))
        else:
            clean_label = n.get("name") or n.get("path") or n.get("id") or "Node"

        return {
            "id": str(n.get("id") or (n.element_id if hasattr(n, "element_id") else n.id)),
            "label": clean_label,
            "type": primary_label,
            "labels": labels,
            "properties": props,
            "code": props.get("code", ""),
            "docstring": props.get("docstring", ""),
            "file_path": props.get("file_path") or props.get("path") or "",
            "language": props.get("language", ""),
            "repo": props.get("repo", ""),
            "start_line": props.get("start_line"),
            "end_line": props.get("end_line"),
            "author": props.get("author", ""),
            "churn": props.get("churn", 0),
            "has_embedding": has_vector,
            "embedding_dim": dim,
            "sample_vector": sample_vec,
            "outgoing_connections": outgoing,
            "incoming_connections": incoming
        }

def get_vector_database_stats(repo_name: str = None) -> dict:
    """
    Returns vector database statistics, dimension info, and total indexed entities.
    """
    dim = validate_embedding_configuration()
    model = os.getenv("EMBEDDING_MODEL", "jina-embeddings-v2-code:latest")

    with driver.session() as session:
        query = """
        MATCH (fn:Function)
        WHERE $repo_name IS NULL OR fn.repo = $repo_name
        WITH count(fn) AS total_fn,
             count(fn.embedding) AS vec_fn
        OPTIONAL MATCH (c:Class)
        WHERE $repo_name IS NULL OR c.repo = $repo_name
        WITH total_fn, vec_fn,
             count(c) AS total_cls,
             count(c.embedding) AS vec_cls
        OPTIONAL MATCH (r:Repository)
        WHERE $repo_name IS NULL OR r.name = $repo_name
        RETURN total_fn, vec_fn, total_cls, vec_cls, count(r) AS total_repos
        """
        rec = session.run(query, repo_name=repo_name).single()
        data = dict(rec) if rec else {}

        vec_fn = data.get("vec_fn", 0)
        vec_cls = data.get("vec_cls", 0)

        return {
            "repo_name": repo_name or "All Repositories",
            "index_name": "function_embeddings",
            "dimensions": dim,
            "similarity_metric": "cosine",
            "model": model,
            "total_functions": data.get("total_fn", 0),
            "vectorized_functions": vec_fn,
            "total_classes": data.get("total_cls", 0),
            "vectorized_classes": vec_cls,
            "total_vectors": vec_fn + vec_cls,
            "status": "ONLINE" if (vec_fn + vec_cls) > 0 else "EMPTY"
        }

def get_vector_database_records(repo_name: str = None, search: str = None, limit: int = 100) -> list[dict]:
    """
    Returns searchable records of all vectorized code entities in Neo4j.
    """
    with driver.session() as session:
        query = """
        MATCH (fn:Function)
        WHERE ($repo_name IS NULL OR fn.repo = $repo_name)
          AND fn.embedding IS NOT NULL
          AND ($search IS NULL OR toLower(fn.name) CONTAINS toLower($search) OR toLower(fn.file_path) CONTAINS toLower($search))
        RETURN fn.id AS id,
               fn.name AS name,
               'Function' AS type,
               fn.file_path AS file_path,
               fn.repo AS repo,
               fn.language AS language,
               fn.start_line AS start_line,
               fn.end_line AS end_line,
               size(fn.embedding) AS dim,
               fn.embedding[0..5] AS sample_vector,
               fn.docstring AS docstring,
               fn.code AS code
        ORDER BY fn.file_path ASC, fn.start_line ASC
        LIMIT $limit
        """
        records = session.run(query, repo_name=repo_name, search=search, limit=limit)
        results = []
        for r in records:
            d = dict(r)
            results.append(d)
        return results

def get_node_vector(node_id: str) -> dict:
    """
    Retrieves the full embedding vector (float array) for a specific node.
    """
    with driver.session() as session:
        query = """
        MATCH (n)
        WHERE n.id = $node_id OR elementId(n) = $node_id
        RETURN n.id AS id,
               coalesce(n.name, n.path, n.id) AS name,
               n.file_path AS file_path,
               n.repo AS repo,
               size(n.embedding) AS dim,
               n.embedding AS vector
        """
        rec = session.run(query, node_id=node_id).single()
        if not rec or not rec.get("vector"):
            return {}
        return dict(rec)

def close_connection():
    driver.close()
