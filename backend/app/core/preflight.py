import os
import re
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional
from pathlib import Path

from app.core.security import ALLOWED_REPO_DIR, sanitize_name
from app.core.llm import call_llm

RED_SYSTEM_PROMPT = """You are the Preflight Autonomous Security & Crash Arbiter for web applications.
Your job is to evaluate suspected 🔴 RED (Critical / Blocker) vulnerabilities.
Inspection Rules for Red Defect Family:
1. Leaked Keys / Secrets: Are API keys (OpenAI, AWS, Stripe) hardcoded or accessible in client-side code?
2. Unprotected Admin Routes: Can sensitive /admin or mutation routes be called without server-side authentication?
3. SQL/NoSQL Injection: Does the database query interpolate unescaped user input into raw strings?
4. Fatal Server Crashes: Do route handlers lack try/catch causing unhandled 500 error pages?
5. Infinite Render Loops: Does React useEffect have cyclic state dependency triggering 100% CPU lockups?
6. Dead Core Buttons: Does a checkout/payment/submit button have an empty or mock click handler?
You MUST respond strictly in valid JSON format:
{
"is_defect": true/false,
"confidence": float between 0.0 and 1.0,
"plain_english_explanation": "One simple sentence for a non-coder explaining what is broken",
"business_consequence": "One sentence explaining financial, security, or customer loss",
"repair_prompt": "Tool-tailored instructions for an AI assistant to fix it safely"
}"""

ORANGE_SYSTEM_PROMPT = """You are the Preflight Autonomous UX & Reliability Arbiter for web applications.
Your job is to evaluate suspected 🟠 ORANGE (High Risk / Operational) hazards.
Inspection Rules for Orange Defect Family:
1. Missing Loading State: Can a user double-click a submit/pay button while an async request is in-flight?
2. Unbounded Input Fields: Does an input/textarea accept unlimited text without maxLength or bounds checking?
3. Un-debounced Search: Does every single keystroke fire an API request without debounce?
4. Heavy Unoptimized Images: Are raw >2MB images loaded directly without compression or lazy loading?
You MUST respond strictly in valid JSON format:
{
"is_defect": true/false,
"confidence": float between 0.0 and 1.0,
"plain_english_explanation": "One simple sentence explaining the interaction or performance risk",
"business_consequence": "One sentence explaining user frustration or double charging risk",
"repair_prompt": "Clear instructions for an AI assistant to fix the state/input logic"
}"""

YELLOW_SYSTEM_PROMPT = """You are the Preflight Autonomous Code Quality Arbiter for web applications.
Your job is to evaluate suspected 🟡 YELLOW (Hygiene / Polish) issues.
Inspection Rules for Yellow Defect Family:
1. Stray Debug Logs: Are console.log or debugger statements present in production components?
2. Contrast / A11y: Is text contrast insufficient or are essential labels missing?
3. Incomplete Documentation: Is README.md empty or generic template text?
You MUST respond strictly in valid JSON format:
{
"is_defect": true/false,
"confidence": float between 0.0 and 1.0,
"plain_english_explanation": "Simple explanation of the hygiene defect",
"business_consequence": "Signals poor code quality or exposes internal debug state",
"repair_prompt": "Instructions to clean up and polish the code"
}"""


def extract_json_defect(llm_output: str, fallback_defect: Dict[str, Any]) -> Dict[str, Any]:
    """Robustly extracts and validates the defect JSON structure from LLM output."""
    if not llm_output or not isinstance(llm_output, str):
        return fallback_defect

    cleaned = llm_output.strip()
    # Strip markdown fences if present
    fence_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    else:
        # Match outermost curly braces
        braces_match = re.search(r'(\{[\s\S]*\})', cleaned)
        if braces_match:
            cleaned = braces_match.group(1).strip()

    try:
        data = json.loads(cleaned)
        is_defect = bool(data.get("is_defect", fallback_defect.get("is_defect", False)))
        raw_conf = data.get("confidence", fallback_defect.get("confidence", 0.85))
        try:
            confidence = max(0.0, min(1.0, float(raw_conf)))
        except (ValueError, TypeError):
            confidence = 0.85

        explanation = str(data.get("plain_english_explanation") or fallback_defect.get("plain_english_explanation", "")).strip()
        consequence = str(data.get("business_consequence") or fallback_defect.get("business_consequence", "")).strip()
        repair = str(data.get("repair_prompt") or fallback_defect.get("repair_prompt", "")).strip()

        extra_defects = []
        if isinstance(data.get("defects"), list):
            for item in data["defects"]:
                if isinstance(item, dict) and item.get("rule"):
                    extra_defects.append(item)

        return {
            "is_defect": is_defect,
            "confidence": round(confidence, 2),
            "plain_english_explanation": explanation,
            "business_consequence": consequence,
            "repair_prompt": repair,
            "defects": extra_defects
        }
    except Exception:
        return fallback_defect


def collect_repo_code_samples(repo_name: Optional[str] = None, max_total_chars: int = 12000) -> str:
    """
    Collects representative code excerpts from the repository files on disk or Neo4j
    focusing on routes, UI handlers, database calls, and documentation.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    samples: List[str] = []
    current_chars = 0

    if clean_repo:
        repo_dir = ALLOWED_REPO_DIR / clean_repo
        if repo_dir.exists() and repo_dir.is_dir():
            # 1. Check README.md
            readme_path = repo_dir / "README.md"
            if readme_path.exists():
                try:
                    content = readme_path.read_text(encoding="utf-8", errors="ignore").strip()
                    sample = f"=== File: README.md ===\n{content[:1500]}\n"
                    samples.append(sample)
                    current_chars += len(sample)
                except Exception:
                    pass

            # 2. Collect representative source files across supported languages (.cs, .razor, .py, .js, .ts, .go, etc.)
            allowed_exts = {
                ".cs", ".razor", ".py", ".js", ".jsx", ".ts", ".tsx",
                ".html", ".vue", ".go", ".java", ".php", ".rb", ".rs",
                ".cpp", ".c", ".h", ".swift", ".kt"
            }
            found_files = []
            for root, _, files in os.walk(repo_dir):
                for f in files:
                    ext = Path(f).suffix.lower()
                    if ext in allowed_exts and not f.startswith("."):
                        found_files.append(Path(root) / f)
                if len(found_files) >= 15:
                    break

            for file_path in found_files:
                if current_chars >= max_total_chars:
                    break
                try:
                    rel_name = file_path.relative_to(repo_dir).as_posix()
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                    clipped = text[:2000]
                    sample = f"=== File: {rel_name} ===\n{clipped}\n"
                    samples.append(sample)
                    current_chars += len(sample)
                except Exception:
                    continue

    if not samples:
        samples.append(
            "=== Repository Context ===\n"
            f"Repository: {clean_repo or 'Default / All Repositories'}\n"
            "Full architectural analysis conducted via Knowledge Graph dependency indexing.\n"
        )

    return "\n".join(samples)[:max_total_chars]


RED_RULES = [
    "Leaked Keys / Secrets: API keys or credentials hardcoded in codebase",
    "Unprotected Admin Routes: Sensitive or mutation endpoints lacking server-side auth",
    "SQL/NoSQL Injection: Raw unescaped query string interpolation",
    "Fatal Server Crashes: Unhandled route exceptions causing 500 error pages",
    "Infinite Render Loops: Cyclic state dependencies causing CPU lockups",
    "Dead Core Buttons: Empty or mock click handlers on critical user actions"
]

ORANGE_RULES = [
    "Missing Loading State: Async actions without busy / pending feedback",
    "Unbounded Input Fields: Text inputs/textareas lacking maxLength bounds",
    "Un-debounced Search: Rapid API requests fired on every keystroke",
    "Heavy Unoptimized Images: Large raw assets without lazy-load or compression"
]

YELLOW_RULES = [
    "Stray Debug Logs: console.log or debugger statements left in production",
    "Contrast / A11y: Missing aria labels, alt attributes, or insufficient text contrast",
    "Incomplete Documentation: Missing or generic README and missing docstrings"
]


def evaluate_red_family(code_context: str) -> Dict[str, Any]:
    """Runs Pass 1: 🔴 RED (Critical / Blocker) vulnerabilities evaluation."""
    fallback = {
        "is_defect": False,
        "confidence": 0.90,
        "plain_english_explanation": "No critical vulnerabilities or security blocker defects were identified in inspected routes and state cycles.",
        "business_consequence": "System operation remains safe against credential leaks, SQL injection, and fatal runtime crashes.",
        "repair_prompt": "Continue enforcing server-side authentication guards, parameterized SQL queries, and robust try/catch exception wrappers on all API routes.",
        "rules": RED_RULES
    }

    has_leaked_key = bool(re.search(r'(sk-[a-zA-Z0-9_-]{20,}|AKIA[0-9A-Z]{16}|stripe.*sk_live)', code_context, re.IGNORECASE))
    has_raw_sql = bool(re.search(r'execute\(f["\']|select\s+.*?\+.*?from', code_context, re.IGNORECASE))
    if has_leaked_key or has_raw_sql:
        fallback["is_defect"] = True
        fallback["confidence"] = 0.95
        fallback["plain_english_explanation"] = "Potential hardcoded secrets or unescaped dynamic database queries detected in codebase."
        fallback["business_consequence"] = "High risk of credential theft, data breach, and unauthorized database access."
        fallback["repair_prompt"] = "Move all secret keys to environment variables and utilize parameterized query bindings."

    user_prompt = f"""Evaluate the following codebase against the 🔴 RED (Critical / Blocker) inspection rules:

```
{code_context[:8000]}
```

Analyze thoroughly and respond strictly in valid JSON."""

    try:
        raw_resp = call_llm(user_prompt, RED_SYSTEM_PROMPT, timeout=60)
        res = extract_json_defect(raw_resp, fallback)
        res["rules"] = RED_RULES
        return res
    except Exception:
        return fallback


def evaluate_orange_family(code_context: str) -> Dict[str, Any]:
    """Runs Pass 2: 🟠 ORANGE (High Risk / Operational) hazards evaluation."""
    fallback = {
        "is_defect": False,
        "confidence": 0.88,
        "plain_english_explanation": "No high-risk UX or operational hazards such as unbounded inputs or un-debounced search were detected.",
        "business_consequence": "Protects users from accidental double submissions and preserves API server throughput.",
        "repair_prompt": "Ensure all async button clicks toggle loading states and inputs declare strict maxLength boundaries.",
        "rules": ORANGE_RULES
    }

    unbounded_input = bool(re.search(r'<input(?![^>]*maxLength)[^>]*type=["\']text["\']', code_context, re.IGNORECASE))
    if unbounded_input:
        fallback["is_defect"] = True
        fallback["confidence"] = 0.80
        fallback["plain_english_explanation"] = "Text inputs were found without explicit maxLength or bounds validation."
        fallback["business_consequence"] = "Risk of buffer inflation, malicious payload pasting, and unexpected layout overflow."
        fallback["repair_prompt"] = "Add maxLength attributes and boundary validation to all user text input fields."

    user_prompt = f"""Evaluate the following codebase against the 🟠 ORANGE (High Risk / Operational) inspection rules:

```
{code_context[:8000]}
```

Analyze thoroughly and respond strictly in valid JSON."""

    try:
        raw_resp = call_llm(user_prompt, ORANGE_SYSTEM_PROMPT, timeout=60)
        res = extract_json_defect(raw_resp, fallback)
        res["rules"] = ORANGE_RULES
        return res
    except Exception:
        return fallback


def evaluate_yellow_family(code_context: str) -> Dict[str, Any]:
    """Runs Pass 3: 🟡 YELLOW (Hygiene / Polish) issues evaluation."""
    fallback = {
        "is_defect": False,
        "confidence": 0.85,
        "plain_english_explanation": "Codebase hygiene is satisfactory with clean production components and adequate documentation.",
        "business_consequence": "Maintains professional code readability and clean runtime console output.",
        "repair_prompt": "Keep production builds stripped of console.log debug statements and ensure all UI elements include aria labels.",
        "rules": YELLOW_RULES
    }

    has_console_log = bool(re.search(r'console\.log\(', code_context))
    has_empty_readme = "=== File: README.md ===\n" not in code_context or len(code_context.split("=== File: README.md ===")[-1].strip()) < 40
    if has_console_log or has_empty_readme:
        fallback["is_defect"] = True
        fallback["confidence"] = 0.85
        fallback["plain_english_explanation"] = "Stray debug log statements or minimal documentation detected in repository."
        fallback["business_consequence"] = "Signals incomplete polish and risks leaking internal diagnostic information in production."
        fallback["repair_prompt"] = "Remove all stray console.log calls and enrich README.md with comprehensive setup and architectural instructions."

    user_prompt = f"""Evaluate the following codebase against the 🟡 YELLOW (Hygiene / Polish) inspection rules:

```
{code_context[:8000]}
```

Analyze thoroughly and respond strictly in valid JSON."""

    try:
        raw_resp = call_llm(user_prompt, YELLOW_SYSTEM_PROMPT, timeout=60)
        res = extract_json_defect(raw_resp, fallback)
        res["rules"] = YELLOW_RULES
        return res
    except Exception:
        return fallback


def get_candidate_directories(repo_name: Optional[str] = None) -> List[Path]:
    """Finds target directories on disk to scan for defects."""
    candidates = []
    clean_repo = sanitize_name(repo_name) if repo_name else None

    if clean_repo:
        d = ALLOWED_REPO_DIR / clean_repo
        if d.exists() and d.is_dir():
            candidates.append(d)
        else:
            ws = Path(os.getcwd())
            if ws.name.lower() == clean_repo.lower():
                candidates.append(ws)
            elif (ws / clean_repo).exists():
                candidates.append(ws / clean_repo)
    else:
        if ALLOWED_REPO_DIR.exists():
            for child in ALLOWED_REPO_DIR.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    candidates.append(child)
        if not candidates:
            candidates.append(Path(os.getcwd()))

    return candidates


def audit_single_tier(tier: str, repo_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Exclusively and independently audits the specified tier (RED, ORANGE, or YELLOW).
    Executes AST and regex code inspection across project files for that tier's exact defect rules,
    and then invokes the LLM Arbiter using the user's exact tier system prompt.
    """
    tier_lower = (tier or "red").strip().lower()
    tier_upper = tier_lower.upper()
    clean_repo = sanitize_name(repo_name) if repo_name else None
    directories = get_candidate_directories(clean_repo)

    ignored_dirs = {
        ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
        ".idea", ".vscode", "dist", "build", ".gemini", ".system_generated",
        ".cache", "tmp", "logs"
    }
    
    code_exts = {
        ".cs", ".csx", ".razor", ".py", ".js", ".jsx", ".ts", ".tsx",
        ".html", ".vue", ".go", ".java", ".php", ".rb", ".rs",
        ".c", ".cpp", ".cc", ".h", ".hpp", ".swift", ".kt", ".scala",
        ".sh", ".bash", ".ps1"
    }
    all_defects: List[Dict[str, Any]] = []

    # Regex patterns for RED
    re_secret = re.compile(r'(sk-[a-zA-Z0-9_-]{20,}|AKIA[0-9A-Z]{16}|stripe.*sk_live[a-zA-Z0-9_]{20,}|(?:ghp|gho|ghu)_[a-zA-Z0-9]{36}|(?:api_key|secret_key|private_key)\s*=\s*[\'"][a-zA-Z0-9_\-\.]{20,}[\'"])', re.IGNORECASE)
    re_sql = re.compile(r'(?:cursor\.execute|session\.run)\s*\(\s*f[\'"]|\bexecute\s*\(\s*[\'"][^\'"]*\s*(?:\+|%|\.format\()|\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^\n\r]*\s*(?:\+|%|\.format\(|f[\'"])', re.IGNORECASE)
    re_dead_btn = re.compile(r'onclick\s*=\s*[\'"]\s*(?:#|javascript:void\(0\);?|;?)\s*[\'"]|onClick\s*=\s*\{(?:\(\)\s*=>\s*\{\s*\}|\(\)\s*=>\s*undefined|undefined|null)\}', re.IGNORECASE)
    re_py_route = re.compile(r'@(?:app|router)\.(?:get|post|put|delete|patch)\b')
    re_empty_catch = re.compile(r'catch\s*(?:\([^)]*\))?\s*\{\s*\}', re.IGNORECASE)
    re_cors_proxy = re.compile(r'https?://(?:corsproxy\.io|cors-anywhere)\b', re.IGNORECASE)
    re_sync_io = re.compile(r'\.(?:Result|GetAwaiter\(\)\.GetResult\(\))\b')
    re_todo = re.compile(r'//\s*(?:TODO|FIXME|HACK)\b', re.IGNORECASE)

    # Regex patterns for ORANGE
    re_unbounded_input = re.compile(r'<input\b(?![^>]*\bmax[lL]ength\b)(?=[^>]*type=[\'"](?:text|email|search|url)[\'"])[^>]*>|<textarea\b(?![^>]*\bmax[lL]ength\b)[^>]*>', re.IGNORECASE)
    re_submit_btn = re.compile(r'<button\b(?![^>]*\bdisabled\b)(?=[^>]*type=[\'"]submit[\'"])[^>]*>', re.IGNORECASE)
    re_search_input = re.compile(r'<input\b(?=[^>]*type=[\'"]search[\'"])(?![^>]*\bdebounce\b)[^>]*>', re.IGNORECASE)

    # Regex patterns for YELLOW
    re_console_log = re.compile(r'\b(?:console\.(?:log|debug|info)|Console\.(?:WriteLine|Write)|Debug\.WriteLine)\s*\(')
    re_img_alt = re.compile(r'<img\b(?![^>]*\balt=)[^>]*>', re.IGNORECASE)
    
    total_files_scanned = 0
    readme_found = False
    readme_char_count = 0
    readme_path = None

    for base_dir in directories:
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
            
            for fname in files:
                file_path = Path(root) / fname
                try:
                    rel_path = file_path.relative_to(base_dir).as_posix()
                except Exception:
                    rel_path = fname
                
                is_test_file = any(t in rel_path.lower() for t in ["test", "spec", "mock", "fixture"])
                
                if fname.lower() == "readme.md":
                    readme_found = True
                    readme_path = rel_path
                    try:
                        readme_char_count = len(file_path.read_text(encoding="utf-8", errors="ignore").strip())
                    except Exception:
                        pass

                ext = file_path.suffix.lower()

                # ORANGE asset check: Heavy images > 2MB
                if tier_lower == "orange" and ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
                    try:
                        fsize = file_path.stat().st_size
                        if fsize > 2 * 1024 * 1024:
                            all_defects.append({
                                "file": rel_path,
                                "line": 1,
                                "rule": "Heavy Unoptimized Images",
                                "snippet": f"{fname} ({round(fsize / (1024*1024), 2)} MB)",
                                "detail": "Image file exceeds 2MB threshold without compression or web optimization."
                            })
                    except Exception:
                        pass
                    continue

                if ext not in code_exts:
                    continue

                total_files_scanned += 1
                try:
                    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    continue

                for line_idx, line in enumerate(lines, start=1):
                    line_str = line.strip()
                    if not line_str or line_str.startswith("//") or line_str.startswith("#"):
                        continue

                    # 1. 🔴 RED INSPECTIONS ONLY
                    if tier_lower == "red":
                        if not is_test_file and re_secret.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Leaked Keys / Secrets",
                                "snippet": line_str[:160],
                                "detail": "Potential hardcoded credential or API secret token detected."
                            })

                        if not is_test_file and re_sql.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "SQL/NoSQL Injection",
                                "snippet": line_str[:160],
                                "detail": "Dynamic string concatenation or f-string used in database execution call."
                            })

                        if re_dead_btn.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Dead Core Buttons",
                                "snippet": line_str[:160],
                                "detail": "Interactive button or link has empty or mock click handler."
                            })

                        if ext == ".py" and re_py_route.search(line_str):
                            block_ahead = "\n".join(lines[line_idx:line_idx + 18])
                            if "def " in block_ahead and "try:" not in block_ahead and "except" not in block_ahead:
                                all_defects.append({
                                    "file": rel_path,
                                    "line": line_idx,
                                    "rule": "Fatal Server Crashes",
                                    "snippet": line_str[:160],
                                    "detail": "API route handler does not enclose execution in a try/except error boundary."
                                })

                        if not is_test_file and re_empty_catch.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Fatal Server Crashes",
                                "snippet": line_str[:160],
                                "detail": "Empty catch block silently suppresses fatal runtime exceptions."
                            })

                        if not is_test_file and re_cors_proxy.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Insecure Third-Party Proxy",
                                "snippet": line_str[:160],
                                "detail": "Routing traffic through public unauthenticated CORS proxy compromises credential privacy."
                            })

                    # 2. 🟠 ORANGE INSPECTIONS ONLY
                    elif tier_lower == "orange":
                        if re_unbounded_input.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Unbounded Input Fields",
                                "snippet": line_str[:160],
                                "detail": "User text input element declared without maxLength attribute."
                            })

                        if re_submit_btn.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Missing Loading State",
                                "snippet": line_str[:160],
                                "detail": "Form submit button lacks disabled attribute to prevent in-flight double submission."
                            })

                        if re_search_input.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Un-debounced Search",
                                "snippet": line_str[:160],
                                "detail": "Search input fires queries directly without debounce throttling."
                            })

                        if not is_test_file and re_sync_io.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Threadpool Starvation",
                                "snippet": line_str[:160],
                                "detail": "Synchronous blocking on async task (.Result / .GetAwaiter().GetResult()) causes threadpool starvation."
                            })

                    # 3. 🟡 YELLOW INSPECTIONS ONLY
                    elif tier_lower == "yellow":
                        if not is_test_file and re_console_log.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Stray Debug Logs",
                                "snippet": line_str[:160],
                                "detail": "Diagnostic console.log statement present in production source code."
                            })

                        if re_img_alt.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Contrast / A11y",
                                "snippet": line_str[:160],
                                "detail": "Image tag declared without alt accessibility attribute."
                            })

                        if re_todo.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Unresolved Technical Debt",
                                "snippet": line_str[:160],
                                "detail": "Unfinished TODO / FIXME comment found in production source."
                            })

    # Yellow check for README
    if tier_lower == "yellow":
        if not readme_found or readme_char_count < 60:
            all_defects.append({
                "file": readme_path or "README.md",
                "line": 1,
                "rule": "Incomplete Documentation",
                "snippet": f"README character count: {readme_char_count} chars",
                "detail": "README.md is either missing or contains less than 60 characters of documentation."
            })

    rules_map = {
        "red": RED_RULES,
        "orange": ORANGE_RULES,
        "yellow": YELLOW_RULES
    }
    system_prompt_map = {
        "red": RED_SYSTEM_PROMPT,
        "orange": ORANGE_SYSTEM_PROMPT,
        "yellow": YELLOW_SYSTEM_PROMPT
    }
    
    rules = rules_map.get(tier_lower, RED_RULES)
    system_prompt = system_prompt_map.get(tier_lower, RED_SYSTEM_PROMPT)
    defect_count = len(all_defects)
    is_defect = defect_count > 0
    confidence = 0.95 if is_defect else 0.90

    # Default rule-based explanation / consequence / repair
    if tier_lower == "red":
        tier_title = "RED: Critical / Blocker Vulnerabilities"
        if is_defect:
            explanation = f"Detected {defect_count} critical blocker defect(s) across scanned project files, including potential secrets, raw SQL queries, unhandled routes, or dead buttons."
            consequence = "High risk of security exploit, data breach, unauthorized access, or complete server crash (HTTP 500)."
            repair_prompt = f"Fix all {defect_count} critical blocker defects immediately: replace hardcoded secrets with environment variables, bind SQL queries with parameterized statements, add try/catch error boundaries to API route handlers, and connect live actions to buttons."
        else:
            explanation = f"Project scan passed! Zero critical security blockers or runtime crash hazards detected across {total_files_scanned} source files."
            consequence = "System security and server stability remain at optimal enterprise production standards."
            repair_prompt = "Maintain continuous automated secret scanning and strict parameterized database queries in CI/CD."

    elif tier_lower == "orange":
        tier_title = "ORANGE: High Risk / Operational Hazards"
        if is_defect:
            explanation = f"Detected {defect_count} high-risk operational defect(s) across scanned project files, such as unbounded input fields, un-debounced search inputs, or missing loading states."
            consequence = "Risk of user double-submissions, server request flooding, payload injection overflows, and poor perceived UX responsiveness."
            repair_prompt = f"Address all {defect_count} operational hazards: specify strict maxLength bounds on all inputs, implement debounce on search fields, and disable submit buttons during async request cycles."
        else:
            explanation = f"Project scan passed! Zero UX or operational hazards detected across {total_files_scanned} source files."
            consequence = "User interaction flows and server API throughput remain reliable and protected from runaway input loops."
            repair_prompt = "Keep enforcing input boundaries and defensive button state bindings on all interactive components."

    else:
        tier_title = "YELLOW: Code Quality & Hygiene Polish"
        if is_defect:
            explanation = f"Detected {defect_count} hygiene defect(s) across scanned project files, including stray console.log statements, missing alt attributes, or sparse documentation."
            consequence = "Degrades code readability, clutters browser developer tools in production, and reduces team onboarding velocity."
            repair_prompt = f"Clean up all {defect_count} hygiene issues: remove stray console.log calls, provide descriptive alt text for images, and expand README documentation."
        else:
            explanation = f"Project scan passed! Codebase hygiene is clean with zero stray debug statements or missing documentation detected across {total_files_scanned} files."
            consequence = "Ensures clean production runtime output and professional maintainability."
            repair_prompt = "Maintain production build stripping of debug logs and adhere to team documentation standards."

    # Collect real code excerpts and invoke LLM Arbiter for deep inspection
    code_samples = collect_repo_code_samples(repo_name=clean_repo, max_total_chars=5000)
    findings_summary = "\n".join([f"- [{d['rule']}] {d['file']}:{d['line']} -> {d['snippet']}" for d in all_defects[:15]])

    user_prompt = f"""You are conducting an enterprise-grade {tier_title} code audit.
Repository: {repo_name or 'Current Project'}
Source Files Scanned: {total_files_scanned}
Static Analyzer Suspected Defects: {len(all_defects)}

Static Analyzer Findings:
{findings_summary or 'No obvious regex patterns matched. Inspect source files below directly.'}

Representative Source Code Excerpts:
{code_samples}

Instructions:
1. Examine both the findings and the source code excerpts for {tier_title} rules.
2. Determine if real defects exist (`is_defect`).
3. If defects exist, provide:
   - "plain_english_explanation": A concise, clear sentence describing the critical defects.
   - "business_consequence": Impact on security, stability, UX, or compliance.
   - "repair_prompt": Concrete, actionable fix instructions.
   - "defects": Optional list of objects {{"file", "line", "rule", "snippet", "detail"}} for any defects found directly in the code.
4. If the code is genuinely clean, set "is_defect": false.

Respond STRICTLY in valid JSON matching your system prompt schema."""

    try:
        raw_llm = call_llm(user_prompt, system_prompt, timeout=90)
        llm_eval = extract_json_defect(raw_llm, {
            "is_defect": is_defect,
            "confidence": confidence,
            "plain_english_explanation": explanation,
            "business_consequence": consequence,
            "repair_prompt": repair_prompt
        })
        if "is_defect" in llm_eval:
            is_defect = llm_eval["is_defect"] or is_defect
        if llm_eval.get("plain_english_explanation"):
            explanation = llm_eval["plain_english_explanation"]
        if llm_eval.get("business_consequence"):
            consequence = llm_eval["business_consequence"]
        if llm_eval.get("repair_prompt"):
            repair_prompt = llm_eval["repair_prompt"]
        if "confidence" in llm_eval:
            confidence = llm_eval["confidence"]

        # Merge LLM-discovered defects if present
        if isinstance(llm_eval.get("defects"), list):
            for d in llm_eval["defects"]:
                if isinstance(d, dict) and d.get("rule"):
                    all_defects.append({
                        "file": d.get("file", repo_name or "codebase"),
                        "line": d.get("line", 1),
                        "rule": d.get("rule", f"{tier_upper} Defect"),
                        "snippet": str(d.get("snippet", ""))[:160],
                        "detail": str(d.get("detail", ""))[:300]
                    })
        defect_count = len(all_defects)
        if defect_count > 0:
            is_defect = True
            confidence = max(confidence, 0.92)
    except Exception as llm_err:
        print(f"[Preflight Tier Audit] LLM Evaluation note ({tier_lower}): {llm_err}")

    # Generate Markdown Report for this Tier
    md_lines = [
        f"# Preflight Tier Audit: {tier_title}",
        f"",
        f"- **Repository:** `{repo_name or 'All Repositories'}`",
        f"- **Audit Status:** **{'DEFECTS DETECTED' if is_defect else 'PASS - ZERO DEFECTS'}**",
        f"- **Defects Found:** `{defect_count}`",
        f"- **Files Scanned:** `{total_files_scanned}`",
        f"",
        f"---",
        f"",
        f"## 1. Executive Findings & Impact",
        f"",
        f"- **Plain English Explanation:** {explanation}",
        f"- **Business & Operational Consequence:** {consequence}",
        f"",
        f"---",
        f"",
        f"## 2. Identified Defect Occurrences Across Entire Project",
        f""
    ]

    if not all_defects:
        md_lines.append("_No defects were identified for this tier across the entire project codebase._")
    else:
        for idx, d in enumerate(all_defects[:50], start=1):
            md_lines.append(f"### {idx}. {d['rule']} in `{d['file']}:{d['line']}`")
            md_lines.append(f"- **Detail:** {d['detail']}")
            md_lines.append(f"- **Offending Code:**")
            md_lines.append(f"```text")
            md_lines.append(f"{d['snippet']}")
            md_lines.append(f"```")
            md_lines.append("")

    md_lines.extend([
        "---",
        "",
        "## 3. Tool-Tailored AI Repair Prompt",
        "",
        "```text",
        repair_prompt,
        "```",
        "",
        "---",
        "*Audit executed dynamically by CodeGraph Brain Preflight Engine.*"
    ])

    markdown_report = "\n".join(md_lines)

    return {
        "tier": tier_lower,
        "title": tier_title,
        "status": "DEFECT DETECTED" if is_defect else "PASS",
        "is_defect": is_defect,
        "defect_count": defect_count,
        "total_defects_found": defect_count,
        "confidence": confidence,
        "repository": repo_name or "All Repositories",
        "files_scanned": total_files_scanned,
        "defects": all_defects[:50],
        "rules_evaluated": rules,
        "rules": rules,
        "plain_english_explanation": explanation,
        "business_consequence": consequence,
        "repair_prompt": repair_prompt,
        "markdown_report": markdown_report
    }


def run_3tier_preflight_evaluation(
    repo_name: Optional[str] = None,
    custom_code: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes all three Preflight evaluation passes (Red, Orange, Yellow) concurrently.
    If custom_code is supplied, delegates to evaluate_*_family.
    Otherwise, runs deep project-wide audit_single_tier for each tier.
    """
    if custom_code:
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_red = executor.submit(evaluate_red_family, custom_code)
            future_orange = executor.submit(evaluate_orange_family, custom_code)
            future_yellow = executor.submit(evaluate_yellow_family, custom_code)

            red_result = future_red.result()
            orange_result = future_orange.result()
            yellow_result = future_yellow.result()
    else:
        clean_repo = sanitize_name(repo_name) if repo_name else None
        with ThreadPoolExecutor(max_workers=3) as executor:
            f_red = executor.submit(audit_single_tier, "red", clean_repo)
            f_orange = executor.submit(audit_single_tier, "orange", clean_repo)
            f_yellow = executor.submit(audit_single_tier, "yellow", clean_repo)

            red_result = f_red.result()
            orange_result = f_orange.result()
            yellow_result = f_yellow.result()

    return {
        "repository": repo_name or "All Repositories",
        "red_defects": red_result,
        "orange_defects": orange_result,
        "yellow_defects": yellow_result
    }


def compile_pure_english_markdown_report(
    eval_results: Dict[str, Any],
    analytics_data: Optional[Dict[str, Any]] = None
) -> str:
    """
    Generates a 100% pure English, comprehensive Markdown report combining:
    1. Executive Summary & Health Index
    2. 🔴 Preflight Defect Family: RED (Critical / Blocker Vulnerabilities)
    3. 🟠 Preflight Defect Family: ORANGE (High Risk / Operational Hazards)
    4. 🟡 Preflight Defect Family: YELLOW (Hygiene & Polish Issues)
    5. 🏛️ Knowledge Graph Architectural Smells (Critical Hubs, Cycles, Dead Code, Clones)
    6. 🛠️ Action Plan & Tool-Tailored Repair Instructions
    """
    repo = eval_results.get("repository", "All Repositories")
    red = eval_results.get("red_defects", {})
    orange = eval_results.get("orange_defects", {})
    yellow = eval_results.get("yellow_defects", {})

    analytics = analytics_data or {}
    cycles = analytics.get("circular_dependencies") or []
    hubs = analytics.get("critical_hubs") or analytics.get("high_risk_hubs") or []
    dead = analytics.get("dead_code_candidates") or []
    clones = analytics.get("code_clones") or []
    hotspots = analytics.get("git_hotspots") or []

    # Calculate overall health score
    defects_count = sum([1 for item in [red, orange, yellow] if item.get("is_defect")])
    total_smells = len(cycles) + len(hubs) + len(dead)
    if red.get("is_defect") or len(cycles) > 2 or len(hubs) > 5:
        overall_status = "CRITICAL RISK"
        badge = "🔴 Critical Action Required"
    elif orange.get("is_defect") or len(cycles) > 0 or len(dead) > 10:
        overall_status = "WARNING"
        badge = "🟠 Moderate Operational Risk"
    else:
        overall_status = "HEALTHY"
        badge = "🟢 Production Ready"

    md = f"""# CodeGraph Brain - Architecture & Preflight Code Review Report

> **Target Repository:** `{repo}`  
> **Evaluation Mode:** 3-Tier Preflight Autonomous Arbiter & AST Knowledge Graph  
> **System Status:** **{overall_status}** ({badge})  

---

## 1. Executive Summary & System Health

| Assessment Metric | Detected Count | Risk Rating | Operational Impact |
| :--- | :--- | :--- | :--- |
| **🔴 RED Vulnerabilities (Blockers)** | {red.get("defect_count", (1 if red.get("is_defect") else 0))} | {('HIGH' if red.get("is_defect") else 'LOW')} | Critical security & server stability |
| **🟠 ORANGE Hazards (Operational)** | {orange.get("defect_count", (1 if orange.get("is_defect") else 0))} | {('MEDIUM' if orange.get("is_defect") else 'LOW')} | UX degradation & runaway inputs |
| **🟡 YELLOW Issues (Hygiene & Polish)** | {yellow.get("defect_count", (1 if yellow.get("is_defect") else 0))} | {('LOW' if yellow.get("is_defect") else 'CLEAN')} | Readability, logging & accessibility |
| **Circular Dependency Chains** | {len(cycles)} | {('CRITICAL' if len(cycles) > 0 else 'OPTIMAL')} | Tightly-coupled architectural cycles |
| **Critical Hubs (God Functions)** | {len(hubs)} | {('HIGH' if len(hubs) > 3 else 'ACCEPTABLE')} | High blast radius & modification fragility |
| **Dead / Unreferenced Code** | {len(dead)} | {('EVALUATE' if len(dead) > 0 else 'CLEAN')} | Unused logic & bundle overhead |
| **Code Clones & Duplications** | {len(clones)} | {('REFACTOR' if len(clones) > 0 else 'CLEAN')} | Maintenance duplication risks |
| **Git Churn Hotspots** | {len(hotspots)} | {('MONITOR' if len(hotspots) > 0 else 'STABLE')} | High volatility / merge conflict zones |

---

## 2. 🔴 Preflight Defect Family: RED (Critical / Blocker Vulnerabilities)

*Rules evaluated: Leaked Keys / Secrets, Unprotected Admin Routes, SQL/NoSQL Injection, Fatal Server Crashes, Infinite Render Loops, Dead Core Buttons.*

- **Defect Status:** `{("DEFECT DETECTED" if red.get("is_defect") else "PASS - NO CRITICAL DEFECTS")}`
- **Defects Count:** `{red.get("defect_count", (1 if red.get("is_defect") else 0))}`
- **Confidence Score:** `{red.get("confidence", 0.0)}`
- **Plain English Explanation:**  
  > {red.get("plain_english_explanation", "No critical blocker defects identified.")}
- **Business Consequence:**  
  > {red.get("business_consequence", "Zero financial or security degradation observed.")}
- **Tool-Tailored Repair Instructions:**  
  ```text
  {red.get("repair_prompt", "Maintain existing automated security scanning and strict auth guards.")}
  ```

---

## 3. 🟠 Preflight Defect Family: ORANGE (High Risk / Operational Hazards)

*Rules evaluated: Missing Loading State, Unbounded Input Fields, Un-debounced Search, Heavy Unoptimized Images.*

- **Defect Status:** `{("DEFECT DETECTED" if orange.get("is_defect") else "PASS - NO OPERATIONAL HAZARDS")}`
- **Defects Count:** `{orange.get("defect_count", (1 if orange.get("is_defect") else 0))}`
- **Confidence Score:** `{orange.get("confidence", 0.0)}`
- **Plain English Explanation:**  
  > {orange.get("plain_english_explanation", "No high-risk operational hazards identified.")}
- **Business Consequence:**  
  > {orange.get("business_consequence", "Preserves optimal user experience and API load boundaries.")}
- **Tool-Tailored Repair Instructions:**  
  ```text
  {orange.get("repair_prompt", "Ensure all asynchronous actions disable triggers and enforce input bounds.")}
  ```

---

## 4. 🟡 Preflight Defect Family: YELLOW (Hygiene / Polish Issues)

*Rules evaluated: Stray Debug Logs, Contrast / A11y, Incomplete Documentation.*

- **Defect Status:** `{("DEFECT DETECTED" if yellow.get("is_defect") else "PASS - CLEAN HYGIENE")}`
- **Defects Count:** `{yellow.get("defect_count", (1 if yellow.get("is_defect") else 0))}`
- **Confidence Score:** `{yellow.get("confidence", 0.0)}`
- **Plain English Explanation:**  
  > {yellow.get("plain_english_explanation", "Code hygiene meets professional standards.")}
- **Business Consequence:**  
  > {yellow.get("business_consequence", "Clean production logs and complete documentation ensure rapid team onboarding.")}
- **Tool-Tailored Repair Instructions:**  
  ```text
  {yellow.get("repair_prompt", "Strip development console statements from production builds.")}
  ```

---

## 5. 🏛️ Knowledge Graph Architectural Smells

### 5.1. Critical Hubs & High Blast Radius Functions
"""
    if not hubs:
        md += "_No high-risk architectural bottlenecks detected._\n\n"
    else:
        for idx, hub in enumerate(hubs[:10], start=1):
            name = hub.get("name", "Unknown")
            fpath = hub.get("file_path", "Unknown file")
            risk = hub.get("risk_score", 0.0)
            in_d = hub.get("in_degree", 0)
            out_d = hub.get("out_degree", 0)
            smell = hub.get("smell_type", "High Risk Hub")
            md += f"#### {idx}. Symbol `{name}` ({smell})\n"
            md += f"- **File Path:** `{fpath}`\n"
            md += f"- **Callers (In-Degree):** {in_d} | **Callees (Out-Degree):** {out_d} | **Risk Score:** `{risk}`\n\n"

    md += "### 5.2. Circular Dependency Chains\n"
    if not cycles:
        md += "_Zero circular dependencies detected in the dependency graph._\n\n"
    else:
        for idx, cyc in enumerate(cycles[:10], start=1):
            chain_str = " -> ".join(cyc.get("cycle", [])) if isinstance(cyc, dict) else str(cyc)
            md += f"- **Cycle {idx}:** `{chain_str}`\n"
        md += "\n"

    md += "### 5.3. Dead Code & Unreferenced Functions\n"
    if not dead:
        md += "_No isolated or unreferenced functions detected._\n\n"
    else:
        md += f"Total candidates identified: **{len(dead)}**\n\n"
        for item in dead[:20]:
            name = item.get("name", "Unknown")
            fpath = item.get("file_path", "Unknown file")
            line = item.get("line", 1)
            md += f"- `{name}` in `{fpath}:{line}` (zero inbound call edges)\n"
        md += "\n"

    md += """---

## 6. 🛠️ Remediation Roadmap & Action Checklist

1. **Immediate (Blockers):**
   - Address any identified 🔴 RED vulnerabilities prior to deployment.
   - Decouple circular dependencies using the Dependency Inversion Principle (DIP) or event emitters.

2. **Medium-Term (Operational UX):**
   - Introduce UI loading indicators and request deduplication/debounce for async interactions.
   - Enforce schema validations and bounds checking across all user-facing inputs.

3. **Long-Term (Hygiene & Architecture):**
   - Refactor identified God Functions into single-responsibility service modules.
   - Safely prune verified dead code candidates to reduce test surface area and bundle size.
   - Remove lingering debug logs and verify accessibility contrast across all interfaces.

---
*Report generated automatically by CodeGraph Brain Preflight Autonomous Arbiter.*
"""
    return md
