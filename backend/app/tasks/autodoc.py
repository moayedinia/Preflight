import os
import time
from app.core.celery import celery_app, CELERY_BROKER_URL
from app.core.graph import driver
from app.core.llm import call_llm

try:
    import redis
    _r_client = redis.from_url(CELERY_BROKER_URL, decode_responses=True)
except Exception:
    _r_client = None

_LOCAL_STOP_FLAGS = set()

def request_stop_autodoc(task_id: str = None, repo_name: str = None):
    """Signals the Auto-Doc worker to halt immediately via Redis and local flag."""
    if task_id:
        _LOCAL_STOP_FLAGS.add(task_id)
        if _r_client:
            try:
                _r_client.set(f"stop_autodoc:{task_id}", "1", ex=600)
            except Exception:
                pass
    if repo_name:
        _LOCAL_STOP_FLAGS.add(f"repo:{repo_name}")
        if _r_client:
            try:
                _r_client.set(f"stop_autodoc_repo:{repo_name}", "1", ex=600)
            except Exception:
                pass

def is_autodoc_stopped(task_id: str = None, repo_name: str = None) -> bool:
    """Checks whether a stop signal has been issued for this task or repository."""
    if task_id and task_id in _LOCAL_STOP_FLAGS:
        return True
    if repo_name and f"repo:{repo_name}" in _LOCAL_STOP_FLAGS:
        return True
    if _r_client:
        try:
            if task_id and _r_client.get(f"stop_autodoc:{task_id}") == "1":
                return True
            if repo_name and _r_client.get(f"stop_autodoc_repo:{repo_name}") == "1":
                return True
        except Exception:
            pass
    return False

@celery_app.task(bind=True, max_retries=1)
def generate_missing_docs_task(self, repo_name: str = None):
    """
    Finds functions without a docstring in the graph and uses the configured LLM to generate them,
    tracking live percentage coverage as the progress bar fills up.
    Regularly checks for user abort/stop requests to halt gracefully.
    """
    task_id = self.request.id if hasattr(self, "request") and self.request else None

    with driver.session() as session:
        stats_query = """
        MATCH (f:Function)
        WHERE $repo_name IS NULL OR f.repo = $repo_name
        RETURN count(f) AS total_functions,
               count(CASE WHEN f.docstring IS NOT NULL AND trim(f.docstring) <> '' THEN 1 END) AS documented_functions
        """
        stat_rec = session.run(stats_query, repo_name=repo_name).single()
        total_fn = stat_rec["total_functions"] if stat_rec else 0
        doc_fn = stat_rec["documented_functions"] if stat_rec else 0
        initial_pct = round((doc_fn * 100.0) / max(total_fn, 1), 1) if total_fn > 0 else 100.0

        # Find functions missing docstring
        query = """
        MATCH (f:Function)
        WHERE (f.docstring IS NULL OR trim(f.docstring) = '')
        """
        if repo_name:
            query += " AND f.repo = $repo_name"
            
        query += " RETURN coalesce(f.id, elementId(f)) AS id, f.name AS name, f.code AS code, f.file_path AS file_path LIMIT 100"
        
        result = session.run(query, repo_name=repo_name)
        functions = [dict(r) for r in result]
        
    if not functions:
        self.update_state(state="SUCCESS", meta={
            "status": "success",
            "percent": 100,
            "progress": "All functions are fully documented! Coverage is 100%.",
            "total_functions": total_fn,
            "documented_functions": doc_fn,
            "coverage_percent": 100.0
        })
        return {
            "status": "success",
            "message": "All functions in this project are already documented! (Coverage: 100%)",
            "total_functions": total_fn,
            "documented_functions": doc_fn,
            "coverage_percent": 100.0,
            "processed": 0
        }

    self.update_state(state="PROGRESS", meta={
        "status": "PROGRESS",
        "percent": initial_pct,
        "progress": f"Initial coverage: {initial_pct}% ({doc_fn}/{total_fn} documented). Starting Auto-Doc...",
        "total": len(functions),
        "total_functions": total_fn,
        "documented_functions": doc_fn,
        "coverage_percent": initial_pct,
        "current": 0
    })
        
    system_prompt = (
        "You are an expert technical writer and senior software engineer. "
        "Your task is to write a concise, professional docstring for the provided function code. "
        "Do not include the function code or Markdown code blocks in your response. "
        "Return ONLY the raw text of the docstring."
    )
    
    processed = 0
    errors = 0
    current_documented = doc_fn
    
    for i, func in enumerate(functions):
        # Graceful user stop check
        if is_autodoc_stopped(task_id, repo_name):
            current_pct = round((current_documented * 100.0) / max(total_fn, 1), 1) if total_fn > 0 else 100.0
            self.update_state(state="REVOKED", meta={
                "status": "stopped",
                "percent": current_pct,
                "progress": f"Auto-Documentation halted by user. Newly documented: {processed} functions. Current coverage: {current_pct}%.",
                "total": len(functions),
                "current": i,
                "total_functions": total_fn,
                "documented_functions": current_documented,
                "coverage_percent": current_pct,
                "processed": processed
            })
            return {
                "status": "stopped",
                "message": f"Auto-Documentation was stopped by user. Documented {processed} functions. Current coverage: {current_pct}%.",
                "processed": processed,
                "errors": errors,
                "total_functions": total_fn,
                "documented_functions": current_documented,
                "coverage_percent": current_pct
            }

        current_pct = round((current_documented * 100.0) / max(total_fn, 1), 1) if total_fn > 0 else 100.0
        self.update_state(state="PROGRESS", meta={
            "status": "PROGRESS",
            "percent": current_pct,
            "progress": f"Documenting {func['name']}... Project Coverage: {current_pct}% ({current_documented}/{total_fn})",
            "total": len(functions),
            "current": i + 1,
            "total_functions": total_fn,
            "documented_functions": current_documented,
            "coverage_percent": current_pct,
            "current_file": func.get("file_path", "")
        })
        
        try:
            code_snippet = (func.get("code") or "")[:2000]
            user_prompt = f"Function Name: {func['name']}\nCode:\n{code_snippet}"
            generated_doc = call_llm(user_prompt, system_prompt)
            
            # Clean up the output in case the LLM returned quotes or markdown
            generated_doc = generated_doc.strip('"""').strip("'''").strip()
            if generated_doc.startswith("```"):
                lines = generated_doc.split("\n")
                generated_doc = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
            
            with driver.session() as session:
                session.run("""
                    MATCH (f:Function)
                    WHERE f.id = $id OR elementId(f) = $id
                    SET f.docstring = $doc
                """, id=func["id"], doc=generated_doc)
            
            processed += 1
            current_documented += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"Error generating doc for {func['name']}: {e}")
            errors += 1
            
    final_pct = round((current_documented * 100.0) / max(total_fn, 1), 1) if total_fn > 0 else 100.0
    return {
        "status": "success",
        "processed": processed,
        "errors": errors,
        "total_functions": total_fn,
        "documented_functions": current_documented,
        "coverage_percent": final_pct,
        "message": f"Auto-Documentation complete! Documented {processed} functions. Coverage increased from {initial_pct}% to {final_pct}%."
    }
