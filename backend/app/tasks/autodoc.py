from app.core.celery import celery_app
from app.core.graph import driver
from app.core.llm import call_llm
import time

@celery_app.task(bind=True, max_retries=1)
def generate_missing_docs_task(self, repo_name: str = None):
    """
    Finds functions without a docstring in the graph and uses the configured LLM to generate them.
    """
    self.update_state(state="PROCESSING", meta={"status": "Querying functions without docstrings..."})
    
    with driver.session() as session:
        # Find functions missing docstring
        query = """
        MATCH (f:Function)
        WHERE (f.docstring IS NULL OR f.docstring = '')
        """
        if repo_name:
            query += " AND f.repo = $repo_name"
            
        query += " RETURN f.id AS id, f.name AS name, f.code AS code LIMIT 50"
        
        result = session.run(query, repo_name=repo_name)
        functions = [dict(r) for r in result]
        
    if not functions:
        return {"status": "success", "message": "No functions missing docstrings found."}
        
    system_prompt = (
        "You are an expert technical writer and senior software engineer. "
        "Your task is to write a concise, professional docstring for the provided function code. "
        "Do not include the function code or Markdown code blocks in your response. "
        "Return ONLY the raw text of the docstring."
    )
    
    processed = 0
    errors = 0
    
    for i, func in enumerate(functions):
        self.update_state(state="PROCESSING", meta={"status": f"Generating docs... {i+1}/{len(functions)} ({func['name']})"})
        
        try:
            user_prompt = f"Function Name: {func['name']}\nCode:\n{func['code'][:2000]}"
            generated_doc = call_llm(user_prompt, system_prompt)
            
            # Clean up the output in case the LLM returned quotes or markdown
            generated_doc = generated_doc.strip('"""').strip("'''").strip()
            if generated_doc.startswith("```"):
                lines = generated_doc.split("\n")
                generated_doc = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
            
            with driver.session() as session:
                session.run("""
                    MATCH (f:Function {id: $id})
                    SET f.docstring = $doc
                """, id=func["id"], doc=generated_doc)
            
            processed += 1
            time.sleep(1) # throttle a bit to avoid overwhelming local LLM
        except Exception as e:
            print(f"Error generating doc for {func['name']}: {e}")
            errors += 1
            
    return {
        "status": "success",
        "processed": processed,
        "errors": errors,
        "message": f"Generated {processed} docstrings."
    }
