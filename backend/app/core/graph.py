import os
from neo4j import GraphDatabase
from app.core.embedder import get_embedding, validate_embedding_configuration

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password123")
NEO4J_AUTH = os.getenv("NEO4J_AUTH", "").strip().lower()

if NEO4J_AUTH == "none" or (NEO4J_PASSWORD and NEO4J_PASSWORD.lower() == "none") or not NEO4J_PASSWORD:
    driver = GraphDatabase.driver(NEO4J_URI)
else:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def init_graph_schema():
    """Initializes constraints and vector search indexes in Neo4j."""
    embedding_dim = validate_embedding_configuration()
    with driver.session() as session:
        # Constraints for faster lookup and deduplication
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (r:Repository) REQUIRE r.name IS UNIQUE")
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (f:File) REQUIRE f.id IS UNIQUE")
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (c:Class) REQUIRE c.id IS UNIQUE")
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (fn:Function) REQUIRE fn.id IS UNIQUE")
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (d:Developer) REQUIRE d.email IS UNIQUE")

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

            # Class methods
            for method in cls.get("methods", []):
                m_name = method["name"]
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
            func_name = func["name"]
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

def vector_search(query_embedding: list[float], top_k: int = 5, repo_name: str = None) -> list[dict]:
    """
    Finds nearest functions/code elements using Neo4j Vector Index or similarity fallback.
    """
    with driver.session() as session:
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

        # Fallback if vector index is not populated/available
        fallback_query = """
        MATCH (fn:Function)
        WHERE $repo_name IS NULL OR fn.repo = $repo_name
        RETURN fn.id AS id, fn.name AS name, fn.file_path AS file_path,
               fn.repo AS repo, fn.code AS code, fn.docstring AS docstring,
               fn.start_line AS start_line, fn.end_line AS end_line, 1.0 AS score
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

def get_graph_elements(repo_name: str = None, limit: int = 150) -> dict:
    """
    Exports nodes and edges formatted for Cytoscape.js / visual rendering.
    """
    with driver.session() as session:
        query = """
        MATCH (n)
        WHERE $repo_name IS NULL OR n.repo = $repo_name OR n.name = $repo_name
        WITH n LIMIT $limit
        OPTIONAL MATCH (n)-[r]->(m)
        RETURN n, r, m
        """
        result = session.run(query, repo_name=repo_name, limit=limit)
        
        nodes = {}
        edges = []

        for record in result:
            n = record.get("n")
            r = record.get("r")
            m = record.get("m")

            if n:
                n_labels = list(n.labels)
                label = n_labels[0] if n_labels else "Node"
                name = n.get("name") or n.get("path") or n.get("id") or "Node"
                node_id = str(n.element_id if hasattr(n, "element_id") else n.id)
                nodes[node_id] = {
                    "data": {
                        "id": node_id,
                        "label": name,
                        "type": label,
                        "properties": dict(n)
                    }
                }

            if m:
                m_labels = list(m.labels)
                m_label = m_labels[0] if m_labels else "Node"
                m_name = m.get("name") or m.get("path") or m.get("id") or "Node"
                m_node_id = str(m.element_id if hasattr(m, "element_id") else m.id)
                nodes[m_node_id] = {
                    "data": {
                        "id": m_node_id,
                        "label": m_name,
                        "type": m_label,
                        "properties": dict(m)
                    }
                }

            if r and n and m:
                edge_id = str(r.element_id if hasattr(r, "element_id") else r.id)
                edges.append({
                    "data": {
                        "id": edge_id,
                        "source": str(n.element_id if hasattr(n, "element_id") else n.id),
                        "target": str(m.element_id if hasattr(m, "element_id") else m.id),
                        "label": r.type
                    }
                })

        return {
            "nodes": list(nodes.values()),
            "edges": edges
        }

def close_connection():
    driver.close()
