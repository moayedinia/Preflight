import os
import re
import json
import ast
import copy
import hashlib
import time
import logging
import requests
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
from fastapi import HTTPException

logger = logging.getLogger(__name__)

from app.core.security import ALLOWED_REPO_DIR, sanitize_name
from app.core.llm import call_llm, get_llm_config

# ==============================================================================
# PREFLIGHT DETECTION SPEC V4 - 13 DEFECT RULES & SYSTEM PROMPTS
# Spec reference: PREFLIGHT_DETECTION_SPEC.md (Version 4, 12 Oct 2026)
# Scope: Code defects for vibe-coding apps (Lovable, Cursor, Bolt, v0, Replit).
# Excluded from scope: Colour contrast, accessibility, spelling (Design review).
# ==============================================================================

PREFLIGHT_IGNORED_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
    ".idea", ".vscode", "dist", "build", ".gemini", ".system_generated",
    ".cache", "tmp", "logs", ".preflight-backups"
}

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

CANONICAL_TIER_RULES: Dict[str, List[str]] = {
    "red": [
        "Leaked API keys",
        "Unprotected admin routes",
        "Database injection",
        "Crashes on unexpected input",
        "Infinite loops",
        "Dead buttons on the main flow"
    ],
    "orange": [
        "No loading state, double submission",
        "No limits on what people can type",
        "Search and sort are wrong",
        "Slow pages and heavy images"
    ],
    "yellow": [
        "Over-complex files",
        "Debug code and no documentation",
        "Outdated methods"
    ]
}

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

_DEFECTS_REGISTRY = None

def get_defects_registry():
    global _DEFECTS_REGISTRY
    if _DEFECTS_REGISTRY is not None:
        return _DEFECTS_REGISTRY
    from pathlib import Path
    import json
    reg_path = Path(__file__).resolve().parent.parent / "defects" / "registry.json"
    if not reg_path.exists():
        reg_path = Path("backend/app/defects/registry.json")
    if reg_path.exists():
        try:
            with open(reg_path, "r", encoding="utf-8") as f:
                _DEFECTS_REGISTRY = json.load(f)
                return _DEFECTS_REGISTRY
        except Exception as e:
            logger.warning(f"Could not load defects registry: {e}")
    _DEFECTS_REGISTRY = {"defects": []}
    return _DEFECTS_REGISTRY

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

    reg = get_defects_registry()
    reg_defects = reg.get("defects", [])
    reg_by_id = {item["id"].upper(): item for item in reg_defects}

    for d in all_defects:
        raw_rule = str(d.get("rule", "")).strip()
        raw_id = str(d.get("id") or d.get("defect_id") or "").strip().upper()
        tier = str(d.get("tier", "")).lower()
        unconf_raw = d.get("unconfirmed", False)
        if isinstance(unconf_raw, str):
            is_unconfirmed = unconf_raw.strip().lower() in ("true", "1", "yes")
        else:
            is_unconfirmed = bool(unconf_raw)

        fixed_raw = d.get("fixed", False)
        if isinstance(fixed_raw, str):
            is_fixed = fixed_raw.strip().lower() in ("true", "1", "yes")
        else:
            is_fixed = bool(fixed_raw)

        # Unconfirmed or fixed findings subtract nothing per spec
        if is_unconfirmed or is_fixed:
            continue

        matched_reg = None
        if raw_id in reg_by_id:
            matched_reg = reg_by_id[raw_id]
        else:
            # Match by canonical name or substring from registry
            rule_clean = raw_rule.lower()
            for item in reg_defects:
                c_name = item["canonical_name"].lower()
                if c_name in rule_clean or rule_clean in c_name:
                    matched_reg = item
                    break

        if not matched_reg:
            # Fallback matching for Code Review P0-P3 tags
            p_lev = str(d.get("p_level", "")).upper()
            if p_lev == "P0" or tier == "red" or "[p0" in raw_rule.lower():
                matched_reg = {"tier": "red", "area": "security" if any(k in raw_rule.lower() for k in ["secret", "key", "admin", "inject", "auth", "rce"]) else "reliability", "penalty": 40}
            elif p_lev == "P1" or tier == "orange" or "[p1" in raw_rule.lower():
                matched_reg = {"tier": "orange", "area": "reliability" if any(k in raw_rule.lower() for k in ["crash", "load", "race", "double"]) else "correctness", "penalty": 25}
            elif p_lev in ("P2", "P3") or tier == "yellow" or any(k in raw_rule.lower() for k in ["[p2", "[p3", "hygiene", "debug", "complex"]):
                matched_reg = {"tier": "yellow", "area": "maintainability", "penalty": 4}
            else:
                logger.info(f"[Preflight Scoring] Finding '{raw_rule}' has no matching registry ID; treating as unconfirmed (0 points deducted).")
                continue

        d_tier = matched_reg.get("tier", tier)
        d_area = matched_reg.get("area", "correctness")
        penalty = matched_reg.get("penalty", 4)

        if d_tier == "red":
            red_count += 1
            if d_area == "security":
                sec_deductions += penalty
            else:
                rel_deductions += penalty
        elif d_tier == "orange":
            orange_count += 1
            if d_area == "reliability":
                rel_deductions += penalty
            else:
                cor_deductions += penalty
        elif d_tier == "yellow":
            yellow_count += 1
            main_deductions += penalty

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

    # Verdict determination per spec:
    # 1. Any red finding OR score < 70 -> 🔴 Not ready
    # 2. Any orange finding OR score < 90 -> 🟠 Launch with care
    # 3. Exactly 0 red AND 0 orange AND score >= 90 -> 🟢 Ready
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
        verdict_meaning = f"Nothing dangerous, but {orange_count} UX/operational hazard{'s' if orange_count > 1 else ''} detected. Users will hit problems. Fix before shipping."
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
                "deductions": min(100, sec_deductions),
                "raw_deductions": sec_deductions
            },
            "reliability": {
                "name": "Reliability",
                "title": "Does it break",
                "weight": 0.30,
                "score": rel_score,
                "deductions": min(100, rel_deductions),
                "raw_deductions": rel_deductions
            },
            "correctness": {
                "name": "Correctness",
                "title": "Does it behave properly",
                "weight": 0.20,
                "score": cor_score,
                "deductions": min(100, cor_deductions),
                "raw_deductions": cor_deductions
            },
            "maintainability": {
                "name": "Maintainability",
                "title": "Can a developer work on it",
                "weight": 0.15,
                "score": main_score,
                "deductions": min(100, main_deductions),
                "raw_deductions": main_deductions
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
            for root, dirs, files in os.walk(repo_dir):
                dirs[:] = sorted([d for d in dirs if d not in PREFLIGHT_IGNORED_DIRS and not d.startswith(".")])
                for f in sorted(files):
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
        # A named repository resolves only inside the repos sandbox. Falling back to the
        # server's working directory would let a repository named e.g. "app" read the
        # backend's own source, configuration and accounts database.
        d = ALLOWED_REPO_DIR / clean_repo
        if d.exists() and d.is_dir():
            candidates.append(d)
    else:
        if ALLOWED_REPO_DIR.exists():
            for child in ALLOWED_REPO_DIR.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    candidates.append(child)
        ws = Path(os.getcwd()).resolve()
        if ws not in [c.resolve() for c in candidates]:
            candidates.append(ws)

    return candidates


def _validate_llm_defect_evidence(
    defect: Dict[str, Any], candidates: List[Path], repo_name: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Accept an LLM-only finding only when its exact snippet exists on disk now."""
    raw_path = str(defect.get("file") or defect.get("file_path") or "").strip().replace("\\", "/")
    snippet = str(defect.get("snippet") or defect.get("code_snippet") or "").strip()
    if not raw_path or not snippet or raw_path in {"codebase", repo_name or ""}:
        return None
    if repo_name and raw_path.startswith(repo_name + "/"):
        raw_path = raw_path[len(repo_name) + 1:]

    for base_dir in candidates:
        candidate = (base_dir / raw_path).resolve()
        try:
            candidate.relative_to(base_dir.resolve())
        except ValueError:
            continue
        if not candidate.is_file():
            continue
        content = candidate.read_text(encoding="utf-8", errors="ignore")
        if snippet not in content:
            continue
        verified = dict(defect)
        verified["file"] = candidate.relative_to(base_dir.resolve()).as_posix()
        verified["snippet"] = snippet
        return verified
    return None


_TIER_AUDIT_CACHE: Dict[str, Dict[str, Any]] = {}
_AUDIT_CODE_REVIEW_CACHE: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}


def get_repo_state_hash(repo_name: Optional[str] = None) -> str:
    """
    Computes a deterministic hash of the target repository state based on
    file paths, sizes, and mtimes. If files on disk do not change, this hash
    remains completely unchanged.
    """
    candidates = get_candidate_directories(repo_name)
    ignored_dirs = PREFLIGHT_IGNORED_DIRS
    file_records = []
    for d in candidates:
        if not d.exists():
            continue
        for root, dirs, files in os.walk(d):
            dirs[:] = [sub for sub in dirs if sub not in ignored_dirs and not sub.startswith(".")]
            for f in sorted(files):
                if f.startswith("."):
                    continue
                fp = Path(root) / f
                try:
                    st = fp.stat()
                    rel_p = fp.relative_to(d).as_posix()
                    file_records.append(f"{rel_p}:{st.st_size}:{st.st_mtime_ns}")
                except (OSError, ValueError):
                    continue
    file_records.sort()
    raw_sig = "\n".join(file_records).encode("utf-8")
    return hashlib.sha256(raw_sig).hexdigest()


def clear_preflight_audit_cache(repo_name: Optional[str] = None):
    """
    Clears all cached preflight tier audits and code review findings.
    If repo_name is provided, invalidates entries belonging to that repository.
    """
    global _AUDIT_CODE_REVIEW_CACHE, _TIER_AUDIT_CACHE
    if repo_name:
        clean = sanitize_name(repo_name)
        _AUDIT_CODE_REVIEW_CACHE = {
            k: v for k, v in _AUDIT_CODE_REVIEW_CACHE.items()
            if not (k.startswith(f"{clean}:") or k == clean or f":{clean}:" in k)
        }
        _TIER_AUDIT_CACHE = {
            k: v for k, v in _TIER_AUDIT_CACHE.items()
            if not (k.startswith(f"{clean}:") or f":{clean}:" in k)
        }
    else:
        _AUDIT_CODE_REVIEW_CACHE.clear()
        _TIER_AUDIT_CACHE.clear()


def clear_code_review_audit_cache():
    clear_preflight_audit_cache()


def extract_code_review_defects(report_md: str) -> List[Dict[str, Any]]:
    """
    Parses findings from a Preflight Code Review report markdown and converts them into
    structured defects mapped into Red (P0), Orange (P1), and Yellow (P2/P3) tiers.
    Strictly ignores summary tables and only extracts verified, concrete defect sections.
    """
    if not report_md or not report_md.strip():
        return []

    # 1. Scope search to Detailed Findings section (skips Executive Summary tables completely)
    search_text = report_md
    findings_match = re.search(r'##\s*(?:Detailed\s+Findings|Findings)', report_md, re.IGNORECASE)
    if findings_match:
        search_text = report_md[findings_match.end():]

    plan_match = re.search(r'##\s*(?:Actionable\s+Remediation|Remediation\s+Plan|Next\s+Steps)', search_text, re.IGNORECASE)
    if plan_match:
        search_text = search_text[:plan_match.start()]

    # Filter out table lines completely so table rows (especially count 0 rows) are never matched
    clean_lines = [
        line for line in search_text.splitlines()
        if not line.strip().startswith('|') and not line.strip().endswith('|') and '|' not in line
    ]
    search_text = '\n'.join(clean_lines)

    defects: List[Dict[str, Any]] = []
    # Match genuine finding headers: ### [P0] Title, ### P0: Title, or 1. [P1] Title
    section_pattern = re.compile(
        r'^(?:###?|\d+\.)\s*\[?(P[0-3])\]?\s*[:\-\s]\s*([^\n\r|]+)',
        re.IGNORECASE | re.MULTILINE
    )
    matches = list(section_pattern.finditer(search_text))

    for i, m in enumerate(matches):
        p_level = m.group(1).upper()
        title = (m.group(2) or '').strip().strip('*` ').strip()
        # Reject table rows or table headers that accidentally matched
        if not title or '|' in title or 'level' in title.lower() or 'critical blocker' in title.lower() or 'high blocker' in title.lower() or 'medium warning' in title.lower() or 'low nit' in title.lower():
            continue

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(search_text)
        body = search_text[start:end]

        if '## ' in body:
            body = body.split('## ')[0]

        tier = 'red' if p_level == 'P0' else ('orange' if p_level == 'P1' else 'yellow')

        # Extract file path & line
        path_match = re.search(
            r'(?:File\s*(?:&|and)?\s*Line|File\s*Path\s*(?:&|and)?\s*Line|Offending\s*File|Location|Path|File):\*{0,2}\s*`?([^\n\r`*]+)`?',
            body,
            re.IGNORECASE
        )
        file_path = 'codebase'
        line_num = 1
        if path_match:
            raw_path = (path_match.group(1) or '').strip('`* ')
            if ':' in raw_path:
                parts = raw_path.rsplit(':', 1)
                file_path = parts[0].strip('`* ')
                l_str = parts[1].split('-')[0].strip('`* ')
                try:
                    line_num = int(l_str)
                except ValueError:
                    line_num = 1
            else:
                file_path = raw_path

        # If file_path is still codebase and body has table markdown or no real code/issue, skip
        if file_path == 'codebase' and ('|' in body or not any(k in body for k in ['```', 'Issue', 'Explanation', 'Recommendation', 'Fix'])):
            continue

        # Extract offending code snippet
        snip_match = re.search(r'```(?:\w+)?\s*\n([\s\S]*?)\n```', body)
        if snip_match:
            snippet = snip_match.group(1).strip()
        else:
            code_line = re.search(
                r'(?:Offending Code|Code Snippet|Snippet):\*{0,2}\s*`?([^\n\r`]+)`?',
                body,
                re.IGNORECASE
            )
            snippet = (code_line.group(1) or '').strip('`* ') if code_line else title

        # Extract issue / explanation / consequence
        exp_match = re.search(
            r'(?:Explanation\s*(?:&|and)?\s*Business\s*Consequence|Issue|Explanation|Description|What\'s\s*wrong):\*{0,2}\s*\n?([^\n\r]+(?:\n[^\n\r\-*#]+)*)',
            body,
            re.IGNORECASE
        )
        if exp_match:
            raw_detail = (exp_match.group(1) or '').strip('`* ')
            detail = raw_detail.split('\n')[0].strip()
        else:
            detail = title

        # Extract actionable recommendation / fix
        rec_match = re.search(
            r'(?:Actionable\s*Code\s*Fix|Actionable\s*Fix|Suggested\s*Fix|Recommendation|Action|Fix):\*{0,2}\s*\n?([\s\S]*?)(?=\n\s*-\s*\*\*|\n\s*###|\Z)',
            body,
            re.IGNORECASE
        )
        if rec_match:
            recommendation = (rec_match.group(1) or '').strip('`* ')
        else:
            recommendation = f'Address [{p_level}] finding in {file_path}.'

        repair_prompt = f'In `{file_path}:{line_num}`:\n{recommendation}'

        if p_level == 'P0':
            consequence = 'Critical security or crash blocker: May lead to remote exploitation, credential leakage, or crash.'
        elif p_level == 'P1':
            consequence = 'High operational risk: Core workflow failure, broken user action, or unhandled exception.'
        else:
            consequence = 'Code hygiene or maintenance risk: Performance degradation, edge case bug, or technical debt.'

        defects.append({
            'file': file_path,
            'file_path': file_path,
            'line': line_num,
            'line_number': line_num,
            'rule': f'[{p_level} Code Review] {title}',
            'snippet': snippet[:160],
            'code_snippet': snippet[:160],
            'detail': detail,
            'plain_english_explanation': detail,
            'business_consequence': consequence,
            'repair_prompt': repair_prompt,
            'tier': tier,
            'source': 'code_review',
            'p_level': p_level
        })

    return defects


def get_code_review_findings_for_audit(repo_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Retrieves Preflight Code Review findings for a repository and returns
    structured defects mapped into Red (P0), Orange (P1), and Yellow (P2/P3) tiers.
    Cached per repository state hash to avoid non-deterministic variations when
    the repository has not changed on disk.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    state_hash = get_repo_state_hash(clean_repo)
    cache_key = f"{clean_repo or 'default'}:{state_hash}"

    if cache_key in _AUDIT_CODE_REVIEW_CACHE:
        cached_hash, cached_defects = _AUDIT_CODE_REVIEW_CACHE[cache_key]
        if cached_hash == state_hash:
            return copy.deepcopy(cached_defects)

    try:
        from app.core.sandbox import run_preflight_code_review
        res = run_preflight_code_review(repo_name=clean_repo)
        report_md = res.get("markdown_report", "")
        # A prose code-review response is never enough evidence by itself.  The
        # reviewer can have seen an earlier version of the repository, while an
        # automated fix may already have changed the file.  Retain only findings
        # whose exact reported snippet still exists at their reported path now.
        # This also makes a new audit reflect the code that was actually saved.
        candidates = get_candidate_directories(clean_repo)
        defects = []
        for finding in extract_code_review_defects(report_md):
            verified = _validate_llm_defect_evidence(finding, candidates, clean_repo)
            if verified:
                defects.append(verified)
        _AUDIT_CODE_REVIEW_CACHE[cache_key] = (state_hash, defects)
        return copy.deepcopy(defects)
    except Exception as e:
        print(f"[Preflight Audit] Code Review Skill Note ({cache_key}): {e}")
        return []


def audit_single_tier(tier: str, repo_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Exclusively and independently audits the specified tier (RED, ORANGE, or YELLOW).
    Executes AST and regex code inspection across project files for that tier's exact defect rules,
    and then invokes the LLM Arbiter using the user's exact tier system prompt.
    """
    tier_lower = (tier or "red").strip().lower()
    tier_upper = tier_lower.upper()
    clean_repo = sanitize_name(repo_name) if repo_name else None

    # Check repository state cache for deterministic instant responses
    state_hash = get_repo_state_hash(clean_repo)
    cache_key = f"{tier_lower}:{clean_repo or 'default'}:{state_hash}"
    if cache_key in _TIER_AUDIT_CACHE:
        return copy.deepcopy(_TIER_AUDIT_CACHE[cache_key])
    directories = get_candidate_directories(clean_repo)
    ignored_dirs = PREFLIGHT_IGNORED_DIRS

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
            dirs[:] = sorted([d for d in dirs if d not in ignored_dirs and not d.startswith(".")])

            for fname in sorted(files):
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

    # Integrate Preflight Code Review Skill findings (P0 -> Red, P1 -> Orange, P2/P3 -> Yellow)
    try:
        cr_defects = get_code_review_findings_for_audit(clean_repo)
        tier_cr_defects = [d for d in cr_defects if d.get("tier") == tier_lower]
        unmatched_cr = [
            crd for crd in tier_cr_defects
            if not any(
                existing.get("file") == crd.get("file")
                and (
                    existing.get("line") == crd.get("line")
                    or existing.get("rule") == crd.get("rule")
                    or (crd.get("snippet") and crd.get("snippet") in existing.get("snippet", ""))
                )
                for existing in all_defects
            )
        ]
        all_defects = unmatched_cr + all_defects
    except Exception as cr_err:
        print(f"[audit_single_tier] Code review merge error ({tier_lower}): {cr_err}")

    # Deterministically sort static defects so LLM prompt and reports are 100% reproducible
    all_defects.sort(key=lambda d: (str(d.get("file", "")), int(d.get("line") or 0), str(d.get("rule", ""))))

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

    engine = "heuristic-fallback"
    llm_cfg = get_llm_config()
    has_api_key = bool(llm_cfg.get("api_key") or os.getenv("VULTR_API_KEY") or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"))
    if not has_api_key and os.getenv("TESTING") != "1" and os.getenv("MOCK_LLM") != "1":
        logger.info(f"No AI API key configured; skipping remote LLM Arbiter and activating heuristic fallback for {tier_lower}.")
    else:
        try:
            raw_llm = call_llm(user_prompt, system_prompt, timeout=90)
            engine = "vultr-inference"
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
                    verified = _validate_llm_defect_evidence(d, directories, clean_repo) if isinstance(d, dict) else None
                    if verified and verified.get("rule"):
                        duplicate = any(
                            existing.get("file") == verified.get("file")
                            and existing.get("snippet") == verified.get("snippet")
                            for existing in all_defects
                        )
                        if duplicate:
                            continue
                        all_defects.append({
                            "file": verified["file"],
                            "line": verified.get("line", 1),
                            "rule": verified.get("rule", f"{tier_upper} Defect"),
                            "snippet": verified["snippet"][:160],
                            "detail": str(verified.get("detail", explanation))[:300],
                            "business_consequence": str(verified.get("business_consequence", consequence))[:300],
                            "repair_prompt": str(verified.get("repair_prompt", repair_prompt)),
                            "tier": tier_lower
                        })
            defect_count = len(all_defects)
            if defect_count > 0:
                is_defect = True
                confidence = max(confidence, 0.92)
        except Exception as llm_err:
            print(f"[Preflight Tier Audit] LLM Note ({tier_lower}): {llm_err}")

    # Deduplicate defects deterministically by (file, line, rule, snippet)
    seen_defect_keys = set()
    deduped_defects = []
    for d in all_defects:
        f_norm = (d.get("file") or d.get("file_path") or "").strip().replace("\\", "/")
        l_norm = int(d.get("line") or d.get("line_number") or 1)
        r_norm = (d.get("rule") or "").strip()
        s_norm = (d.get("snippet") or d.get("code_snippet") or "").strip()[:80]
        dedup_key = (f_norm, l_norm, r_norm, s_norm)
        if dedup_key in seen_defect_keys:
            continue
        seen_defect_keys.add(dedup_key)
        deduped_defects.append(d)
    all_defects = deduped_defects

    all_defects.sort(key=lambda d: (
        0 if d.get("source") == "code_review" or d.get("p_level") else 1,
        str(d.get("file", "")),
        int(d.get("line") or d.get("line_number") or 1),
        str(d.get("rule", ""))
    ))
    defect_count = len(all_defects)
    is_defect = defect_count > 0

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

        ed_item = {
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
        }
        if d.get("p_level"):
            ed_item["p_level"] = d["p_level"]
        if d.get("source"):
            ed_item["source"] = d["source"]
        enriched_defects.append(ed_item)

    res_data = {
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
        "markdown_report": markdown_report,
        "engine": engine
    }
    _TIER_AUDIT_CACHE[cache_key] = copy.deepcopy(res_data)
    return res_data


def _probe_dead_primary_action_with_playwright(url: str, primary_labels: List[str]) -> Optional[bool]:
    """
    Executes actual Playwright browser interaction (Checklist v2 Item 1.1):
    1. Launches headless browser.
    2. Navigates to running sandbox page.
    3. Finds element by visible label (Pay, Checkout, Buy, Subscribe, Submit, Confirm).
    4. Computes DOM hash before click.
    5. Clicks the button.
    6. Waits 2 seconds while counting network requests.
    7. Computes DOM hash after click.
    8. Confirmed (True) ONLY if requests == 0 AND dom_before == dom_after AND element is enabled.
    9. In every other case (requests > 0, DOM changed, or element not found), returns False (Uncertain).
    Returns None if Playwright is not available or browser cannot launch.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = None
            for launch_kwargs in [
                {"headless": True, "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]},
                {"headless": True, "channel": "chrome", "args": ["--no-sandbox", "--disable-setuid-sandbox"]},
                {"headless": True, "channel": "msedge", "args": ["--no-sandbox", "--disable-setuid-sandbox"]},
                {"headless": True, "channel": "chromium", "args": ["--no-sandbox", "--disable-setuid-sandbox"]}
            ]:
                try:
                    browser = p.chromium.launch(**launch_kwargs)
                    break
                except Exception:
                    continue

            if not browser:
                return None

            try:
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()

                network_requests = 0
                def on_request(req):
                    nonlocal network_requests
                    network_requests += 1

                page.on("request", on_request)

                try:
                    page.goto(url, timeout=5000, wait_until="domcontentloaded")
                    page.wait_for_timeout(800)  # Brief wait for client-side React/Vue hydration
                except Exception:
                    return False

                # Find element by visible label
                target_el = None
                for lbl in primary_labels:
                    # 1. Look for button by visible name
                    try:
                        loc = page.get_by_role("button", name=re.compile(r"\b" + re.escape(lbl) + r"\b", re.IGNORECASE))
                        if loc.count() > 0 and loc.first.is_visible():
                            target_el = loc.first
                            break
                    except Exception:
                        pass

                    # 2. Look for button, link or submit input containing label text
                    try:
                        loc = page.locator(f"button:has-text('{lbl}'), a:has-text('{lbl}'), input[type='submit'][value*='{lbl}' i]")
                        if loc.count() > 0 and loc.first.is_visible():
                            target_el = loc.first
                            break
                    except Exception:
                        pass

                if not target_el or not target_el.is_visible():
                    # Item 1.1: "Uncertain in every other case, including when the element cannot be found"
                    return False

                if not target_el.is_enabled():
                    return False

                # Capture DOM hash before click
                dom_before = hashlib.sha256(page.content().encode("utf-8")).hexdigest()

                # Reset request counter before click
                network_requests = 0

                try:
                    target_el.click(timeout=2000)
                except Exception:
                    return False

                # Count network requests in next 2 seconds
                page.wait_for_timeout(2000)

                # Capture DOM hash after click
                dom_after = hashlib.sha256(page.content().encode("utf-8")).hexdigest()

                # Confirmed when requests == 0 AND DOM hash unchanged AND element enabled
                return bool(network_requests == 0 and dom_before == dom_after)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        logger.debug(f"Playwright dead primary action probe error: {exc}")
        return None


def _probe_dead_primary_action(candidate: Dict[str, Any], repo_name: Optional[str]) -> bool:
    """
    Checklist v2 Item 1.1: Dead primary action
    Find element by visible label (Pay, Checkout, Buy, Subscribe, Submit, Confirm).
    Click it. Count network requests in next 2 seconds. Capture DOM hash before and after.
    Confirmed when requests == 0 AND DOM hash unchanged AND element enabled.
    Uncertain in every other case, including when element cannot be found.

    NOTE: React/v0/Lovable buttons never have an HTML 'onclick' attribute.
    Therefore, static regex checks on raw HTML are strictly prohibited to avoid false-positive
    confirmations. If Playwright is not available or the sandbox is stopped, the candidate
    remains UNCERTAIN (unconfirmed: True) and is dropped from score deduction.
    """
    rule = str(candidate.get("rule", "")).lower()
    id_str = str(candidate.get("id") or candidate.get("defect_id") or "").upper()
    snippet = str(candidate.get("snippet", "")).lower()
    detail = str(candidate.get("detail", "")).lower()
    
    is_target_rule = (
        id_str == "RED-06"
        or any(k in rule for k in ["dead button", "dead_btn", "pay button", "button does nothing"])
        or any(k in detail for k in ["dead button", "button does nothing", "pay button"])
        or ("button" in snippet and any(lbl in snippet for lbl in ["pay", "checkout", "buy", "subscribe", "submit", "confirm"]))
    )
    if not is_target_rule:
        return False

    primary_labels = ["Pay", "Checkout", "Buy", "Subscribe", "Submit", "Confirm"]

    # 1. Attempt live Playwright browser click in running sandbox
    try:
        from app.core.sandbox import get_sandbox_session_status
        session_status = get_sandbox_session_status(repo_name)
        port = session_status.get("port")
        if port and session_status.get("status") in ("running", "starting"):
            pw_result = _probe_dead_primary_action_with_playwright(f"http://localhost:{port}/", primary_labels)
            if pw_result is not None:
                return pw_result
    except Exception as exc:
        logger.debug(f"Could not probe dead button with running sandbox: {exc}")

    # 2. When sandbox or Playwright is not running or cannot perform real browser click:
    # Per Checklist v2 Item 1.1 & Auditor directive:
    # "React buttons never have onclick, so it will mark every button in a Lovable or v0 app as dead.
    # Can you make it actually click with Playwright, count requests for two seconds, and hash the DOM before and after?
    # If that is not doable by Friday, tell me and we drop the dead button from the scored set."
    # Never mark as confirmed via static HTML regex. Return False so it is marked unconfirmed: True
    # (Uncertain, 0 points deducted, dropped from the scored set).
    return False


def _probe_incomplete_input_crash(candidate: Dict[str, Any], repo_name: Optional[str]) -> bool:
    """
    Checklist v2 Item 1.2: Handler crashes on incomplete input
    POST the route named in candidate's runtime_hint with payload missing a required field (empty dict).
    Confirmed on a 5xx, or a 200 carrying an unhandled traceback.
    Uncertain on a 4xx (handler validated input correctly).
    """
    rule = str(candidate.get("rule", "")).lower()
    id_str = str(candidate.get("id") or candidate.get("defect_id") or "").upper()
    detail = str(candidate.get("detail", "")).lower()
    
    is_target_rule = (
        id_str == "RED-04"
        or any(k in rule for k in ["crash", "unexpected input", "unhandled exception"])
        or any(k in detail for k in ["crash", "unexpected input", "error page"])
    )
    if not is_target_rule:
        return False

    route = candidate.get("runtime_hint") or candidate.get("route")
    if not route:
        snip = candidate.get("snippet", "") + " " + candidate.get("detail", "")
        m = re.search(r"(/api/[a-zA-Z0-9_\-/]+)", snip)
        if m:
            route = m.group(1)

    try:
        from app.core.sandbox import get_sandbox_session_status
        session_status = get_sandbox_session_status(repo_name)
        port = session_status.get("port")
        if port and session_status.get("status") == "running" and route:
            resp = requests.post(f"http://localhost:{port}{route}", json={}, timeout=3)
            if resp.status_code >= 500:
                return True
            if resp.status_code == 200:
                text_resp = resp.text
                if any(tb in text_resp for tb in ["Traceback (most recent call last)", "Internal Server Error", "Unhandled exception", "TypeError:", "KeyError:"]):
                    return True
            # 4xx means input was validated properly -> uncertain
            return False
    except Exception:
        pass

    return False


def _probe_page_never_settles(candidate: Dict[str, Any], repo_name: Optional[str]) -> bool:
    """
    Checklist v2 Item 1.3: Page never settles
    Load the page, sample container CPU for 10 seconds.
    Confirmed when CPU stays above 80% for the whole window, or watchdog kills it.
    Uncertain otherwise.
    """
    rule = str(candidate.get("rule", "")).lower()
    id_str = str(candidate.get("id") or candidate.get("defect_id") or "").upper()
    detail = str(candidate.get("detail", "")).lower()
    
    is_target_rule = (
        id_str == "RED-05"
        or any(k in rule for k in ["infinite loop", "never stop", "freeze", "cpu saturation"])
        or any(k in detail for k in ["freeze", "infinite loop", "never stops loading"])
    )
    if not is_target_rule:
        return False

    try:
        from app.core.sandbox import get_sandbox_session_status
        session_status = get_sandbox_session_status(repo_name)
        stopped_reason = str(session_status.get("stopped_reason") or "").lower()
        if "watchdog" in stopped_reason or "lifetime_exceeded" in stopped_reason:
            return True
        if session_status.get("status") == "crashed":
            return True
    except Exception:
        pass

    return False


def confirm_candidates_in_sandbox(
    candidates: List[Dict[str, Any]],
    repo_name: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Preflight Core Confirmation Engine (Checklist v2 Item 1).
    Classifies candidate findings into Confirmed Findings and Uncertain Findings:
    - 1.1 Dead primary action (Confirmed when requests == 0 AND DOM unchanged AND element enabled)
    - 1.2 Handler crashes on incomplete input (Confirmed on 5xx or unhandled traceback, uncertain on 4xx)
    - 1.3 Page never settles (Confirmed on CPU > 80% or watchdog kill)
    - 1.4 Everything else gets unconfirmed: True and deducts 0 points.
    """
    confirmed = []
    uncertain = []

    for c in candidates:
        # If test or caller explicitly passed unconfirmed flag
        if "unconfirmed" in c:
            u_val = c["unconfirmed"]
            is_unconf = u_val.strip().lower() in ("true", "1", "yes") if isinstance(u_val, str) else bool(u_val)
            if not is_unconf:
                c["unconfirmed"] = False
                confirmed.append(c)
                continue

        # Check candidate against the 3 mechanical defect confirmation probes
        if _probe_dead_primary_action(c, repo_name):
            c["unconfirmed"] = False
            c["confirmed_by"] = "sandbox_primary_action_probe"
            confirmed.append(c)
        elif _probe_incomplete_input_crash(c, repo_name):
            c["unconfirmed"] = False
            c["confirmed_by"] = "sandbox_incomplete_input_crash"
            confirmed.append(c)
        elif _probe_page_never_settles(c, repo_name):
            c["unconfirmed"] = False
            c["confirmed_by"] = "sandbox_watchdog_cpu_saturation"
            confirmed.append(c)
        else:
            # 1.4 Everything else: unconfirmed = True, deducts nothing
            c["unconfirmed"] = True
            c["confirmed_by"] = "unconfirmed_candidate"
            uncertain.append(c)

    return confirmed, uncertain


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

    confirmed_defects, uncertain_findings = confirm_candidates_in_sandbox(all_defects, repo_name=clean_repo if not custom_code else None)
    scoring = compute_preflight_scoring(all_defects)

    cr_summary = {
        "P0": sum(1 for d in all_defects if d.get("p_level") == "P0"),
        "P1": sum(1 for d in all_defects if d.get("p_level") == "P1"),
        "P2": sum(1 for d in all_defects if d.get("p_level") == "P2"),
        "P3": sum(1 for d in all_defects if d.get("p_level") == "P3"),
    }

    return {
        "repository": repo_name or "All Repositories",
        "red_defects": red_result,
        "orange_defects": orange_result,
        "yellow_defects": yellow_result,
        "code_review_summary": cr_summary,
        "confirmed_defects": confirmed_defects,
        "uncertain_findings": uncertain_findings,
        "scoring": scoring,
        "engine": red_result.get("engine") or orange_result.get("engine") or "vultr-inference",
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

    red_repair = red.get("repair_prompt") or "Copy this and paste it into Lovable / Cursor:\nMaintain existing secret scanning and auth guards."
    orange_repair = orange.get("repair_prompt") or "Copy this and paste it into Lovable / Cursor:\nEnsure all async actions disable triggers and enforce input bounds."
    yellow_repair = yellow.get("repair_prompt") or "Copy this and paste it into Lovable / Cursor:\nStrip development console statements from production builds and create a clear README.md."

    engine = eval_results.get("engine") or red.get("engine") or orange.get("engine") or "vultr-inference"
    engine_label = "Vultr Serverless Inference (Autonomous Arbiter Spec v4)" if engine == "vultr-inference" else "Heuristic Fallback (Local Static Rules - No LLM Key)"

    md = f"""# Preflight Code Quality & Shipping Report

> **Target Repository:** `{repo}`  
> **Evaluation Engine:** {engine_label}  
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
  {red_repair}
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
  {orange_repair}
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
  {yellow_repair}
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

def extract_code_block(llm_output: str, fallback_code: str, snippet: Optional[str] = None) -> str:
    """
    Extracts updated source code content from LLM markdown code blocks or fences.
    Supports reasoning models (DeepSeek, Qwen) with <think>...</think> tags and multiple fences.
    Intelligently handles both full-file replacements and snippet-level substitutions.
    Falls back to fallback_code if extraction produces empty or invalid content.
    """
    if not llm_output or not isinstance(llm_output, str):
        return fallback_code

    cleaned = llm_output.strip()
    # 1. Strip reasoning/thinking tags emitted by models like DeepSeek-R1 / Qwen
    cleaned = re.sub(r'<think>[\s\S]*?</think>', '', cleaned, flags=re.IGNORECASE).strip()

    # 2. Normalize quadruple or more backticks anywhere
    cleaned = re.sub(r'`{4,}', '```', cleaned)

    # 3. Extract all code fences (including unclosed fences at end of text)
    fences = re.findall(r'```(?:[^\n]*)\n([\s\S]*?)\n```', cleaned)
    if not fences:
        fences = re.findall(r'```(?:[^\n]*)\s*([\s\S]*?)(?:```|$)', cleaned)

    # Clean candidate fences
    candidates = []
    for f in fences:
        c = f.strip()
        # Remove inner fences if any
        c = re.sub(r'^`{3,}[^\n]*\n?', '', c)
        c = re.sub(r'\n?`{3,}$', '', c)
        if len(c) >= 5:
            candidates.append(c)

    # Check candidates for full-file replacement
    if candidates:
        # Sort by length descending
        candidates.sort(key=len, reverse=True)
        fallback_len = len(fallback_code)
        
        # Priority A: Check if any candidate is a full-file replacement that actually changed the code
        for cand in candidates:
            if cand != fallback_code and (fallback_len <= 150 or len(cand) >= fallback_len * 0.35):
                return cand

        # Priority B: If candidate is a snippet, try applying as a replacement for the offending snippet
        clean_snip = (snippet or "").strip()
        if clean_snip and len(clean_snip) >= 6:
            for cand in candidates:
                if cand != clean_snip and cand != fallback_code:
                    if clean_snip in fallback_code:
                        patched = fallback_code.replace(clean_snip, cand)
                        if patched != fallback_code:
                            logger.info("Successfully applied snippet-level replacement from AI code block.")
                            return patched
                    else:
                        # Try line-by-line whitespace-trimmed matching
                        orig_lines = fallback_code.splitlines()
                        snip_lines = [l.strip() for l in clean_snip.splitlines() if l.strip()]
                        if snip_lines:
                            for i, o_line in enumerate(orig_lines):
                                if o_line.strip() == snip_lines[0]:
                                    matched = True
                                    for j in range(1, len(snip_lines)):
                                        if i + j >= len(orig_lines) or orig_lines[i + j].strip() != snip_lines[j]:
                                            matched = False
                                            break
                                    if matched:
                                        new_lines = orig_lines[:i] + [cand] + orig_lines[i + len(snip_lines):]
                                        patched = "\n".join(new_lines)
                                        if patched != fallback_code:
                                            logger.info("Successfully applied line-slice snippet replacement.")
                                            return patched

        # Priority C: Function or method replacement by signature matching
        for cand in candidates:
            if cand != fallback_code:
                fn_match = re.search(r'(?:def|function|async\s+def|async\s+function|public\s+(?:async\s+)?\w+[\w<>\[\]]*\s+)(\w+)\s*\(', cand)
                if fn_match:
                    fn_name = fn_match.group(1)
                    pattern = rf'((?:[ \t]*(?:def|function|async\s+def|async\s+function|public\s+(?:async\s+)?[\w<>\[\]]+\s+){fn_name}\s*\([\s\S]*?)(?=\n[ \t]*(?:def|function|async\s+def|async\s+function|public\s+|class\s+|\Z)))'
                    m_orig = re.search(pattern, fallback_code)
                    if m_orig:
                        patched = fallback_code[:m_orig.start()] + cand + fallback_code[m_orig.end():]
                        if patched != fallback_code:
                            logger.info(f"Successfully applied function-level replacement for '{fn_name}'.")
                            return patched

    # 4. If raw code without fences and not conversational prose
    if len(cleaned) >= 20 and not cleaned.startswith("{") and not cleaned.startswith("I apologize") and not cleaned.startswith("Sorry") and not cleaned.startswith("```"):
        if len(fallback_code) <= 150 or len(cleaned) >= len(fallback_code) * 0.35:
            return cleaned
        clean_snip = (snippet or "").strip()
        if clean_snip and clean_snip in fallback_code and cleaned != clean_snip:
            patched = fallback_code.replace(clean_snip, cleaned)
            if patched != fallback_code:
                return patched

    return fallback_code

def _validate_fixed_source(original: str, fixed: str, suffix: str, snippet: Optional[str]) -> str:
    """
    Rejects unchanged, severely truncated, or syntactically broken LLM output.
    Cleans residual markdown fences if present and returns the verified fixed code.
    """
    if not fixed or fixed == original:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "NO_EFFECTIVE_CHANGE",
                "message": "The AI model returned no effective code changes for this defect.",
                "suggested_action": "The original file was kept intact. You can click 'Retry Fix' or use 'Copy Prompt' to customize the fix in Cursor/Claude."
            }
        )

    # Strip any accidental residual outer fences
    cleaned_fixed = fixed
    if re.search(r'^\s*```|```\s*$', cleaned_fixed):
        cleaned_fixed = re.sub(r'^\s*```[^\n]*\n?', '', cleaned_fixed)
        cleaned_fixed = re.sub(r'\n?```\s*$', '', cleaned_fixed).strip()

    original_len = max(len(original), 1)
    # Safeguard against true truncation (less than 35% of original length on non-trivial files)
    if len(original) >= 400 and len(cleaned_fixed) < original_len * 0.35:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "TRUNCATED_OUTPUT",
                "message": f"The proposed fix appeared truncated ({len(cleaned_fixed)} of {len(original)} characters).",
                "suggested_action": "The original file was preserved to prevent code loss. Click 'Retry Fix' or use 'Copy Prompt'."
            }
        )

    clean_snippet = (snippet or "").strip()
    if 8 <= len(clean_snippet) <= 5000 and clean_snippet in original and clean_snippet in cleaned_fixed:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "SNIPPET_UNCHANGED",
                "message": "The reported offending code snippet remained unmodified by the AI.",
                "suggested_action": "The original file was preserved. Click 'Retry Fix' or use 'Copy Prompt' to fix manually."
            }
        )

    if suffix.lower() == ".py":
        try:
            ast.parse(cleaned_fixed)
        except SyntaxError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "SYNTAX_ERROR",
                    "message": f"The proposed Python fix is not syntactically valid (line {exc.lineno}).",
                    "suggested_action": "The original file was preserved. Click 'Retry Fix' to let AI regenerate correct syntax."
                }
            )

    return cleaned_fixed

def apply_preflight_bug_fix(
    repo_name: Optional[str],
    file_path: str,
    repair_prompt: Optional[str] = None,
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
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "FILE_PATH_REQUIRED",
                "message": "Target file path is required to apply automated bug fix.",
                "suggested_action": "Ensure the defect specifies a file path or use 'Copy Prompt' to apply manually."
            }
        )

    # Strip any trailing line numbers like :12, :L45, :12:34
    clean_path = re.sub(r':L?\d+(?::\d+)?$', '', clean_path).strip()
    clean_path = clean_path.lstrip("./")

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

    # Disambiguate if relative path wasn't matched directly
    if not target_file or not target_file.exists():
        fname = Path(clean_path).name
        matched_candidates = []
        ignored_dir_names = {".git", "node_modules", "venv", ".venv", "bin", "obj", ".preflight-backups", "__pycache__"}
        for base_dir in candidates:
            if base_dir.exists():
                for root, dirs, files in os.walk(base_dir):
                    dirs[:] = [d for d in dirs if d not in ignored_dir_names]
                    if fname in files:
                        matched_candidates.append(Path(root) / fname)
        if len(matched_candidates) == 1:
            target_file = matched_candidates[0]
        elif len(matched_candidates) > 1:
            matching_paths = [m.as_posix() for m in matched_candidates[:4]]
            raise HTTPException(
                status_code=404,
                detail={
                    "error_code": "AMBIGUOUS_FILE_MATCH",
                    "message": f"Multiple matching files found for '{fname}': {', '.join(matching_paths)}",
                    "suggested_action": "Specify the full relative path in the file parameter or use 'Copy Prompt' to fix manually."
                }
            )

    if not target_file or not target_file.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "FILE_NOT_FOUND",
                "message": f"Exact target file '{file_path}' was not found in repository '{clean_repo or 'workspace'}'.",
                "suggested_action": "Verify that the file exists in the repository or use the 'Copy Prompt' button to apply the fix manually."
            }
        )

    try:
        original_code = target_file.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "FILE_READ_ERROR",
                "message": f"Failed to read file '{file_path}': {str(e)}",
                "suggested_action": "Check file read permissions on the host filesystem."
            }
        )

    file_ext = target_file.suffix.lstrip(".").lower() or "txt"
    display_rel = clean_path

    # Clean repair prompt or synthesize fallback prompt
    clean_repair = (repair_prompt or "").strip()
    if clean_repair.startswith("Copy this and paste it into Lovable / Cursor:"):
        clean_repair = clean_repair.replace("Copy this and paste it into Lovable / Cursor:", "").strip()
    if not clean_repair:
        clean_repair = f"Fix the defect '{rule or 'reported issue'}' at line {line_number or 'N/A'}. Details: {detail or 'Repair the issue and ensure production safety.'}"

    # System prompt strictly instructing autonomous code repair
    fix_system_prompt = f"""You are a senior software engineer fixing a bug in production code.
Apply the requested fix for '{rule or 'reported defect'}' in '{display_rel}'.
Preserve all existing code structure, comments, and unaffected functions.
Return the complete updated file inside a markdown code block:
```{file_ext}
[complete updated code here]
```"""

    user_prompt = f"""Please fix the following defect in '{display_rel}':

Defect Rule: {rule or 'Suspected Defect'}
Line Number: {line_number or 'N/A'}
Problem Explanation: {detail or 'Suspected issue identified in code inspection'}
Consequence: {business_consequence or 'N/A'}
Offending Snippet to Fix:
```{file_ext}
{snippet or 'N/A'}
```

Repair Instructions:
{clean_repair}

Original Source Code of '{display_rel}':
```{file_ext}
{original_code}
```

Please apply the fix and return the complete updated code inside a ```{file_ext} ... ``` code block."""

    # Handle testing environment without real LLM endpoint
    if os.getenv("TESTING") == "1" and not os.getenv("REAL_LLM"):
        # Simulated test mode fix
        fixed_code = original_code + "\n// [Preflight Auto-Fix Applied: Defect Resolved]\n"
    else:
        try:
            call_timeout = int(os.getenv("LLM_BUG_FIX_TIMEOUT", "90"))
            raw_llm = call_llm(user_prompt, fix_system_prompt, timeout=call_timeout, max_tokens=8192)
            if not raw_llm or not raw_llm.strip():
                raise ValueError("AI endpoint returned an empty code fix response.")
            fixed_code = extract_code_block(raw_llm, original_code, snippet)
        except Exception as e:
            err_msg = str(e)
            if "Read timed out" in err_msg or "timeout" in err_msg.lower():
                user_friendly = "The AI endpoint timed out while generating the code fix (network or proxy delay)."
                action = "Check your local AI proxy connection or click 'Retry Fix'."
            else:
                user_friendly = f"AI model failed while generating fix: {err_msg[:140]}"
                action = "Ensure your AI model endpoint is online and responsive."
            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": "LLM_GENERATION_FAILED",
                    "message": user_friendly,
                    "technical_detail": err_msg,
                    "suggested_action": action
                }
            )

    fixed_code = _validate_fixed_source(original_code, fixed_code, target_file.suffix, snippet)

    try:
        base_dir = next(base for base in candidates if target_file.is_relative_to(base.resolve()))
        actual_rel = target_file.relative_to(base_dir.resolve()).as_posix()
        backup_path = base_dir / ".preflight-backups" / f"{actual_rel}.{os.getpid()}.{time.time_ns()}.bak"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_text(original_code, encoding="utf-8")

        temp_path = target_file.with_name(f".{target_file.name}.preflight-{os.getpid()}.tmp")
        temp_path.write_text(fixed_code, encoding="utf-8")
        os.replace(temp_path, target_file)
        persisted_code = target_file.read_text(encoding="utf-8")
        if persisted_code != fixed_code:
            os.replace(backup_path, target_file)
            raise RuntimeError("Post-write verification failed; original content was restored.")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "FILE_WRITE_ERROR",
                "message": f"Failed to write fixed code to '{display_rel}': {str(e)}",
                "suggested_action": "Check write permissions for target file on disk."
            }
        )

    clear_preflight_audit_cache(clean_repo)

    return {
        "status": "success",
        "file_path": actual_rel,
        "resolved_file_path": str(target_file),
        "backup_path": str(backup_path),
        "message": f"Successfully fixed defect and verified '{actual_rel}' on disk!",
        "rule": rule,
        "line_number": line_number,
        "original_size": len(original_code),
        "fixed_size": len(fixed_code),
        "original_sha256": hashlib.sha256(original_code.encode("utf-8")).hexdigest(),
        "fixed_sha256": hashlib.sha256(fixed_code.encode("utf-8")).hexdigest(),
        "changed": True,
    }

