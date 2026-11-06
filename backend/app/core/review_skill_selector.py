"""Progressive code-review skill selection for Preflight.

The selection model follows the MIT-licensed `awesome-skills/code-review-skill`
project.  Only the relevant language/framework instructions are placed in the
model context, together with universal security and quality checks.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List


SOURCE = "awesome-skills/code-review-skill (MIT)"

CORE_SKILL = """# Mandatory Code Review Protocol
Follow four phases: establish context; assess architecture, performance and tests;
inspect code line by line for correctness, security, errors and maintainability;
then give a concise merge decision. Report only evidence present in the supplied
files. Do not review formatting that a linter can check. Every finding needs a
path, line when available, impact, and a concrete remediation. Label findings as
[blocking], [important], [nit], [suggestion], [learning], or [praise]."""

GUIDES: Dict[str, str] = {
    "typescript": """# TypeScript / JavaScript skill
Check strict type boundaries, unsafe any/unknown casts, promise rejection paths,
async cancellation, stale closures, mutation, untrusted browser input, dependency
and API contract compatibility. For React components also inspect hooks dependency
arrays, cleanup, loading/error/empty states, accessibility and XSS boundaries.""",
    "react": """# React / Next.js skill
Review hook rules and effect cleanup, server/client component boundaries, Suspense
and error boundaries, optimistic updates and rollback, stable keys, race conditions,
form pending states, data-fetch cache invalidation and safe rendering of user data.""",
    "vue": """# Vue skill
Review Composition API reactivity and watcher cleanup, prop mutation, emit contracts,
async component errors, keyed lists, router/auth guards and template XSS exposure.""",
    "angular": """# Angular skill
Review signal/RxJS lifecycle cleanup, subscription leaks, change detection cost,
template safety, route guards, HTTP interceptor error handling and typed forms.""",
    "svelte": """# Svelte / SvelteKit skill
Review rune/store lifecycle, SSR versus browser-only boundaries, load/action error
handling, form validation, reactive loops and safe serialization of user data.""",
    "python": """# Python skill
Check mutable defaults, exception breadth, resource/context cleanup, typing at public
boundaries, async code that blocks the event loop, subprocess safety, serialization,
input validation, tests for failure paths and secrets in configuration.""",
    "fastapi": """# FastAPI skill
Check Pydantic validation and response models, dependency/authentication versus
authorization, async correctness, database session lifecycle, status codes, exception
handlers, pagination, CORS/CSRF, background-task failure handling and test coverage.""",
    "django": """# Django / DRF skill
Check serializer validation, permissions on every object access, queryset filtering,
N+1 queries, transaction boundaries, migrations, CSRF/session behavior, escaping,
file uploads, async view safety and DRF error responses.""",
    "csharp": """# C# / .NET skill
Check nullable reference handling, async/await (no blocking .Result/.Wait), cancellation
tokens, IDisposable lifetime, EF Core query translation and N+1 patterns, LINQ multiple
enumeration, ASP.NET authorization, model binding validation, logging without secrets,
configuration and exception middleware.""",
    "java": """# Java / Spring skill
Check null/Optional use, stream side effects, exception boundaries, virtual-thread or
executor lifecycle, Spring constructor injection, bean scope, validation, transaction
boundaries, JPA lazy-loading/N+1, authorization and HTTP error contracts.""",
    "php": """# PHP skill
Check strict types, input validation, PDO parameterization, output escaping, auth and
authorization, Composer dependency constraints, error reporting, file upload handling
and PHPUnit coverage for edge cases.""",
    "ruby": """# Ruby / Rails skill
Check strong parameters, authorization, Active Record query count/N+1, transactions,
background job idempotency, escaping, time zones, exception handling and test coverage.""",
    "nestjs": """# NestJS skill
Check DTO validation, guard coverage, dependency injection scopes, circular dependencies,
interceptor/filter error behavior, async provider failures, ORM query count and API
authorization boundaries.""",
    "go": """# Go skill
Check error wrapping and handling, context propagation/cancellation, goroutine lifecycle,
channel ownership and closing, data races, defer cleanup, bounds checks, SQL parameters,
HTTP timeouts and tests with race-prone paths.""",
    "rust": """# Rust skill
Check ownership and lifetime assumptions, unsafe blocks with safety invariants, Result
handling, panic boundaries, async blocking/cancellation, lock ordering, overflow, input
validation and error types suitable for library versus application code.""",
    "cpp": """# C / C++ skill
Check ownership, RAII, bounds and integer overflow, null/dangling pointers, undefined
behavior, exception safety, concurrency, resource cleanup, untrusted input and tests
with sanitizers or edge cases.""",
    "mobile": """# Mobile skill
Check lifecycle and cancellation, memory leaks, UI-thread blocking, null safety, state
restoration, permission/data handling, network errors and platform-specific tests.""",
    "security": """# Mandatory security skill
Review authentication and object-level authorization, injection (SQL/command/template),
XSS/output encoding, SSRF, path traversal, unsafe deserialization, CSRF, secrets,
rate limits, logging privacy, dependency trust and validation at every trust boundary.""",
    "quality": """# Universal quality skill
Check cohesion/coupling, duplicated logic, parameter sprawl, leaky abstractions, nested
conditionals, stringly typed contracts, TOCTOU, no-op updates, race conditions, error
handling, observability, test strategy and backwards compatibility.""",
    "performance": """# Performance skill
Check algorithmic complexity, N+1 queries, unbounded reads, repeated rendering/work,
memory retention, cache invalidation, network timeouts, batching and cancellation.""",
}


def _read_package_json(repo_dir: Path) -> dict:
    for candidate in repo_dir.rglob("package.json"):
        if any(part in {"node_modules", ".git"} for part in candidate.parts):
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _has_any(files: Iterable[Path], suffixes: tuple[str, ...]) -> bool:
    return any(path.suffix.lower() in suffixes for path in files)


def select_review_skills(repo_dir: Path) -> List[str]:
    """Return stable, relevant guide names from repository evidence."""
    files = [path for path in repo_dir.rglob("*") if path.is_file() and not any(part in {".git", "node_modules", "bin", "obj", "dist", "build", ".venv", "venv"} for part in path.parts)]
    names = ["security", "quality", "performance"]
    package = _read_package_json(repo_dir)
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    deps = {str(key).lower() for key in dependencies}
    text = " ".join(path.as_posix().lower() for path in files)

    if _has_any(files, (".cs", ".razor", ".csproj")):
        names.append("csharp")
    if _has_any(files, (".py",)):
        names.append("fastapi" if "fastapi" in text else "django" if "django" in text else "python")
    if _has_any(files, (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
        names.append("typescript")
        if "react" in deps or "next" in deps:
            names.append("react")
        elif "vue" in deps:
            names.append("vue")
        elif "@angular/core" in deps:
            names.append("angular")
        elif "@nestjs/core" in deps:
            names.append("nestjs")
        elif "svelte" in deps or "@sveltejs/kit" in deps:
            names.append("svelte")
    if _has_any(files, (".java",)):
        names.append("java")
    if _has_any(files, (".php",)):
        names.append("php")
    if _has_any(files, (".rb",)):
        names.append("ruby")
    if _has_any(files, (".go",)):
        names.append("go")
    if _has_any(files, (".rs",)):
        names.append("rust")
    if _has_any(files, (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp")):
        names.append("cpp")
    if _has_any(files, (".kt", ".swift", ".dart")):
        names.append("mobile")
    return list(dict.fromkeys(names))


def build_review_skill_bundle(repo_dir: Path) -> tuple[List[str], str]:
    selected = select_review_skills(repo_dir)
    guides = "\n\n".join(GUIDES[name] for name in selected)
    bundle = f"""Skill source: {SOURCE}.

{CORE_SKILL}

# Selected skills (mandatory)
{guides}

The selected skill instructions above are mandatory for this review. Do not omit
a selected guide, do not invent evidence, and do not report a runtime result."""
    return selected, bundle
