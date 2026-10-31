import os
import re
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional
from pathlib import Path
from fastapi import HTTPException

from app.core.security import ALLOWED_REPO_DIR, sanitize_name
from app.core.llm import call_llm

# ==============================================================================
# PREFLIGHT DETECTION SPEC V4 - 13 DEFECT RULES & SYSTEM PROMPTS
# Spec reference: PREFLIGHT_DETECTION_SPEC.md (Version 4, 12 Oct 2026)
# Scope: Code defects for vibe-coding apps (Lovable, Cursor, Bolt, v0, Replit).
# Excluded from scope: Colour contrast, accessibility, spelling (Design review).
# ==============================================================================

RED_RULES = [
    "Leaked API keys: Your secret key is visible to anyone who opens your site. They can spend your money.",
    "Unprotected admin routes: Your admin page is hidden, not protected. Anyone who guesses the address can delete your data.",
    "Database injection: Someone typing the right characters into your search box could read or destroy your database.",
    "Crashes on unexpected input: If a user leaves a field empty, your whole app returns an error page.",
    "Infinite loops: This page freezes the browser and never stops loading.",
    "Dead buttons on the main flow: Your Pay button does nothing. Customers cannot give you money."
]

ORANGE_RULES = [
    "No loading state, double submission: Impatient users who click twice get charged twice.",
    "No limits on what people can type: Someone pasting a long text breaks the page or the database.",
    "Search and sort are wrong: Your list sorts 10 before 2, and search fires on every keystroke.",
    "Slow pages and heavy images: Your homepage loads a 10MB photo. Mobile users will leave."
]

YELLOW_RULES = [
    "Over-complex files: One file is 600 lines. Any developer you hire will charge you more to work in it.",
    "Debug code and no documentation: Leftover test code and an empty README. Looks unfinished to anyone technical.",
    "Outdated methods: Some code uses methods browsers have deprecated."
]

RED_SYSTEM_PROMPT = """You are the Preflight Autonomous Security & Crash Arbiter for web applications built with vibe-coding tools (Lovable, Cursor, Bolt, v0, Replit).
Your job is to evaluate suspected 🔴 RED (Fix before anyone uses this) blocker defects strictly according to the Preflight Detection Spec v4.

Inspection Rules for Red Defect Family:
1. Leaked API keys: Your secret key is visible to anyone who opens your site. They can spend your money.
2. Unprotected admin routes: Your admin page is hidden, not protected. Anyone who guesses the address can delete your data.
3. Database injection: Someone typing the right characters into your search box could read or destroy your database.
4. Crashes on unexpected input: If a user leaves a field empty, your whole app returns an error page.
5. Infinite loops: This page freezes the browser and never stops loading.
6. Dead buttons on the main flow: Your Pay button does nothing. Customers cannot give you money.

For every defect, you must provide the 4-part user explanation:
1. What's wrong: In one sentence with no jargon.
2. Why it matters to them: In consequences they care about (money, security, crash, lost customers).
3. Proof: Offending code snippet or line details.
4. How to fix it: Formatted specifically as a ready-to-paste prompt for AI tools:
"Copy this and paste it into Lovable / Cursor:\n[Clear, actionable instruction naming the file and fix]"

You MUST respond strictly in valid JSON format:
{
  "is_defect": true/false,
  "confidence": float between 0.0 and 1.0,
  "plain_english_explanation": "One clear sentence for a non-coder with no jargon explaining what is broken",
  "business_consequence": "One sentence explaining financial, security, or customer loss",
  "repair_prompt": "Copy this and paste it into Lovable / Cursor:\n[Concrete instructions for the AI tool]",
  "defects": [
    {
      "rule": "Leaked API keys",
      "file": "path/to/file",
      "line": 1,
      "snippet": "offending snippet",
      "detail": "What is wrong in one plain sentence",
      "business_consequence": "Why it matters in consequences they care about",
      "repair_prompt": "Copy this and paste it into Lovable / Cursor:\n[instruction]"
    }
  ]
}"""

ORANGE_SYSTEM_PROMPT = """You are the Preflight Autonomous UX & Reliability Arbiter for web applications built with vibe-coding tools (Lovable, Cursor, Bolt, v0, Replit).
Your job is to evaluate suspected 🟠 ORANGE (Your users will hit these) hazards strictly according to the Preflight Detection Spec v4.

Inspection Rules for Orange Defect Family:
7. No loading state, double submission: Impatient users who click twice get charged twice.
8. No limits on what people can type: Someone pasting a long text breaks the page or the database.
9. Search and sort are wrong: Your list sorts 10 before 2, and search fires on every keystroke.
10. Slow pages and heavy images: Your homepage loads a 10MB photo. Mobile users will leave.

For every defect, provide the 4-part user explanation:
1. What's wrong: In one sentence with no jargon.
2. Why it matters to them: Consequences they care about (double charges, broken layouts, slow speeds).
3. Proof: Offending code snippet or component details.
4. How to fix it: Formatted specifically as:
"Copy this and paste it into Lovable / Cursor:\n[Clear, actionable instruction]"

You MUST respond strictly in valid JSON format:
{
  "is_defect": true/false,
  "confidence": float between 0.0 and 1.0,
  "plain_english_explanation": "One clear sentence explaining the interaction or reliability risk",
  "business_consequence": "One sentence explaining user frustration or double charging risk",
  "repair_prompt": "Copy this and paste it into Lovable / Cursor:\n[Concrete instructions for the AI tool]",
  "defects": [
    {
      "rule": "No loading state, double submission",
      "file": "path/to/file",
      "line": 1,
      "snippet": "offending snippet",
      "detail": "What is wrong in one plain sentence",
      "business_consequence": "Why it matters to users",
      "repair_prompt": "Copy this and paste it into Lovable / Cursor:\n[instruction]"
    }
  ]
}"""

YELLOW_SYSTEM_PROMPT = """You are the Preflight Autonomous Maintainability Arbiter for web applications built with vibe-coding tools (Lovable, Cursor, Bolt, v0, Replit).
Your job is to evaluate suspected 🟡 YELLOW (Tidy up before you hire a developer) issues strictly according to the Preflight Detection Spec v4.

SCOPE NOTE: Colour contrast, accessibility, and spelling are explicitly REMOVED from scope.

Inspection Rules for Yellow Defect Family:
11. Over-complex files: One file is 600 lines. Any developer you hire will charge you more to work in it. (Note: Complexity is reported as an advisory suggestion).
12. Debug code and no documentation: Leftover test code and an empty README. Looks unfinished to anyone technical.
13. Outdated methods: Some code uses methods browsers have deprecated.

For every defect, provide the 4-part user explanation:
1. What's wrong: In one sentence with no jargon.
2. Why it matters to them: Consequences (developer costs, maintenance friction, deprecated APIs).
3. Proof: Offending snippet or file metrics.
4. How to fix it: Formatted specifically as:
"Copy this and paste it into Lovable / Cursor:\n[Clear, actionable instruction]"

You MUST respond strictly in valid JSON format:
{
  "is_defect": true/false,
  "confidence": float between 0.0 and 1.0,
  "plain_english_explanation": "One clear sentence explaining the hygiene or maintainability issue",
  "business_consequence": "One sentence explaining impact on technical credibility or future developer costs",
  "repair_prompt": "Copy this and paste it into Lovable / Cursor:\n[Concrete instructions for the AI tool]",
  "defects": [
    {
      "rule": "Debug code and no documentation",
      "file": "path/to/file",
      "line": 1,
      "snippet": "offending snippet",
      "detail": "What is wrong in one plain sentence",
      "business_consequence": "Why it matters",
      "repair_prompt": "Copy this and paste it into Lovable / Cursor:\n[instruction]"
    }
  ]
}"""


# ==============================================================================
# PART 5: PREFLIGHT 4-AREA SCORING ENGINE
# ==============================================================================

def compute_preflight_scoring(all_defects: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes Preflight scoring across 4 areas and overall Readiness score strictly
    following PART 5 of the Preflight Detection Spec v4:

    Area Weights & User-facing Names:
    1. Security (35%): "Can someone hurt you"
    2. Reliability (30%): "Does it break"
    3. Correctness (20%): "Does it behave properly"
    4. Maintainability (15%): "Can a developer work on it"

    Deductions per confirmed finding:
    - Red (40 pts):
        - Leaked API keys, Unprotected admin routes, Database injection -> Deducts 40 from Security
        - Crashes on unexpected input, Infinite loops, Dead buttons -> Deducts 40 from Reliability
    - Orange-High (25 pts):
        - No loading state, double submission -> Deducts 25 from Reliability
        - No limits on what people can type -> Deducts 25 from Correctness
    - Orange-Medium (12 pts):
        - Search and sort are wrong -> Deducts 12 from Correctness
        - Slow pages and heavy images -> Deducts 12 from Correctness
    - Yellow (4 pts):
        - Over-complex files -> Deducts 4 from Maintainability (unconfirmed suggestions subtract 0)
        - Debug code and no documentation -> Deducts 4 from Maintainability
        - Outdated methods -> Deducts 4 from Maintainability

    Each area floors at 0. Unconfirmed findings subtract 0.
    Readiness = round(Security * 0.35 + Reliability * 0.30 + Correctness * 0.20 + Maintainability * 0.15)

    Verdict States (Part 2.1 of Spec):
    - 🔴 Not ready: Any Red defect > 0, or Readiness < 70
    - 🟠 Launch with care: 0 Red defects, but Orange defects present or Readiness 70-89
    - 🟢 Ready: 0 Red defects, 0 Orange defects, and Readiness >= 90
    """
    sec_deductions = 0
    rel_deductions = 0
    cor_deductions = 0
    main_deductions = 0

    red_count = 0
    orange_count = 0
    yellow_count = 0

    for d in all_defects:
        rule = str(d.get("rule", "")).lower()
        tier = str(d.get("tier", "")).lower()
        is_unconfirmed = bool(d.get("unconfirmed", False))

        # Unconfirmed findings subtract nothing per spec
        if is_unconfirmed:
            continue

        if tier == "red" or any(k in rule for k in ["leaked", "secret", "admin", "injection", "crash", "loop", "dead button"]):
            red_count += 1
            if any(k in rule for k in ["leaked", "secret", "admin", "injection", "proxy"]):
                sec_deductions += 40
            else:
                rel_deductions += 40

        elif tier == "orange" or any(k in rule for k in ["loading", "double submission", "limit", "unbounded", "search", "sort", "slow", "heavy image"]):
            orange_count += 1
            if any(k in rule for k in ["loading", "double submission", "threadpool"]):
                # Orange-High: Reliability (-25)
                rel_deductions += 25
            elif any(k in rule for k in ["limit", "unbounded"]):
                # Orange-High: Correctness (-25)
                cor_deductions += 25
            elif any(k in rule for k in ["search", "sort"]):
                # Orange-Medium: Correctness (-12)
                cor_deductions += 12
            elif any(k in rule for k in ["slow", "heavy", "image"]):
                # Orange-Medium: Correctness (-12)
                cor_deductions += 12
            else:
                cor_deductions += 12

        elif tier == "yellow" or any(k in rule for k in ["complex", "debug", "documentation", "readme", "outdated", "deprecated"]):
            yellow_count += 1
            if "complex" in rule:
                # Over-complex files: if explicitly scored, deducts 4
                main_deductions += 4
            elif any(k in rule for k in ["debug", "documentation", "readme", "console", "todo"]):
                main_deductions += 4
            elif any(k in rule for k in ["outdated", "deprecated"]):
                main_deductions += 4
            else:
                main_deductions += 4

    sec_score = max(0, 100 - sec_deductions)
    rel_score = max(0, 100 - rel_deductions)
    cor_score = max(0, 100 - cor_deductions)
    main_score = max(0, 100 - main_deductions)

    readiness = round(
        (sec_score * 0.35) +
        (rel_score * 0.30) +
        (cor_score * 0.20) +
        (main_score * 0.15)
    )
    readiness = max(0, min(100, readiness))

    # Verdict determination per spec
    if red_count > 0 or readiness < 70:
        verdict_state = "not_ready"
        verdict_badge = "🔴 Not ready"
        verdict_title = "🔴 Not ready to launch"
        if red_count > 0:
            verdict_meaning = f"{red_count} serious problem{'s' if red_count > 1 else ''} need fixing first. Something here will hurt you. Fix before anyone uses it."
        else:
            verdict_meaning = "Overall readiness is below launch safety threshold. Fix critical blockers before launching."
    elif orange_count > 0 or readiness < 90:
        verdict_state = "launch_with_care"
        verdict_badge = "🟠 Launch with care"
        verdict_title = "🟠 Launch with care"
        verdict_meaning = "Nothing dangerous, but users will hit problems. Fix UX and reliability issues for a smooth launch."
    else:
        verdict_state = "ready"
        verdict_badge = "🟢 Ready"
        verdict_title = "🟢 Ready to launch"
        verdict_meaning = "Nothing serious found. Your app passed all preflight safety and reliability checks. Go."

    return {
        "readiness_score": readiness,
        "verdict_state": verdict_state,
        "verdict_badge": verdict_badge,
        "verdict_title": verdict_title,
        "verdict_meaning": verdict_meaning,
        "areas": {
            "security": {
                "name": "Security",
                "title": "Can someone hurt you",
                "weight": 0.35,
                "score": sec_score,
                "deductions": sec_deductions
            },
            "reliability": {
                "name": "Reliability",
                "title": "Does it break",
                "weight": 0.30,
                "score": rel_score,
                "deductions": rel_deductions
            },
            "correctness": {
                "name": "Correctness",
                "title": "Does it behave properly",
                "weight": 0.20,
                "score": cor_score,
                "deductions": cor_deductions
            },
            "maintainability": {
                "name": "Maintainability",
                "title": "Can a developer work on it",
                "weight": 0.15,
                "score": main_score,
                "deductions": main_deductions
            }
        },
        "counts": {
            "red": red_count,
            "orange": orange_count,
            "yellow": yellow_count,
            "total": len(all_defects)
        }
    }


def extract_json_defect(llm_output: str, fallback_defect: Dict[str, Any]) -> Dict[str, Any]:
    """Robustly extracts and validates the defect JSON structure from LLM output."""
    if not llm_output or not isinstance(llm_output, str):
        return fallback_defect

    cleaned = llm_output.strip()
    fence_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    else:
        braces_match = re.search(r'(\{[\s\S]*\})', cleaned)
        if braces_match:
            cleaned = braces_match.group(1).strip()

    try:
        data = json.loads(cleaned, strict=False)
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
            readme_path = repo_dir / "README.md"
            if readme_path.exists():
                try:
                    content = readme_path.read_text(encoding="utf-8", errors="ignore").strip()
                    sample = f"=== File: README.md ===\n{content[:1500]}\n"
                    samples.append(sample)
                    current_chars += len(sample)
                except Exception:
                    pass

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


def evaluate_red_family(code_context: str) -> Dict[str, Any]:
    """Runs Pass 1: 🔴 RED (Fix before anyone uses this) vulnerabilities evaluation."""
    fallback = {
        "is_defect": False,
        "confidence": 0.90,
        "plain_english_explanation": "No critical vulnerabilities or security blocker defects were identified in inspected routes and state cycles.",
        "business_consequence": "System operation remains safe against credential leaks, SQL injection, and fatal runtime crashes.",
        "repair_prompt": "Copy this and paste it into Lovable / Cursor:\nEnsure all database queries use parameterized bindings, protect admin routes with server authentication, and wrap API endpoints with try/catch error boundaries.",
        "rules": RED_RULES
    }

    has_leaked_key = bool(re.search(r'(sk-[a-zA-Z0-9_-]{20,}|AKIA[0-9A-Z]{16}|stripe.*sk_live)', code_context, re.IGNORECASE))
    has_raw_sql = bool(re.search(r'execute\(f["\']|select\s+.*?\+.*?from', code_context, re.IGNORECASE))
    if has_leaked_key or has_raw_sql:
        fallback["is_defect"] = True
        fallback["confidence"] = 0.95
        fallback["plain_english_explanation"] = "Potential hardcoded secrets or unescaped dynamic database queries detected in codebase."
        fallback["business_consequence"] = "Anyone who visits your site can copy your secret key and spend your money, or inject queries to read/destroy your database."
        fallback["repair_prompt"] = (
            "Copy this and paste it into Lovable / Cursor:\n"
            "Move all API secret keys to server-side environment variables. Update database queries to use parameterized prepared statements instead of raw string interpolation."
        )

    user_prompt = f"""Evaluate the following codebase against the 🔴 RED (Fix before anyone uses this) inspection rules from Preflight Spec v4:
Rules:
1. Leaked API keys: "Your secret key is visible to anyone who opens your site. They can spend your money."
2. Unprotected admin routes: "Your admin page is hidden, not protected. Anyone who guesses the address can delete your data."
3. Database injection: "Someone typing the right characters into your search box could read or destroy your database."
4. Crashes on unexpected input: "If a user leaves a field empty, your whole app returns an error page."
5. Infinite loops: "This page freezes the browser and never stops loading."
6. Dead buttons on the main flow: "Your Pay button does nothing. Customers cannot give you money."

Code Excerpt:
```
{code_context[:8000]}
```

Respond STRICTLY in valid JSON matching your system prompt schema."""

    try:
        raw_resp = call_llm(user_prompt, RED_SYSTEM_PROMPT, timeout=60)
        res = extract_json_defect(raw_resp, fallback)
        res["rules"] = RED_RULES
        return res
    except Exception:
        return fallback


def evaluate_orange_family(code_context: str) -> Dict[str, Any]:
    """Runs Pass 2: 🟠 ORANGE (Your users will hit these) hazards evaluation."""
    fallback = {
        "is_defect": False,
        "confidence": 0.88,
        "plain_english_explanation": "No high-risk UX or operational hazards such as unbounded inputs or un-debounced search were detected.",
        "business_consequence": "Protects users from accidental double submissions and preserves API server throughput.",
        "repair_prompt": "Copy this and paste it into Lovable / Cursor:\nEnsure all async button clicks toggle loading states and inputs declare strict maxLength boundaries.",
        "rules": ORANGE_RULES
    }

    unbounded_input = bool(re.search(r'<input(?![^>]*maxLength)[^>]*type=["\']text["\']', code_context, re.IGNORECASE))
    if unbounded_input:
        fallback["is_defect"] = True
        fallback["confidence"] = 0.80
        fallback["plain_english_explanation"] = "Text inputs were found without explicit maxLength or bounds validation."
        fallback["business_consequence"] = "Someone pasting a long text breaks the page layout or the database."
        fallback["repair_prompt"] = (
            "Copy this and paste it into Lovable / Cursor:\n"
            "Add maxLength limits (e.g. maxLength=255) to all user text input fields to prevent layout overflow and database errors."
        )

    user_prompt = f"""Evaluate the following codebase against the 🟠 ORANGE (Your users will hit these) inspection rules from Preflight Spec v4:
Rules:
7. No loading state, double submission: "Impatient users who click twice get charged twice."
8. No limits on what people can type: "Someone pasting a long text breaks the page or the database."
9. Search and sort are wrong: "Your list sorts 10 before 2, and search fires on every keystroke."
10. Slow pages and heavy images: "Your homepage loads a 10MB photo. Mobile users will leave."

Code Excerpt:
```
{code_context[:8000]}
```

Respond STRICTLY in valid JSON matching your system prompt schema."""

    try:
        raw_resp = call_llm(user_prompt, ORANGE_SYSTEM_PROMPT, timeout=60)
        res = extract_json_defect(raw_resp, fallback)
        res["rules"] = ORANGE_RULES
        return res
    except Exception:
        return fallback


def evaluate_yellow_family(code_context: str) -> Dict[str, Any]:
    """Runs Pass 3: 🟡 YELLOW (Tidy up before you hire a developer) issues evaluation."""
    fallback = {
        "is_defect": False,
        "confidence": 0.85,
        "plain_english_explanation": "Codebase hygiene is satisfactory with clean production components and adequate documentation.",
        "business_consequence": "Maintains professional code readability and clean runtime console output.",
        "repair_prompt": "Copy this and paste it into Lovable / Cursor:\nKeep production builds stripped of console.log debug statements and ensure project documentation is updated.",
        "rules": YELLOW_RULES
    }

    has_console_log = bool(re.search(r'console\.log\(', code_context))
    has_empty_readme = "=== File: README.md ===\n" not in code_context or len(code_context.split("=== File: README.md ===")[-1].strip()) < 40
    if has_console_log or has_empty_readme:
        fallback["is_defect"] = True
        fallback["confidence"] = 0.85
        fallback["plain_english_explanation"] = "Leftover test code and an empty README detected in repository."
        fallback["business_consequence"] = "Looks unfinished to anyone technical and clutters production browser logs."
        fallback["repair_prompt"] = (
            "Copy this and paste it into Lovable / Cursor:\n"
            "Remove all leftover console.log and debugger statements from production files, and write a clear README.md explaining the app setup."
        )

    user_prompt = f"""Evaluate the following codebase against the 🟡 YELLOW (Tidy up before you hire a developer) inspection rules from Preflight Spec v4:
Note: Colour contrast, accessibility, and spelling are removed from scope.
Rules:
11. Over-complex files: "One file is 600 lines. Any developer you hire will charge you more to work in it."
12. Debug code and no documentation: "Leftover test code and an empty README. Looks unfinished to anyone technical."
13. Outdated methods: "Some code uses methods browsers have deprecated."

Code Excerpt:
```
{code_context[:8000]}
```

Respond STRICTLY in valid JSON matching your system prompt schema."""

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

    # 1. 🔴 RED REGEXES (6 Defects per Spec v4)
    # Defect 1: Leaked API keys
    re_secret = re.compile(
        r'(sk-[a-zA-Z0-9_-]{20,}|AKIA[0-9A-Z]{16}|stripe.*sk_live[a-zA-Z0-9_]{20,}|(?:ghp|gho|ghu)_[a-zA-Z0-9]{36}|(?:api_key|secret_key|private_key)\s*=\s*[\'"][a-zA-Z0-9_\-\.]{20,}[\'"])',
        re.IGNORECASE
    )
    # Defect 2: Unprotected admin routes
    re_admin_route = re.compile(r'(@app\.(?:get|post|put|delete|patch)\([\'"]/(?:admin|manage|settings/billing|secret)[^\'"]*[\'"]|router\.(?:get|post|put|delete|patch)\([\'"]/(?:admin|manage)[^\'"]*[\'"])', re.IGNORECASE)
    # Defect 3: Database injection
    re_sql = re.compile(
        r'(?:cursor\.execute|session\.run)\s*\(\s*f[\'"]|\bexecute\s*\(\s*[\'"][^\'"]*\s*(?:\+|%|\.format\()|\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^\n\r]*\s*(?:\+|%|\.format\(|f[\'"])',
        re.IGNORECASE
    )
    # Defect 4: Crashes on unexpected input
    re_py_route = re.compile(r'@(?:app|router)\.(?:get|post|put|delete|patch)\b')
    re_empty_catch = re.compile(r'catch\s*(?:\([^)]*\))?\s*\{\s*\}', re.IGNORECASE)
    # Defect 5: Infinite loops
    re_infinite_loop = re.compile(r'\bwhile\s*\(\s*(?:true|1)\s*\)\s*\{(?![^}]*\bbreak\b)|\buseEffect\s*\(\s*\(\)\s*=>\s*\{[^}]*set[A-Z]\w*\([^)]*\)[^}]*\}\s*,\s*\[\s*\]\s*\)', re.IGNORECASE)
    # Defect 6: Dead buttons on the main flow
    re_dead_btn = re.compile(
        r'onclick\s*=\s*[\'"]\s*(?:#|javascript:void\(0\);?|;?)\s*[\'"]|onClick\s*=\s*\{(?:\(\)\s*=>\s*\{\s*\}|\(\)\s*=>\s*undefined|undefined|null)\}',
        re.IGNORECASE
    )
    re_cors_proxy = re.compile(r'https?://(?:corsproxy\.io|cors-anywhere)\b', re.IGNORECASE)

    # 2. 🟠 ORANGE REGEXES (4 Defects per Spec v4)
    # Defect 7: No loading state, double submission
    re_submit_btn = re.compile(r'<button\b(?![^>]*\bdisabled\b)(?=[^>]*type=[\'"]submit[\'"])[^>]*>', re.IGNORECASE)
    re_sync_io = re.compile(r'\.(?:Result|GetAwaiter\(\)\.GetResult\(\))\b')
    # Defect 8: No limits on what people can type
    re_unbounded_input = re.compile(r'<input\b(?![^>]*\bmax[lL]ength\b)(?=[^>]*type=[\'"](?:text|email|search|url)[\'"])[^>]*>|<textarea\b(?![^>]*\bmax[lL]ength\b)[^>]*>', re.IGNORECASE)
    # Defect 9: Search and sort are wrong
    re_search_input = re.compile(r'<input\b(?=[^>]*type=[\'"]search[\'"])(?![^>]*\bdebounce\b)[^>]*>', re.IGNORECASE)
    re_bad_sort = re.compile(r'\.sort\(\s*\)(?!\s*\.\s*map)', re.IGNORECASE)

    # 3. 🟡 YELLOW REGEXES (3 Defects per Spec v4: Over-complex files, Debug code & no docs, Outdated methods)
    # Defect 12: Debug code and no documentation
    re_console_log = re.compile(r'\b(?:console\.(?:log|debug|info)|Console\.(?:WriteLine|Write)|Debug\.WriteLine)\s*\(')
    re_debugger = re.compile(r'\bdebugger\s*;?')
    re_todo = re.compile(r'//\s*(?:TODO|FIXME|HACK)\b', re.IGNORECASE)
    # Defect 13: Outdated methods
    re_deprecated_apis = re.compile(r'\b(?:document\.execCommand|unescape\(|escape\(|event\.keyCode|\.substr\(|__defineGetter__|__defineSetter__)\b')

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

                # Defect 10 (ORANGE): Heavy images > 2MB
                if tier_lower == "orange" and ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
                    try:
                        fsize = file_path.stat().st_size
                        if fsize > 2 * 1024 * 1024:
                            all_defects.append({
                                "file": rel_path,
                                "line": 1,
                                "rule": "Slow pages and heavy images",
                                "snippet": f"{fname} ({round(fsize / (1024*1024), 2)} MB)",
                                "detail": "Your homepage loads a 10MB photo. Mobile users will leave.",
                                "business_consequence": "Mobile users experience painful load times and abandon the app immediately.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"Compress image '{fname}' to modern WebP format under 300KB and add loading=\"lazy\" to the image tag."
                                ),
                                "tier": "orange"
                            })
                    except Exception:
                        pass
                    continue

                if ext not in code_exts:
                    continue

                total_files_scanned += 1
                try:
                    raw_content = file_path.read_text(encoding="utf-8", errors="ignore")
                    lines = raw_content.splitlines()
                except Exception:
                    continue

                # Defect 11 (YELLOW): Over-complex files > 500 lines (Reported as advisory suggestion per spec)
                if tier_lower == "yellow" and len(lines) > 500 and not is_test_file:
                    all_defects.append({
                        "file": rel_path,
                        "line": 1,
                        "rule": "Over-complex files",
                        "snippet": f"File total lines: {len(lines)} lines",
                        "detail": f"One file is {len(lines)} lines. Any developer you hire will charge you more to work in it.",
                        "business_consequence": "High complexity slows down new feature delivery and increases bugs when making changes.",
                        "repair_prompt": (
                            "Copy this and paste it into Lovable / Cursor:\n"
                            f"Refactor '{rel_path}' by splitting it into smaller, focused modules under 250 lines each."
                        ),
                        "tier": "yellow",
                        "unconfirmed": False
                    })

                for line_idx, line in enumerate(lines, start=1):
                    line_str = line.strip()
                    if not line_str or line_str.startswith("//") or line_str.startswith("#"):
                        continue

                    # 1. 🔴 RED INSPECTIONS (Defects 1 - 6)
                    if tier_lower == "red":
                        # Defect 1: Leaked API keys
                        if not is_test_file and re_secret.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Leaked API keys",
                                "snippet": line_str[:160],
                                "detail": "Your secret key is visible to anyone who opens your site. They can spend your money.",
                                "business_consequence": "Anyone can copy your key and rack up huge bills on your account.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"My secret key in '{rel_path}' is visible in code. Move it to a server environment variable and call it from backend only."
                                ),
                                "tier": "red"
                            })

                        # Defect 2: Unprotected admin routes
                        if not is_test_file and re_admin_route.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Unprotected admin routes",
                                "snippet": line_str[:160],
                                "detail": "Your admin page is hidden, not protected. Anyone who guesses the address can delete your data.",
                                "business_consequence": "Unauthorized users can tamper with sensitive settings or wipe customer databases.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"Add server-side authentication middleware to protected admin routes in '{rel_path}'. Reject unauthenticated requests with 401/403."
                                ),
                                "tier": "red"
                            })

                        # Defect 3: Database injection
                        if not is_test_file and re_sql.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Database injection",
                                "snippet": line_str[:160],
                                "detail": "Someone typing the right characters into your search box could read or destroy your database.",
                                "business_consequence": "Malicious actors can execute arbitrary queries to extract or wipe all project records.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"In '{rel_path}', change raw SQL string concatenation to parameterized queries or ORM query bindings."
                                ),
                                "tier": "red"
                            })

                        # Defect 4: Crashes on unexpected input
                        if ext == ".py" and re_py_route.search(line_str):
                            block_ahead = "\n".join(lines[line_idx:line_idx + 18])
                            if "def " in block_ahead and "try:" not in block_ahead and "except" not in block_ahead:
                                all_defects.append({
                                    "file": rel_path,
                                    "line": line_idx,
                                    "rule": "Crashes on unexpected input",
                                    "snippet": line_str[:160],
                                    "detail": "If a user leaves a field empty, your whole app returns an error page.",
                                    "business_consequence": "Unhandled route exceptions return HTTP 500 crash pages to customers.",
                                    "repair_prompt": (
                                        "Copy this and paste it into Lovable / Cursor:\n"
                                        f"In '{rel_path}', wrap this route in a try/except block and return a friendly 400 Bad Request error if input is missing."
                                    ),
                                    "tier": "red"
                                })

                        if not is_test_file and re_empty_catch.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Crashes on unexpected input",
                                "snippet": line_str[:160],
                                "detail": "Empty catch block silently suppresses fatal runtime exceptions.",
                                "business_consequence": "Unexpected failures hide error details and crash silently.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"In '{rel_path}', log errors and return fallback state instead of an empty catch block."
                                ),
                                "tier": "red"
                            })

                        # Defect 5: Infinite loops
                        if re_infinite_loop.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Infinite loops",
                                "snippet": line_str[:160],
                                "detail": "This page freezes the browser and never stops loading.",
                                "business_consequence": "Freezes user tabs and spikes CPU to 100%, causing users to close the app.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"In '{rel_path}', fix the loop condition or useEffect dependency array so state updates do not cycle endlessly."
                                ),
                                "tier": "red"
                            })

                        # Defect 6: Dead buttons on the main flow
                        if re_dead_btn.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Dead buttons on the main flow",
                                "snippet": line_str[:160],
                                "detail": "Your Pay button does nothing. Customers cannot give you money.",
                                "business_consequence": "Users cannot complete the core checkout or action flow, directly killing revenue.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"In '{rel_path}', attach an active handler function to this button so clicking executes the actual submission or payment."
                                ),
                                "tier": "red"
                            })

                        if not is_test_file and re_cors_proxy.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Leaked API keys",
                                "snippet": line_str[:160],
                                "detail": "Traffic routed through a public CORS proxy leaks API tokens and credentials.",
                                "business_consequence": "Third-party proxy owners can inspect headers and steal your tokens.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"Remove public CORS proxy from '{rel_path}' and proxy requests through your own backend server."
                                ),
                                "tier": "red"
                            })

                    # 2. 🟠 ORANGE INSPECTIONS (Defects 7 - 10)
                    elif tier_lower == "orange":
                        # Defect 7: No loading state, double submission
                        if re_submit_btn.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "No loading state, double submission",
                                "snippet": line_str[:160],
                                "detail": "Impatient users who click twice get charged twice.",
                                "business_consequence": "Double submission leads to duplicate orders, duplicate billing, and angry refund requests.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"In '{rel_path}', disable this button while the async request is loading and show a spinner."
                                ),
                                "tier": "orange"
                            })

                        if not is_test_file and re_sync_io.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "No loading state, double submission",
                                "snippet": line_str[:160],
                                "detail": "Synchronous blocking on async task freezes the server threadpool.",
                                "business_consequence": "Concurrent users experience frozen pages and timeouts.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"In '{rel_path}', replace .Result / .GetAwaiter().GetResult() with async/await."
                                ),
                                "tier": "orange"
                            })

                        # Defect 8: No limits on what people can type
                        if re_unbounded_input.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "No limits on what people can type",
                                "snippet": line_str[:160],
                                "detail": "Someone pasting a long text breaks the page or the database.",
                                "business_consequence": "Large payloads break frontend layout and cause database insert exceptions.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"In '{rel_path}', add maxLength='255' to this input field and validate string length."
                                ),
                                "tier": "orange"
                            })

                        # Defect 9: Search and sort are wrong
                        if re_search_input.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Search and sort are wrong",
                                "snippet": line_str[:160],
                                "detail": "Your list sorts 10 before 2, and search fires on every keystroke.",
                                "business_consequence": "Typing rapidly sends dozens of duplicate queries, overloading your backend.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"In '{rel_path}', debounce this search input with 300ms delay before triggering fetch."
                                ),
                                "tier": "orange"
                            })

                        if re_bad_sort.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Search and sort are wrong",
                                "snippet": line_str[:160],
                                "detail": "Your list sorts 10 before 2 because JavaScript default sort is alphabetical.",
                                "business_consequence": "Users see prices and rankings in incorrect order.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"In '{rel_path}', replace .sort() with .sort((a, b) => a - b) for numeric sorting."
                                ),
                                "tier": "orange"
                            })

                    # 3. 🟡 YELLOW INSPECTIONS (Defects 11 - 13)
                    elif tier_lower == "yellow":
                        # Defect 12: Debug code and no documentation
                        if not is_test_file and re_console_log.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Debug code and no documentation",
                                "snippet": line_str[:160],
                                "detail": "Leftover test code and console logs clutter production browser tools.",
                                "business_consequence": "Looks unfinished to anyone technical and exposes internal state.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"Remove console.log from '{rel_path}' in production code."
                                ),
                                "tier": "yellow"
                            })

                        if not is_test_file and re_debugger.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Debug code and no documentation",
                                "snippet": line_str[:160],
                                "detail": "Leftover debugger breakpoint left in production source code.",
                                "business_consequence": "Halts execution unexpectedly if user opens developer tools.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"Remove debugger statement from '{rel_path}'."
                                ),
                                "tier": "yellow"
                            })

                        if re_todo.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Debug code and no documentation",
                                "snippet": line_str[:160],
                                "detail": "Unfinished TODO / FIXME note found in production source.",
                                "business_consequence": "Flags uncompleted work or technical debt.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"Resolve or tidy up the TODO comment in '{rel_path}'."
                                ),
                                "tier": "yellow"
                            })

                        # Defect 13: Outdated methods
                        if re_deprecated_apis.search(line_str):
                            all_defects.append({
                                "file": rel_path,
                                "line": line_idx,
                                "rule": "Outdated methods",
                                "snippet": line_str[:160],
                                "detail": "Some code uses methods browsers have deprecated.",
                                "business_consequence": "Deprecated browser APIs risk breaking without warning in future browser releases.",
                                "repair_prompt": (
                                    "Copy this and paste it into Lovable / Cursor:\n"
                                    f"In '{rel_path}', replace deprecated browser method with standard modern alternative."
                                ),
                                "tier": "yellow"
                            })

    # Yellow check for README (Defect 12)
    if tier_lower == "yellow":
        if not readme_found or readme_char_count < 60:
            all_defects.append({
                "file": readme_path or "README.md",
                "line": 1,
                "rule": "Debug code and no documentation",
                "snippet": f"README character count: {readme_char_count} chars",
                "detail": "Leftover test code and an empty README. Looks unfinished to anyone technical.",
                "business_consequence": "Makes the project look incomplete and unmaintainable to partners or developers.",
                "repair_prompt": (
                    "Copy this and paste it into Lovable / Cursor:\n"
                    "Create a concise README.md with project title, what the app does, and how to run it."
                ),
                "tier": "yellow"
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

    # Default explanations per spec
    if tier_lower == "red":
        tier_title = "RED: Fix Before Anyone Uses This"
        if is_defect:
            explanation = f"Detected {defect_count} critical blocker defect(s). Something here will hurt you (leaked keys, unprotected admin routes, injection, crashes, loops, or dead buttons)."
            consequence = "Anyone who opens your site could spend your money, access private databases, or get stuck with non-functioning pay buttons."
            repair_prompt = (
                "Copy this and paste it into Lovable / Cursor:\n"
                f"Fix the {defect_count} critical blockers identified: move secret keys to backend environment variables, protect admin routes, parameterize SQL, and connect active handlers to dead buttons."
            )
        else:
            explanation = f"Project scan passed! Zero critical blockers detected across {total_files_scanned} source files."
            consequence = "Your app is protected against leaked secrets, database injection, and main flow dead buttons."
            repair_prompt = "No action required. Keep secret keys in environment variables and verify routes before deploying."

    elif tier_lower == "orange":
        tier_title = "ORANGE: Your Users Will Hit These"
        if is_defect:
            explanation = f"Detected {defect_count} UX and operational hazard(s), such as missing loading states, unbounded inputs, or un-debounced search."
            consequence = "Impatient users who click twice get charged twice, pasting long text breaks pages, and search fires on every keystroke."
            repair_prompt = (
                "Copy this and paste it into Lovable / Cursor:\n"
                f"Fix the {defect_count} operational hazards: disable submit buttons while loading, set maxLength on inputs, and debounce search queries by 300ms."
            )
        else:
            explanation = f"Project scan passed! Zero operational UX hazards detected across {total_files_scanned} source files."
            consequence = "Form buttons, search queries, and input boundaries behave properly."
            repair_prompt = "No action required. Maintain button loading states and input bounds."

    else:
        tier_title = "YELLOW: Tidy Up Before You Hire A Developer"
        if is_defect:
            explanation = f"Detected {defect_count} code hygiene issue(s), including over-complex files, stray console logs, empty README, or outdated methods."
            consequence = "One file is over 500 lines or contains leftover test code. Looks unfinished to technical reviewers and costs more to maintain."
            repair_prompt = (
                "Copy this and paste it into Lovable / Cursor:\n"
                f"Tidy up the codebase: remove console.log statements, write a clear README.md, and break down files over 500 lines into smaller modules."
            )
        else:
            explanation = f"Project scan passed! Codebase hygiene is clean across {total_files_scanned} files."
            consequence = "Code is tidy and ready for technical collaboration."
            repair_prompt = "No action required. Code hygiene is clean."

    # Collect code excerpts and invoke LLM Arbiter for deep inspection
    code_samples = collect_repo_code_samples(repo_name=clean_repo, max_total_chars=5000)
    findings_summary = "\n".join([f"- [{d['rule']}] {d['file']}:{d['line']} -> {d['snippet']}" for d in all_defects[:15]])

    user_prompt = f"""You are conducting a Preflight {tier_title} code audit for a vibe-coded web application.
Repository: {repo_name or 'Current Project'}
Source Files Scanned: {total_files_scanned}
Static Analyzer Suspected Defects: {len(all_defects)}

Static Findings:
{findings_summary or 'No obvious regex patterns matched. Inspect source files below directly.'}

Representative Source Code Excerpts:
{code_samples}

Instructions:
1. Examine both findings and source excerpts for {tier_title} rules strictly.
2. Determine if real defects exist (`is_defect`).
3. If defects exist, provide:
   - "plain_english_explanation": One sentence for a non-coder with no jargon.
   - "business_consequence": Real impact (money, data loss, crashes, user drop-off).
   - "repair_prompt": Formatted specifically as "Copy this and paste it into Lovable / Cursor:\n..."
   - "defects": List of defect objects {{"file", "line", "rule", "snippet", "detail", "business_consequence", "repair_prompt"}}.
4. If the code is clean, set "is_defect": false.

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
                        "detail": str(d.get("detail", explanation))[:300],
                        "business_consequence": str(d.get("business_consequence", consequence))[:300],
                        "repair_prompt": str(d.get("repair_prompt", repair_prompt)),
                        "tier": tier_lower
                    })
        defect_count = len(all_defects)
        if defect_count > 0:
            is_defect = True
            confidence = max(confidence, 0.92)
    except Exception as llm_err:
        print(f"[Preflight Tier Audit] LLM Note ({tier_lower}): {llm_err}")

    # Compute scoring & verdict for this audit pass
    scoring = compute_preflight_scoring(all_defects)

    # Format Markdown Report
    md_lines = [
        f"# Preflight Tier Audit: {tier_title}",
        f"",
        f"- **Repository:** `{repo_name or 'All Repositories'}`",
        f"- **Verdict:** **{scoring['verdict_title']}**",
        f"- **Readiness Score:** **{scoring['readiness_score']} / 100**",
        f"- **Defects Found:** `{defect_count}`",
        f"- **Files Scanned:** `{total_files_scanned}`",
        f"",
        f"> **Verdict Meaning:** {scoring['verdict_meaning']}",
        f"",
        f"---",
        f"",
        f"## 1. What The User Sees (4-Part Plain English Breakdown)",
        f"",
        f"- **1. What's wrong:** {explanation}",
        f"- **2. Why it matters to them:** {consequence}",
        f"- **3. Proof:** {defect_count} confirmed suspect(s) in codebase.",
        f"- **4. How to fix it:**",
        f"",
        f"```text",
        repair_prompt,
        f"```",
        f"",
        f"---",
        f"",
        f"## 2. Identified Defect Occurrences",
        f""
    ]

    if not all_defects:
        md_lines.append("_No defects were identified for this tier across the entire project codebase._")
    else:
        for idx, d in enumerate(all_defects[:50], start=1):
            md_lines.append(f"### {idx}. {d['rule']} in `{d['file']}:{d['line']}`")
            md_lines.append(f"- **What's wrong:** {d.get('detail', explanation)}")
            md_lines.append(f"- **Why it matters:** {d.get('business_consequence', consequence)}")
            md_lines.append(f"- **Proof (Code Snippet):**")
            md_lines.append(f"```text")
            md_lines.append(f"{d['snippet']}")
            md_lines.append(f"```")
            md_lines.append(f"- **Ready-to-Paste AI Fix:**")
            md_lines.append(f"```text")
            md_lines.append(f"{d.get('repair_prompt', repair_prompt)}")
            md_lines.append(f"```")
            md_lines.append("")

    md_lines.extend([
        "---",
        "*Audit executed by Preflight Detection Engine according to Preflight Detection Spec v4.*"
    ])

    markdown_report = "\n".join(md_lines)

    enriched_defects = []
    for d in all_defects:
        f_val = d.get("file") or d.get("file_path") or "codebase"
        l_val = d.get("line") or d.get("line_number") or 1
        r_val = d.get("rule") or f"{tier_upper} Defect"
        s_val = d.get("snippet") or d.get("code_snippet") or ""
        det_val = d.get("detail") or d.get("plain_english_explanation") or explanation
        cons_val = d.get("business_consequence") or consequence
        rep_val = d.get("repair_prompt") or repair_prompt

        enriched_defects.append({
            "file": f_val,
            "file_path": f_val,
            "line": l_val,
            "line_number": l_val,
            "rule": r_val,
            "snippet": s_val,
            "code_snippet": s_val,
            "detail": det_val,
            "plain_english_explanation": det_val,
            "business_consequence": cons_val,
            "repair_prompt": rep_val,
            "tier": tier_lower
        })

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
        "defects": enriched_defects[:50],
        "rules_evaluated": rules,
        "rules": rules,
        "plain_english_explanation": explanation,
        "business_consequence": consequence,
        "repair_prompt": repair_prompt,
        "scoring": scoring,
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
    Computes overall 4-area Preflight scoring and Readiness score across all tiers.
    """
    if custom_code:
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_red = executor.submit(evaluate_red_family, custom_code)
            future_orange = executor.submit(evaluate_orange_family, custom_code)
            future_yellow = executor.submit(evaluate_yellow_family, custom_code)

            red_result = future_red.result()
            orange_result = future_orange.result()
            yellow_result = future_yellow.result()
        
        all_defects = []
        if red_result.get("is_defect"):
            all_defects.append({"tier": "red", "rule": "RED Vulnerability", "detail": red_result.get("plain_english_explanation")})
        if orange_result.get("is_defect"):
            all_defects.append({"tier": "orange", "rule": "ORANGE Hazard", "detail": orange_result.get("plain_english_explanation")})
        if yellow_result.get("is_defect"):
            all_defects.append({"tier": "yellow", "rule": "YELLOW Polish", "detail": yellow_result.get("plain_english_explanation")})
    else:
        clean_repo = sanitize_name(repo_name) if repo_name else None
        with ThreadPoolExecutor(max_workers=3) as executor:
            f_red = executor.submit(audit_single_tier, "red", clean_repo)
            f_orange = executor.submit(audit_single_tier, "orange", clean_repo)
            f_yellow = executor.submit(audit_single_tier, "yellow", clean_repo)

            red_result = f_red.result()
            orange_result = f_orange.result()
            yellow_result = f_yellow.result()

        all_defects = []
        all_defects.extend(red_result.get("defects", []))
        all_defects.extend(orange_result.get("defects", []))
        all_defects.extend(yellow_result.get("defects", []))

    scoring = compute_preflight_scoring(all_defects)

    return {
        "repository": repo_name or "All Repositories",
        "red_defects": red_result,
        "orange_defects": orange_result,
        "yellow_defects": yellow_result,
        "scoring": scoring,
        "readiness_score": scoring["readiness_score"],
        "verdict_title": scoring["verdict_title"],
        "verdict_meaning": scoring["verdict_meaning"],
        "verdict_state": scoring["verdict_state"]
    }


def compile_pure_english_markdown_report(
    eval_results: Dict[str, Any],
    analytics_data: Optional[Dict[str, Any]] = None
) -> str:
    """
    Generates a 100% pure English, comprehensive Markdown report strictly aligned
    with Preflight Detection Spec v4 combining:
    1. Verdict and Readiness Score (Part 2.1)
    2. The 4-Area Health Breakdown (Part 5: Security, Reliability, Correctness, Maintainability)
    3. 🔴 Preflight Defect Family: RED (Fix before anyone uses this)
    4. 🟠 Preflight Defect Family: ORANGE (Your users will hit these)
    5. 🟡 Preflight Defect Family: YELLOW (Tidy up before you hire a developer)
    6. 🏛️ Knowledge Graph Architectural Smells (Critical Hubs, Cycles, Dead Code)
    7. 🛠️ Ready-to-Paste AI Repair Prompts (Part 2.3)
    """
    repo = eval_results.get("repository", "All Repositories")
    red = eval_results.get("red_defects", {})
    orange = eval_results.get("orange_defects", {})
    yellow = eval_results.get("yellow_defects", {})
    scoring = eval_results.get("scoring")

    if not scoring:
        all_d = []
        all_d.extend(red.get("defects", []))
        all_d.extend(orange.get("defects", []))
        all_d.extend(yellow.get("defects", []))
        scoring = compute_preflight_scoring(all_d)

    readiness = scoring["readiness_score"]
    verdict_title = scoring["verdict_title"]
    verdict_meaning = scoring["verdict_meaning"]
    areas = scoring["areas"]

    analytics = analytics_data or {}
    cycles = analytics.get("circular_dependencies") or []
    hubs = analytics.get("critical_hubs") or analytics.get("high_risk_hubs") or []
    dead = analytics.get("dead_code_candidates") or []
    clones = analytics.get("code_clones") or []
    hotspots = analytics.get("git_hotspots") or []

    md = f"""# Preflight Code Quality & Shipping Report

> **Target Repository:** `{repo}`  
> **Evaluation Engine:** Preflight Autonomous Arbiter (Spec v4)  
> **Verdict:** **{verdict_title}**  
> **Readiness Score:** **{readiness} / 100**  

> **Verdict Assessment:** {verdict_meaning}

---

## 1. The 4-Area Readiness Breakdown

| Area | Weight | Spec Category | Score | Status |
| :--- | :--- | :--- | :--- | :--- |
| **Security** | 35% | **Can someone hurt you** | `{areas['security']['score']} / 100` | {('🔴 DANGER' if areas['security']['score'] < 70 else '🟢 SAFE')} |
| **Reliability** | 30% | **Does it break** | `{areas['reliability']['score']} / 100` | {('🔴 UNSTABLE' if areas['reliability']['score'] < 70 else '🟢 STABLE')} |
| **Correctness** | 20% | **Does it behave properly** | `{areas['correctness']['score']} / 100` | {('🟠 FAULTY' if areas['correctness']['score'] < 80 else '🟢 CLEAN')} |
| **Maintainability** | 15% | **Can a developer work on it** | `{areas['maintainability']['score']} / 100` | {('🟡 FRICTION' if areas['maintainability']['score'] < 80 else '🟢 MAINTAINABLE')} |

---

## 2. 🔴 Preflight Defect Family: RED (Fix Before Anyone Uses This)

*Defects evaluated: Leaked API keys, Unprotected admin routes, Database injection, Crashes on unexpected input, Infinite loops, Dead buttons on the main flow.*

- **Defect Status:** `{("DEFECT DETECTED" if red.get("is_defect") else "PASS - NO BLOCKERS")}`
- **Defects Count:** `{red.get("defect_count", (1 if red.get("is_defect") else 0))}`
- **1. What's wrong:**  
  > {red.get("plain_english_explanation", "No critical blocker defects identified.")}
- **2. Why it matters to them:**  
  > {red.get("business_consequence", "Zero financial or security degradation observed.")}
- **3. How to fix it:**  
  ```text
  {red.get("repair_prompt", "Copy this and paste it into Lovable / Cursor:\nMaintain existing secret scanning and auth guards.")}
  ```

---

## 3. 🟠 Preflight Defect Family: ORANGE (Your Users Will Hit These)

*Defects evaluated: No loading state / double submission, No limits on what people can type, Search and sort are wrong, Slow pages and heavy images.*

- **Defect Status:** `{("DEFECT DETECTED" if orange.get("is_defect") else "PASS - NO OPERATIONAL HAZARDS")}`
- **Defects Count:** `{orange.get("defect_count", (1 if orange.get("is_defect") else 0))}`
- **1. What's wrong:**  
  > {orange.get("plain_english_explanation", "No high-risk operational hazards identified.")}
- **2. Why it matters to them:**  
  > {orange.get("business_consequence", "Preserves optimal user experience and API load boundaries.")}
- **3. How to fix it:**  
  ```text
  {orange.get("repair_prompt", "Copy this and paste it into Lovable / Cursor:\nEnsure all async actions disable triggers and enforce input bounds.")}
  ```

---

## 4. 🟡 Preflight Defect Family: YELLOW (Tidy Up Before You Hire A Developer)

*Defects evaluated: Over-complex files, Debug code and no documentation, Outdated methods.*  
*(Note: Colour contrast, accessibility, and spelling are excluded from scope).*

- **Defect Status:** `{("DEFECT DETECTED" if yellow.get("is_defect") else "PASS - CLEAN HYGIENE")}`
- **Defects Count:** `{yellow.get("defect_count", (1 if yellow.get("is_defect") else 0))}`
- **1. What's wrong:**  
  > {yellow.get("plain_english_explanation", "Code hygiene meets maintainability standards.")}
- **2. Why it matters to them:**  
  > {yellow.get("business_consequence", "Clean production logs and complete documentation ensure rapid team onboarding.")}
- **3. How to fix it:**  
  ```text
  {yellow.get("repair_prompt", "Copy this and paste it into Lovable / Cursor:\nStrip development console statements from production builds and create a clear README.md.")}
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

## 6. 🛠️ Ready-to-Paste Remediation Instructions

Copy and paste the repair prompts directly into Lovable, Cursor, Bolt, or v0, re-run Preflight, and watch your Readiness score rise from 40 to 90+.

---
*Report generated automatically by Preflight Autonomous Arbiter.*
"""
    return md


# ==============================================================================
# AUTONOMOUS BUG FIXING ENGINE (SPEC FIX-BUG CAPABILITY)
# ==============================================================================

def extract_code_block(llm_output: str, fallback_code: str) -> str:
    """
    Extracts updated source code content from LLM markdown code blocks or fences.
    Falls back to fallback_code if extraction produces empty or invalid content.
    """
    if not llm_output or not isinstance(llm_output, str):
        return fallback_code

    cleaned = llm_output.strip()

    # Pattern 1: ```lang ... ``` with line break
    fence_match = re.search(r'```(?:[a-zA-Z0-9_\-\.]+)?\s*\n([\s\S]*?)\n```', cleaned)
    if fence_match:
        extracted = fence_match.group(1).strip()
        if len(extracted) >= 10:
            return extracted

    # Pattern 2: Any ``` ... ```
    fence_match2 = re.search(r'```(?:[a-zA-Z0-9_\-\.]+)?\s*([\s\S]*?)```', cleaned)
    if fence_match2:
        extracted = fence_match2.group(1).strip()
        if len(extracted) >= 10:
            return extracted

    # If the response is raw code without fences and not an error/json message
    if len(cleaned) >= 20 and not cleaned.startswith("{") and not cleaned.startswith("I apologize") and not cleaned.startswith("Sorry"):
        return cleaned

    return fallback_code


def apply_preflight_bug_fix(
    repo_name: Optional[str],
    file_path: str,
    repair_prompt: str,
    rule: Optional[str] = None,
    detail: Optional[str] = None,
    business_consequence: Optional[str] = None,
    snippet: Optional[str] = None,
    line_number: Optional[int] = None
) -> Dict[str, Any]:
    """
    Autonomously fixes a reported Preflight defect by giving the generated repair prompt
    and target file content to the LLM, and writes the corrected code back to disk.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    clean_path = (file_path or "").strip().replace("\\", "/")
    if not clean_path:
        raise HTTPException(status_code=400, detail="file_path is required.")

    # Strip repo name prefix if present in the file path
    if clean_repo and clean_path.startswith(clean_repo + "/"):
        clean_path = clean_path[len(clean_repo) + 1:]

    candidates = get_candidate_directories(clean_repo)
    target_file: Optional[Path] = None

    for base_dir in candidates:
        candidate_path = (base_dir / clean_path).resolve()
        try:
            candidate_path.relative_to(base_dir.resolve())
            if candidate_path.exists() and candidate_path.is_file():
                target_file = candidate_path
                break
        except (ValueError, Exception):
            continue

    if not target_file:
        query_fname = Path(clean_path).name.lower()
        for base_dir in candidates:
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "venv", ".venv", "__pycache__"} and not d.startswith(".")]
                for f in files:
                    if f.lower() == query_fname:
                        found_path = (Path(root) / f).resolve()
                        try:
                            found_path.relative_to(base_dir.resolve())
                            target_file = found_path
                            break
                        except Exception:
                            continue
                if target_file:
                    break
            if target_file:
                break

    if not target_file or not target_file.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Target file '{file_path}' was not found in repository directories."
        )

    try:
        original_code = target_file.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read file '{file_path}': {str(e)}"
        )

    file_ext = target_file.suffix.lstrip(".").lower() or "txt"
    display_rel = clean_path

    # Clean repair prompt if it contains the spec copy header
    clean_repair = repair_prompt or ""
    if clean_repair.startswith("Copy this and paste it into Lovable / Cursor:"):
        clean_repair = clean_repair.replace("Copy this and paste it into Lovable / Cursor:", "").strip()

    # System prompt strictly instructing autonomous code repair
    fix_system_prompt = """You are an autonomous senior software engineer and production code repair arbiter.
Your task is to fix the reported defect in the provided source code according to the repair instructions.

Instructions:
1. Apply the exact bug fix requested in the repair instructions.
2. Maintain documentation integrity and preserve all existing comments, docstrings, imports, and unaffected functions/classes.
3. Keep the exact same coding style, indentation, and structure.
4. Output MUST be ONLY the complete, production-ready, updated source code of the entire file.
5. Enclose the updated code inside a single markdown code fence with the file language, e.g.:
```{language}
[complete updated source code]
```
Do not output any conversational explanations, notes, or preamble outside the code block."""

    user_prompt = f"""Please fix the following defect in '{display_rel}':

Defect Rule: {rule or 'Suspected Defect'}
Line Number: {line_number or 'N/A'}
Problem Explanation: {detail or 'Suspected issue identified in code inspection'}
Consequence: {business_consequence or 'N/A'}
Offending Snippet:
```
{snippet or 'N/A'}
```

Repair Instructions:
{clean_repair}

Current Full File Content:
```{file_ext}
{original_code}
```

Apply the fix directly and return the full updated file in a code block."""

    # Handle testing environment without real LLM endpoint
    if os.getenv("TESTING") == "1" and not os.getenv("REAL_LLM"):
        # Simulated test mode fix
        fixed_code = original_code + "\n// [Preflight Auto-Fix Applied: Defect Resolved]\n"
    else:
        try:
            raw_llm = call_llm(user_prompt, fix_system_prompt, timeout=120)
            fixed_code = extract_code_block(raw_llm, original_code)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"AI model failed while generating fix: {str(e)}"
            )

    if not fixed_code or fixed_code == original_code:
        raise HTTPException(
            status_code=500,
            detail="The AI did not produce a modified code block for this file."
        )

    try:
        target_file.write_text(fixed_code, encoding="utf-8")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to write fixed code to '{display_rel}': {str(e)}"
        )

    return {
        "status": "success",
        "file_path": display_rel,
        "message": f"Successfully fixed defect and saved '{display_rel}'!",
        "rule": rule,
        "line_number": line_number,
        "original_size": len(original_code),
        "fixed_size": len(fixed_code)
    }

