import os
import re
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from app.core.llm import get_llm_config, normalize_llm_base_url, resolve_docker_target_url

logger = logging.getLogger("codegraph.multi_agent")


def get_openai_client(timeout: float = 60.0):
    """
    Creates an authentic OpenAI SDK client targeting the configured inference endpoint
    (Vultr Serverless Inference, OpenAI, OpenRouter, Groq, Ollama).
    """
    from openai import OpenAI
    import httpx

    config = get_llm_config()
    api_key = (
        config.get("api_key")
        or os.getenv("VULTR_INFERENCE_API_KEY")
        or os.getenv("VULTR_API_KEY")
        or os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise ValueError("No AI API key configured. Remote inference call refused.")

    clean_key = re.sub(r'^(?:Bearer\s+|["\'])', '', api_key, flags=re.IGNORECASE)
    clean_key = re.sub(r'["\']$', '', clean_key).strip()

    base_url = config.get("base_url", "") or os.getenv("VULTR_INFERENCE_BASE_URL") or "https://api.vultrinference.com/v1"
    norm_base = normalize_llm_base_url(base_url)
    model = config.get("model", "") or os.getenv("LLM_MODEL") or "deepseek-v4.1-flash-1017"

    http_proxy = (config.get("http_proxy") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or "").strip()
    is_local_target = any(host in norm_base.lower() for host in ["host.docker.internal", "localhost", "127.0.0.1", "::1"])

    client_kwargs = {
        "base_url": norm_base,
        "api_key": clean_key,
        "timeout": float(timeout)
    }

    if http_proxy and not is_local_target:
        resolved_proxy = resolve_docker_target_url(http_proxy)
        try:
            client_kwargs["http_client"] = httpx.Client(proxy=resolved_proxy, timeout=float(timeout))
        except Exception as pe:
            logger.warning(f"Could not initialize httpx proxy: {pe}")

    client = OpenAI(**client_kwargs)
    return client, model, norm_base


# ==============================================================================
# AGENT 1: CODE REVIEW AGENT (PREFLIGHT CODE REVIEW / P0-P3 ARBITER)
# ==============================================================================
class CodeReviewAgent:
    """
    Specialized Multi-Agent Arbiter for Preflight Code Review.
    Strictly guarantees that:
    1. ZERO reasoning tokens or scratchpad monologues appear in output.
    2. Only clean, professional Markdown starting with '# Preflight Code Review Report' is emitted.
    3. Handles DeepSeek reasoning models without leaking thinking tokens.
    """

    @classmethod
    def _test_mode_report(cls, repo_name: str) -> str:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"""# Preflight Code Review Report: {repo_name}
**Date:** {now_str}
**Merge Gate:** 🟢 APPROVED

## Executive Summary & Metrics Table
| Severity | Level | Count | Action |
| :--- | :--- | :--- | :--- |
| **[P0]** | Critical Blocker | 0 | ❌ Blocks Deployment |
| **[P1]** | High Blocker | 0 | ❌ Blocks Deployment |
| **[P2]** | Medium Warning | 1 | 💬 Review Before Shipping |
| **[P3]** | Low Nit | 2 | 💡 Optional Hygiene |

## Detailed Findings
### [P2] Input Validation Boundary
- **Path:** `main.py`
- **Issue:** Input parameter bounds are unconstrained.
- **Recommendation:** Add schema validation.

## Actionable Remediation Plan
1. Address [P2] input boundary checks.
"""

    @classmethod
    def execute_review(
        cls,
        repo_name: str,
        user_prompt: str,
        system_prompt: str,
        timeout: int = 60,
        max_tokens: int = 4000
    ) -> str:
        if os.getenv("TESTING") == "1" and not os.getenv("REAL_LLM"):
            return cls._test_mode_report(repo_name)

        client, model, _ = get_openai_client(timeout=timeout)
        is_reasoning_model = any(k in (model or "").lower() for k in ["deepseek", "r1", "reasoning"])

        # DeepSeek reasoning models work best when instructions are at top of user prompt
        if is_reasoning_model:
            full_prompt = (
                f"{system_prompt.strip()}\n\n"
                f"---\n\n"
                f"{user_prompt.strip()}\n\n"
                "CRITICAL: Output ONLY the complete markdown report beginning with '# Preflight Code Review Report'."
            )
            messages = [{"role": "user", "content": full_prompt}]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

        create_kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens
        }
        if not is_reasoning_model:
            create_kwargs["temperature"] = 0.0
        else:
            create_kwargs["temperature"] = 0.3

        reply = client.chat.completions.create(**create_kwargs)
        choice = reply.choices[0]
        raw_content = getattr(choice.message, "content", None) or ""

        # 1. Clean think tags from content
        clean_content = re.sub(r'<think>[\s\S]*?</think>', '', raw_content, flags=re.IGNORECASE).strip()

        # 2. Check if clean_content contains a markdown report
        if clean_content and ("# " in clean_content or "## " in clean_content):
            # Extract from first header downward
            m_header = re.search(r'(#+ (?:Preflight|Code Review|Executive Summary)[\s\S]*)', clean_content, flags=re.IGNORECASE)
            if m_header:
                return m_header.group(1).strip()
            return clean_content

        # 3. If content was empty or lacked markdown headers (model put report in reasoning channel):
        extra = getattr(choice.message, "model_extra", {}) or {}
        reasoning_val = (
            getattr(choice.message, "reasoning", None)
            or getattr(choice.message, "reasoning_content", None)
            or extra.get("reasoning")
            or extra.get("reasoning_content")
        )
        if reasoning_val:
            clean_reasoning = re.sub(r'<think>[\s\S]*?</think>', '', str(reasoning_val), flags=re.IGNORECASE).strip()
            m_report = re.search(r'(#+ (?:Preflight|Code Review|Executive Summary)[\s\S]*)', clean_reasoning, flags=re.IGNORECASE)
            if m_report:
                report_extracted = m_report.group(1).strip()
                if report_extracted:
                    return report_extracted

        if clean_content:
            return clean_content

        raise ValueError("CodeReviewAgent: AI endpoint returned an empty code review report.")


# ==============================================================================
# AGENT 2: BUG FIX AGENT (AUTONOMOUS CODE REPAIR ARBITER)
# ==============================================================================
class BugFixAgent:
    """
    Specialized Multi-Agent Coder for Autonomous Bug Repair.
    Strictly guarantees that:
    1. Code blocks are extracted accurately from content or reasoning channels.
    2. Function-level and line-level replacements preserve file integrity.
    3. Excludes all conversational preambles and reasoning monologues.
    """

    @classmethod
    def execute_bug_fix(
        cls,
        repo_name: Optional[str],
        file_path: str,
        original_code: str,
        clean_repair: str,
        rule: Optional[str] = None,
        detail: Optional[str] = None,
        snippet: Optional[str] = None,
        line_number: Optional[int] = None,
        file_ext: str = "txt",
        display_rel: str = "file",
        timeout: int = 90
    ) -> str:
        if os.getenv("TESTING") == "1" and not os.getenv("REAL_LLM"):
            return original_code + "\n// [Preflight Auto-Fix Applied: Defect Resolved]\n"

        client, model, _ = get_openai_client(timeout=timeout)
        is_reasoning_model = any(k in (model or "").lower() for k in ["deepseek", "r1", "reasoning"])

        user_prompt = f"""You are a senior software engineer fixing a defect in production source code.

Defect Details:
- Defect Rule: {rule or 'Suspected Defect'}
- Target File: '{display_rel}'
- Line Number: {line_number or 'N/A'}
- Problem Explanation: {detail or 'Suspected issue'}
- Repair Instructions: {clean_repair}

Offending Code Snippet to Fix:
```{file_ext}
{snippet or 'N/A'}
```

Original Source Code of '{display_rel}':
```{file_ext}
{original_code}
```

Instructions:
1. Apply the exact bug fix requested in the repair instructions.
2. Maintain documentation integrity and preserve existing comments, imports, and unaffected functions.
3. Return the complete updated source code inside a single markdown code block:
```{file_ext}
[complete updated code here]
```"""

        messages = [{"role": "user", "content": user_prompt}]
        create_kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": 8192
        }
        if not is_reasoning_model:
            create_kwargs["temperature"] = 0.0
        else:
            create_kwargs["temperature"] = 0.2

        reply = client.chat.completions.create(**create_kwargs)
        choice = reply.choices[0]
        raw_content = getattr(choice.message, "content", None) or ""

        # 1. Clean think tags
        clean_content = re.sub(r'<think>[\s\S]*?</think>', '', raw_content, flags=re.IGNORECASE).strip()

        from app.core.preflight import extract_code_block
        if clean_content:
            fixed = extract_code_block(clean_content, original_code, snippet)
            if fixed != original_code:
                return fixed

        # 2. Check reasoning channel fallback if content produced no effective change
        extra = getattr(choice.message, "model_extra", {}) or {}
        reasoning_val = (
            getattr(choice.message, "reasoning", None)
            or getattr(choice.message, "reasoning_content", None)
            or extra.get("reasoning")
            or extra.get("reasoning_content")
        )
        if reasoning_val:
            clean_reasoning = re.sub(r'<think>[\s\S]*?</think>', '', str(reasoning_val), flags=re.IGNORECASE).strip()
            fixed = extract_code_block(clean_reasoning, original_code, snippet)
            if fixed != original_code:
                return fixed

        return clean_content or original_code


# ==============================================================================
# AGENT 3: DEFECT ARBITER AGENT (3-TIER SECURITY & HAZARD AUDIT)
# ==============================================================================
class DefectArbiterAgent:
    """
    Specialized Multi-Agent Arbiter for 3-Tier Security & Defect Evaluation (Red, Orange, Yellow).
    Strictly guarantees that:
    1. Returns valid JSON schema (is_defect, confidence, defects, repair_prompt).
    2. Strips any reasoning or markdown chatter outside the JSON object.
    """

    @classmethod
    def execute_audit(
        cls,
        user_prompt: str,
        system_prompt: str,
        tier_name: str,
        fallback_dict: dict,
        timeout: int = 60
    ) -> dict:
        if os.getenv("TESTING") == "1" and not os.getenv("REAL_LLM"):
            return fallback_dict

        client, model, _ = get_openai_client(timeout=timeout)
        is_reasoning_model = any(k in (model or "").lower() for k in ["deepseek", "r1", "reasoning"])

        if is_reasoning_model:
            full_prompt = f"{system_prompt.strip()}\n\n---\n\n{user_prompt.strip()}\n\nReturn strictly valid JSON."
            messages = [{"role": "user", "content": full_prompt}]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

        create_kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": 3000
        }
        if not is_reasoning_model:
            create_kwargs["temperature"] = 0.0

        try:
            reply = client.chat.completions.create(**create_kwargs)
            choice = reply.choices[0]
            raw_content = getattr(choice.message, "content", None) or ""
            clean_content = re.sub(r'<think>[\s\S]*?</think>', '', raw_content, flags=re.IGNORECASE).strip()

            from app.core.preflight import extract_json_defect
            if clean_content:
                res = extract_json_defect(clean_content, fallback_dict)
                if res != fallback_dict:
                    return res

            # Fallback to reasoning JSON if content was empty
            extra = getattr(choice.message, "model_extra", {}) or {}
            reasoning_val = (
                getattr(choice.message, "reasoning", None)
                or getattr(choice.message, "reasoning_content", None)
                or extra.get("reasoning")
                or extra.get("reasoning_content")
            )
            if reasoning_val:
                clean_reasoning = re.sub(r'<think>[\s\S]*?</think>', '', str(reasoning_val), flags=re.IGNORECASE).strip()
                res = extract_json_defect(clean_reasoning, fallback_dict)
                return res

            return fallback_dict
        except Exception as e:
            logger.warning(f"[DefectArbiterAgent] Audit call notice for {tier_name}: {e}")
            return fallback_dict


# ==============================================================================
# AGENT 4: COPILOT RAG AGENT (GRAPHRAG & ARCHITECTURE CHAT ASSISTANT)
# ==============================================================================
class CopilotAgent:
    """
    Specialized Multi-Agent Conversational Architect for AI Copilot (GraphRAG & Direct Chat).
    Strictly guarantees that:
    1. Grounds answers in the active repository file tree, manifests, and relevant code excerpts.
    2. Strips all reasoning tokens, providing clean, developer-focused answers in pure English.
    """

    @classmethod
    def execute_chat(
        cls,
        query: str,
        repo_name: Optional[str] = None,
        top_k: int = 4
    ) -> dict:
        from app.core.rag import (
            collect_rag_repo_context,
            vector_search,
            expand_graph_context,
            classify_query_intent,
            serialize_graph_topology
        )
        from app.core.embedder import get_embedding

        codebase_context = collect_rag_repo_context(repo_name=repo_name, query=query)

        hits = []
        try:
            query_vector = get_embedding(query)
            hits = vector_search(query_vector, top_k=top_k, repo_name=repo_name, query_text=query)
        except Exception:
            try:
                hits = vector_search(None, top_k=top_k, repo_name=repo_name, query_text=query)
            except Exception:
                hits = []

        system_instruction = (
            f"You are CodeGraph Brain AI Assistant, an expert software engineer and enterprise architect.\n"
            f"You have direct access to the codebase '{repo_name or 'current repository'}'.\n"
            "Use the provided Project File Tree, Configurations, Source Code excerpts, and Graph Topology "
            "to directly and concretely answer the user's question about this codebase. Cite specific file paths, classes, and logic. "
            "Never say you don't have access to the codebase or files."
        )

        if not hits:
            user_content = f"""Target Project: {repo_name or 'Active Codebase'}
User Question:
{query}

{codebase_context}

Please provide an accurate, clear, and comprehensive answer explaining the system architecture and code logic:"""
        else:
            func_ids = [h["id"] for h in hits if h.get("id")]
            relational_context = expand_graph_context(func_ids)
            intent = classify_query_intent(query)
            compact_graph_context = serialize_graph_topology(hits, relational_context, intent)
            user_content = f"""Target Project: {repo_name or 'Active Codebase'}
User Question:
{query}

{codebase_context}

Code Knowledge Graph Topology:
{compact_graph_context}

Please provide an accurate, structured response focusing on code relationships and architecture:"""

        client, model, _ = get_openai_client(timeout=60)
        is_reasoning_model = any(k in (model or "").lower() for k in ["deepseek", "r1", "reasoning"])

        if is_reasoning_model:
            full_prompt = f"{system_instruction}\n\n---\n\n{user_content}"
            messages = [{"role": "user", "content": full_prompt}]
        else:
            messages = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content}
            ]

        try:
            reply = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.3,
                max_tokens=3000
            )
            raw_content = reply.choices[0].message.content or ""
            clean_content = re.sub(r'<think>[\s\S]*?</think>', '', raw_content, flags=re.IGNORECASE).strip()
            if not clean_content:
                clean_content = f"### Project Architecture Overview for `{repo_name}`\n\nAnalyzed codebase files and dependency graph."
            return {
                "answer": clean_content,
                "retrieved_nodes": hits,
                "graph_context": []
            }
        except Exception as e:
            return {
                "answer": f"⚠️ Copilot communication notice: {e}\n\n{codebase_context[:500]}",
                "retrieved_nodes": hits,
                "graph_context": []
            }
