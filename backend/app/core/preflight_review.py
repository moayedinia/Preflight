"""Read-only Preflight code review service.

This module intentionally never starts processes, installs dependencies, or
contacts Docker.  It reviews only the repository files already imported by the
user and produces a P0–P3 merge-gate report.
"""
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.core.llm import call_llm, verify_llm_connection
from app.core.security import ALLOWED_REPO_DIR, sanitize_name

logger = logging.getLogger("codegraph.preflight_review")
_LAST_REPORTS: Dict[str, str] = {}
_IGNORED_DIRS = {".git", "node_modules", "bin", "obj", "dist", "build", "coverage", "__pycache__", ".venv", "venv", "target", "vendor"}
_SOURCE_EXTENSIONS = {".cs", ".razor", ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".php", ".rb", ".cpp", ".c", ".html", ".vue", ".svelte", ".json", ".yaml", ".yml", ".toml", ".csproj"}

PREFLIGHT_REVIEW_SYSTEM_PROMPT = """You are Preflight's senior code reviewer.
Review only the supplied repository source. Return a concise Markdown report in English.
Classify each real finding as P0, P1, P2, or P3. P0/P1 block a merge; P2/P3 do not.
Every finding must cite a file and line when available, explain impact, and give a concrete fix.
Do not claim that code was executed, built, installed, or tested: this is a read-only code review."""


def _repo_dir(repo_name: Optional[str]) -> Tuple[str, Path]:
    clean_name = sanitize_name(repo_name) if repo_name else None
    if clean_name:
        path = ALLOWED_REPO_DIR / clean_name
    else:
        candidates = [item for item in ALLOWED_REPO_DIR.iterdir() if item.is_dir() and not item.name.startswith(".")] if ALLOWED_REPO_DIR.exists() else []
        path = candidates[0] if candidates else ALLOWED_REPO_DIR
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail=f"Repository directory '{path}' not found.")
    return clean_name or path.name, path


def collect_repo_code_context(repo_dir: Path, max_files: int = 24, max_bytes_per_file: int = 4500, max_total_bytes: int = 48000) -> str:
    """Collect bounded source context without following outside the repository."""
    snippets: List[str] = []
    total = 0
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [name for name in dirs if name not in _IGNORED_DIRS and not name.startswith(".")]
        for name in sorted(files):
            file_path = Path(root) / name
            if file_path.suffix.lower() not in _SOURCE_EXTENSIONS or name.endswith((".min.js", ".map", ".d.ts")):
                continue
            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")[:max_bytes_per_file]
            except OSError:
                continue
            if not source.strip():
                continue
            relative = file_path.relative_to(repo_dir).as_posix()
            entry = f"### File: {relative}\n```{file_path.suffix.lstrip('.')}\n{source}\n```"
            if snippets and total + len(entry.encode("utf-8")) > max_total_bytes:
                return "\n\n".join(snippets)
            snippets.append(entry)
            total += len(entry.encode("utf-8"))
            if len(snippets) >= max_files:
                return "\n\n".join(snippets)
    return "\n\n".join(snippets)


def _static_findings(repo_dir: Path) -> List[Tuple[str, str, str, str, str]]:
    rules = [
        ("P0", re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|(?:ghp|gho|ghu)_[A-Za-z0-9]{36})"), "Hardcoded credential", "Move the secret to a managed environment variable and rotate it."),
        ("P0", re.compile(r"(?:subprocess\.(?:run|call|Popen)\([^\n]*shell\s*=\s*True|\bexec\s*\([^\n]*\+)"), "Command injection risk", "Avoid shell execution and pass validated arguments as an array."),
        ("P1", re.compile(r"(?:except\s*:\s*pass|except\s+Exception\s*:\s*pass|catch\s*\([^)]*\)\s*\{\s*\})"), "Silent exception swallowing", "Log the exception and return or raise a meaningful error."),
        ("P1", re.compile(r"dangerouslySetInnerHTML|\.innerHTML\s*="), "Potential unsafe HTML injection", "Sanitize untrusted HTML or use safe DOM rendering APIs."),
        ("P2", re.compile(r"\b(?:TODO|FIXME)\b"), "Unresolved implementation note", "Resolve the note or track it as a planned issue before release."),
    ]
    findings: List[Tuple[str, str, str, str, str]] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [name for name in dirs if name not in _IGNORED_DIRS and not name.startswith(".")]
        for name in sorted(files):
            path = Path(root) / name
            if path.suffix.lower() not in _SOURCE_EXTENSIONS:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines[:800], 1):
                for severity, pattern, title, fix in rules:
                    if pattern.search(line):
                        findings.append((severity, title, f"{path.relative_to(repo_dir).as_posix()}:{number}", line.strip()[:180], fix))
                        break
            if len(findings) >= 80:
                return findings
    return findings


def _static_report(repo_name: str, repo_dir: Path, reason: str = "") -> Tuple[str, Dict[str, int], str]:
    findings = _static_findings(repo_dir)
    counts = {level: sum(1 for finding in findings if finding[0] == level) for level in ("P0", "P1", "P2", "P3")}
    gate = "BLOCKED" if counts["P0"] or counts["P1"] else "APPROVED"
    detail = "\n".join(
        f"### [{severity}] {title}\n- **Location:** `{location}`\n- **Evidence:** `{snippet}`\n- **Fix:** {fix}\n"
        for severity, title, location, snippet, fix in findings
    ) or "No deterministic P0–P3 patterns were found in the inspected files."
    note = f"\n> Static fallback used: {reason[:160]}\n" if reason else ""
    report = f"""# Preflight Code Review Report: {repo_name}
**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Merge Gate:** {'🔴 BLOCKED' if gate == 'BLOCKED' else '🟢 APPROVED'}
{note}
| Severity | Count |
| --- | ---: |
| P0 | {counts['P0']} |
| P1 | {counts['P1']} |
| P2 | {counts['P2']} |
| P3 | {counts['P3']} |

## Findings
{detail}
"""
    return report, counts, gate


def _counts_from_report(report: str) -> Dict[str, int]:
    return {level: len(re.findall(rf"\[?{level}\]?", report)) for level in ("P0", "P1", "P2", "P3")}


def run_preflight_code_review(repo_name: Optional[str] = None) -> Dict[str, Any]:
    key, repo_dir = _repo_dir(repo_name)
    context = collect_repo_code_context(repo_dir)
    if not context:
        raise HTTPException(status_code=422, detail="No reviewable source files were found in this repository.")
    user_prompt = f"""Review repository `{key}`. The following is read-only source context:\n\n{context}"""
    engine = "heuristic-fallback"
    report, counts, gate = _static_report(key, repo_dir)
    if os.getenv("TESTING") != "1":
        connected, detail = verify_llm_connection(timeout=3.0)
        if connected:
            try:
                candidate = call_llm(user_prompt, PREFLIGHT_REVIEW_SYSTEM_PROMPT, timeout=int(os.getenv("LLM_REVIEW_TIMEOUT", "45")), max_tokens=3500)
                if candidate and candidate.strip():
                    report = candidate.strip()
                    counts = _counts_from_report(report)
                    gate = "BLOCKED" if counts["P0"] or counts["P1"] else "APPROVED"
                    engine = "vultr-inference"
            except Exception as exc:
                report, counts, gate = _static_report(key, repo_dir, str(exc))
        else:
            report, counts, gate = _static_report(key, repo_dir, detail)
    _LAST_REPORTS[key] = report
    _LAST_REPORTS["default"] = report
    return {"status": "success", "repo_name": key, "merge_gate": gate, "counts": counts, "markdown_report": report, "report_md": report, "engine": engine, "prompt": user_prompt, "system_prompt": PREFLIGHT_REVIEW_SYSTEM_PROMPT, "user_prompt": user_prompt}


def get_last_preflight_report(repo_name: Optional[str] = None) -> str:
    key = sanitize_name(repo_name) if repo_name else "default"
    return _LAST_REPORTS.get(key) or run_preflight_code_review(repo_name).get("markdown_report", "")
