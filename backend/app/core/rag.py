import os
from app.core.embedder import get_embedding
from app.core.graph import vector_search, expand_graph_context

def execute_graphrag_query(query: str, repo_name: str = None, top_k: int = 4) -> dict:
    """
    Executes a GraphRAG query over the code knowledge graph:
    1. Embeds the user query.
    2. Retrieves top-K semantically relevant functions/methods using Vector Search.
    3. Traverses the graph to gather relational context (Callers, Callees, Classes, Files).
    4. Synthesizes a structured answer using an LLM.
    """
    # Text-to-Cypher (NL2Cypher) RAG Mode
    if query.strip().startswith("/cypher "):
        actual_query = query.strip().replace("/cypher ", "").strip()
        return execute_nl2cypher_query(actual_query, repo_name)

    # 1. Query Embedding
    query_vector = get_embedding(query)

    # 2. Vector Search
    hits = vector_search(query_vector, top_k=top_k, repo_name=repo_name)
    if not hits:
        return {
            "answer": "No relevant code entities found in the graph database. Please index your repository first.",
            "retrieved_nodes": [],
            "graph_context": []
        }

    # 3. Graph Expansion (1-2 hops)
    func_ids = [h["id"] for h in hits if h.get("id")]
    relational_context = expand_graph_context(func_ids)

    # Build context string for the LLM
    context_blocks = []
    for hit in hits:
        name = hit.get("name")
        file_path = hit.get("file_path")
        code = hit.get("code") or ""
        doc = hit.get("docstring") or ""
        score = hit.get("score", 0.0)

        # Match relational context
        rel = next((r for r in relational_context if r.get("function_id") == hit.get("id")), {})
        callers = ", ".join(rel.get("called_by", [])) or "None"
        callees = ", ".join(rel.get("calls", [])) or "None"
        parent_class = rel.get("class_name") or "None"

        block = f"""
Entity: {name} (File: {file_path})
Parent Class: {parent_class}
Relevance Score: {score:.4f}
Docstring: {doc}
Called By: {callers}
Calls: {callees}
Code:
```
{code}
```
"""
        context_blocks.append(block.strip())

    full_context = "\n\n---\n\n".join(context_blocks)

    system_prompt = (
        "You are an expert Enterprise Software Architect and Code Reviewer. "
        "Use the provided Code Graph context (including function code, classes, and call relationships) "
        "to answer the user's inquiry accurately. "
        "Reference exact file paths, class/function names, and explain call graphs when relevant."
    )

    user_prompt = f"""
User Question:
{query}

Retrieved Code Graph Context:
{full_context}

Please provide a detailed, well-structured explanation:
"""

    from app.core.llm import call_llm
    try:
        answer = call_llm(user_prompt, system_prompt)
    except Exception as e:
        answer = f"*(Notice: LLM response unavailable: {e})*\n\n### Retrieved Code Graph Context:\n\n{full_context}"

    return {
        "answer": answer,
        "retrieved_nodes": hits,
        "graph_context": relational_context
    }

def execute_nl2cypher_query(query: str, repo_name: str = None) -> dict:
    from app.core.llm import call_llm
    from app.core.graph import driver
    import json
    import re

    schema = \"\"\"
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
    \"\"\"

    system_prompt_cypher = (
        "You are an expert Neo4j Cypher query writer. "
        "Write a read-only Cypher query to answer the user's question based on this schema:\n"
        f"{schema}\n"
        "Return ONLY a JSON object with a single key 'cypher'. Do not include markdown blocks or any other text."
    )

    try:
        # Step 1: Generate Cypher
        llm_resp = call_llm(query, system_prompt_cypher)
        # Parse JSON
        cypher_match = re.search(r'\{.*\}', llm_resp.replace('\n', ' '))
        if not cypher_match:
            raise ValueError("Failed to extract JSON from LLM response.")
        
        cypher_query = json.loads(cypher_match.group(0)).get("cypher", "")
        if not cypher_query.lower().startswith("match"):
            raise ValueError("Invalid Cypher query generated.")

        # Step 2: Execute Cypher
        with driver.session() as session:
            result = session.run(cypher_query)
            db_results = [dict(r) for r in result]
        
        # Step 3: Explain Results
        explain_prompt = (
            "You are a Senior Software Architect. Explain the result of this graph database query to the user.\n\n"
            f"User Question: {query}\n"
            f"Cypher Query Executed: {cypher_query}\n"
            f"Database Result: {db_results[:20]}\n\n" # Limit to 20 to avoid context overflow
            "Provide a concise, analytical answer based ONLY on the Database Result."
        )
        final_answer = call_llm("Explain the results.", explain_prompt)
        
        return {
            "answer": final_answer + f"\n\n*(Generated via Cypher: `{cypher_query}`)*",
            "retrieved_nodes": [],
            "graph_context": db_results[:10]
        }
    except Exception as e:
        return {
            "answer": f"❌ NL2Cypher Failed: {e}",
            "retrieved_nodes": [],
            "graph_context": []
        }

