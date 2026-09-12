import os
from app.core.embedder import get_embedding, client
from app.core.graph import vector_search, expand_graph_context

def execute_graphrag_query(query: str, repo_name: str = None, top_k: int = 4) -> dict:
    """
    Executes a GraphRAG query over the code knowledge graph:
    1. Embeds the user query.
    2. Retrieves top-K semantically relevant functions/methods using Vector Search.
    3. Traverses the graph to gather relational context (Callers, Callees, Classes, Files).
    4. Synthesizes a structured answer using an LLM.
    """
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

    answer = ""
    if client:
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.2
            )
            answer = response.choices[0].message.content
        except Exception as e:
            answer = f"(LLM generation error: {e})\n\nContext summary:\n" + full_context[:1000]
    else:
        answer = (
            "OpenAI API key not set or unavailable. "
            "Here is the retrieved GraphRAG context based on vector & structural graph traversal:\n\n"
            f"Top Matches Found: {len(hits)}\n"
            + "\n".join([f"- **{h.get('name')}** in `{h.get('file_path')}`" for h in hits])
        )

    return {
        "answer": answer,
        "retrieved_nodes": hits,
        "graph_context": relational_context
    }
