import os
import re
from typing import Dict, Any, List, Optional
from app.core.analytics import calculate_blast_radius

def extract_modified_identifiers(diff_text: str) -> List[str]:
    """Extracts function and class names affected in a git diff."""
    identifiers = set()
    # Matches def name, function name, class name, func Name
    pattern = re.compile(r'^[+\- ]*(?:def|func|function|fn|class|interface)\s+([A-Za-z0-9_]+)', re.MULTILINE)
    for m in pattern.finditer(diff_text):
        name = m.group(1)
        if name not in ["if", "for", "while", "return"]:
            identifiers.add(name)
    return list(identifiers)

def generate_architectural_code_review(diff_text: str, repo_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Analyzes a Git diff against the Knowledge Graph:
    1. Identifies touched functions and classes.
    2. Computes the cumulative Blast Radius across the enterprise.
    3. Uses LLM to produce an architectural review emphasizing safety, breaking changes, and testing strategy.
    """
    touched_symbols = extract_modified_identifiers(diff_text)
    
    impact_reports = []
    total_affected_functions = set()
    total_affected_files = set()
    highest_risk = "LOW"

    for symbol in touched_symbols:
        blast = calculate_blast_radius(symbol, repo_name=repo_name)
        if blast["found"]:
            impact_reports.append(blast)
            total_affected_functions.update(blast["affected_functions"])
            total_affected_files.update(blast["affected_files"])
            if blast["risk_level"] == "CRITICAL":
                highest_risk = "CRITICAL"
            elif blast["risk_level"] == "HIGH" and highest_risk != "CRITICAL":
                highest_risk = "HIGH"
            elif blast["risk_level"] == "MEDIUM" and highest_risk == "LOW":
                highest_risk = "MEDIUM"

    context_summary = f"""
Modified Symbols Identified: {', '.join(touched_symbols) or 'None explicitly parsed'}
Total Downstream Affected Functions: {len(total_affected_functions)}
Total Downstream Affected Files: {len(total_affected_files)}
Calculated Architectural Risk: {highest_risk}
Affected Files List: {', '.join(list(total_affected_files)[:10]) or 'Local only'}
"""

    system_prompt = (
        "You are a Principal Software Architect conducting an Automated Pull Request Review. "
        "Analyze the provided Git diff and its cascading structural impact on the Knowledge Graph. "
        "Highlight breaking changes, architectural risks, and prescribe an exact regression testing checklist."
    )

    user_prompt = f"""
Git Diff to Review:
```diff
{diff_text[:3000]}
```

Knowledge Graph Blast Radius Analysis:
{context_summary}

Please provide a formal 4-part architectural review:
1. Executive Summary & Risk Rating
2. Impact Analysis on Downstream Consumers
3. Potential Architectural Smells / Regressions
4. Recommended Quality Assurance & Testing Steps
"""

    ai_review_text = ""
    # LLM integration pending update to local HF or alternative
    ai_review_text = (
            f"### Automated Architectural Review (Graph Engine)\n\n"
            f"**Overall Risk Rating:** `{highest_risk}`\n\n"
            f"**Downstream Blast Radius:**\n"
            f"- Modified Symbols: {', '.join(touched_symbols) or 'General edits'}\n"
            f"- Affected Functions: {len(total_affected_functions)} ({', '.join(list(total_affected_functions)[:8])})\n"
            f"- Affected Files: {len(total_affected_files)} files require regression testing.\n\n"
            f"{context_summary}"
        )

    return {
        "risk_rating": highest_risk,
        "touched_symbols": touched_symbols,
        "affected_functions_count": len(total_affected_functions),
        "affected_files_count": len(total_affected_files),
        "affected_files": list(total_affected_files),
        "ai_review": ai_review_text,
        "detailed_impacts": impact_reports
    }
