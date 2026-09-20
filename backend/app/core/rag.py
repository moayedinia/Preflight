import os
import re
import json
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
        "کد", "چطور نوشته", "پیاده سازی", "پیاده‌سازی", "الگوریتم", "دیباگ", "باگ",
        "خطا", "سورس", "code", "implementation", "implement", "how it works internally",
        "debug", "refactor", "source", "syntax", "write"
    ]
    if any(k in q for k in impl_keywords):
        return "IMPLEMENTATION"

    impact_keywords = [
        "اثر", "تغییر", "ریسک", "وابستگی", "کجا فراخوانی", "کی صدا میزنه", "وابسته",
        "blast radius", "impact", "who calls", "caller", "callee", "dependency", "depend",
        "شکستن", "شکسته", "downstream", "upstream"
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
        lines.append("### ۱. زنجیره فراخوانی‌های گراف (Call Graph Paths):")
        seen = set()
        for chain in call_chains:
            if chain not in seen:
                seen.add(chain)
                lines.append(f"  • {chain}")
        lines.append("")

    # 2. Entity Contracts (Signatures & Metadata)
    lines.append("### ۲. قرارداد نمادها در گراف کد (Code Entity Contracts):")
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
            lines.append("  - Implementation Slice (برش منطق کلیدی):")
            code_lines = [line for line in raw_code.splitlines()[:15] if line.strip()]
            code_slice = "\n    ".join(code_lines)
            lines.append(f"    ```\n    {code_slice}\n    ```")
        lines.append("")

    return "\n".join(lines).strip()

def execute_graphrag_query(query: str, repo_name: str = None, top_k: int = 4) -> dict:
    """
    Executes an ultra-efficient GraphRAG query over the code knowledge graph:
    1. Embeds the user query and retrieves top semantic focal points.
    2. Traverses 1-2 hops of relational graph context (Callers, Callees, Classes, Files).
    3. Serializes context into compact Call Graph Paths and AST Contracts (Code-less / AST Sliced).
    4. Synthesizes a high-precision architectural response using minimal tokens (< 300 tokens).
    """
    # Text-to-Cypher (NL2Cypher) Shortcut
    if query.strip().startswith("/cypher "):
        actual_query = query.strip().replace("/cypher ", "").strip()
        return execute_nl2cypher_query(actual_query, repo_name)

    # 1. Query Embedding & Vector Search
    hits = []
    try:
        query_vector = get_embedding(query)
        hits = vector_search(query_vector, top_k=top_k, repo_name=repo_name)
    except Exception as emb_err:
        print(f"[GraphRAG] Vector search note ({emb_err}). Continuing with graph fallback.")
        hits = []

    # If no semantic nodes retrieved, fallback to direct synthesis
    if not hits:
        from app.core.llm import call_llm
        system_prompt = (
            "You are CodeGraph Brain AI Assistant, an expert software architect. "
            "Provide clean, well-structured, and practical answers in Persian."
        )
        try:
            direct_answer = call_llm(query, system_prompt)
        except Exception as e:
            direct_answer = (
                "سیستم هوش مصنوعی CodeGraph Brain:\n"
                f"گره‌های متناظری در پایگاه دانش گراف یافت نشد و سرویس LLM در دسترس نیست: {e}"
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
        "You are an expert Enterprise Software Architect and Code Reviewer. "
        "Use the provided Knowledge Graph topology (call chains, symbol signatures, and relationships) "
        "to answer the user's question accurately and concisely in Persian. "
        "Rely strictly on the verified graph relationships and exact file paths."
    )

    user_prompt = f"""
پرسش کاربر:
{query}

اطلاعات استخراج‌شده از گراف دانش کد (Code Knowledge Graph Topology):
{compact_graph_context}

لطفاً پاسخی دقیق، ساختاریافته و متمرکز بر روابط و معماری ارائه دهید:
"""

    from app.core.llm import call_llm
    try:
        answer = call_llm(user_prompt, system_prompt)
    except Exception as e:
        print(f"[GraphRAG] Synthesis notice: {e}")
        try:
            fn_names = ", ".join([h.get("name", "") for h in hits if h.get("name")])
            light_prompt = f"{query}\n\n(Relevant symbols in graph: {fn_names})"
            answer = call_llm(light_prompt, system_prompt)
        except Exception as e2:
            answer = f"⚠️ خطا در ارتباط با مدل زبانی: {e2}\n\n{compact_graph_context}"

    return {
        "answer": answer,
        "retrieved_nodes": hits,
        "graph_context": relational_context,
        "intent": intent
    }

def execute_nl2cypher_query(query: str, repo_name: str = None) -> dict:
    from app.core.llm import call_llm
    from app.core.graph import driver
    import json
    import re

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
        "Write a read-only Cypher query to answer the user's question based on this schema:\n"
        f"{schema}\n"
        "Return ONLY a JSON object with a single key 'cypher'. Do not include markdown blocks or any other text."
    )

    try:
        llm_resp = call_llm(query, system_prompt_cypher)
        cypher_match = re.search(r'\{.*\}', llm_resp.replace('\n', ' '))
        if not cypher_match:
            raise ValueError("Failed to extract JSON from LLM response.")

        cypher_query = json.loads(cypher_match.group(0)).get("cypher", "")
        if not cypher_query.lower().startswith("match"):
            raise ValueError("Invalid Cypher query generated.")

        with driver.session() as session:
            result = session.run(cypher_query)
            db_results = [dict(r) for r in result]

        explain_prompt = (
            "You are a Senior Software Architect. Explain the result of this graph database query to the user in Persian.\n\n"
            f"User Question: {query}\n"
            f"Cypher Query Executed: {cypher_query}\n"
            f"Database Result: {db_results[:15]}\n\n"
            "Provide a concise, analytical answer based ONLY on the Database Result."
        )
        final_answer = call_llm("Explain the results.", explain_prompt)

        return {
            "answer": final_answer + f"\n\n*(کوئری گراف اجرا شده: `{cypher_query}`)*",
            "retrieved_nodes": [],
            "graph_context": db_results[:10]
        }
    except Exception as e:
        return {
            "answer": f"❌ NL2Cypher Failed: {e}",
            "retrieved_nodes": [],
            "graph_context": []
        }
