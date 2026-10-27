import os
import re
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Set
from fastapi import HTTPException

from app.core.security import ALLOWED_REPO_DIR, sanitize_name
from app.core.llm import call_llm
from app.core.preflight import get_candidate_directories
from app.core.skills import skill_inspect_routes

IGNORED_SCAN_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "env", "node_modules",
    ".idea", ".vscode", "dist", "build", "egg-info", "target", "vendor",
    "bin", "obj", ".gradle", ".mvn", ".gemini", ".system_generated"
}


def detect_repo_tech_stack(repo_dir: Path) -> Dict[str, Any]:
    """Detects languages, frameworks, manifests, and project structure from repository directory."""
    manifests = []
    frameworks = []
    languages = []
    entrypoints = []

    # 1. Manifest discovery
    if (repo_dir / "package.json").exists():
        manifests.append("package.json")
        languages.append("JavaScript")
        try:
            pkg = json.loads((repo_dir / "package.json").read_text(encoding="utf-8", errors="ignore"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "typescript" in deps or list(repo_dir.rglob("*.ts")) or list(repo_dir.rglob("*.tsx")):
                languages.append("TypeScript")
            if "next" in deps:
                frameworks.append("Next.js")
            if "react" in deps:
                frameworks.append("React")
            if "vue" in deps:
                frameworks.append("Vue.js")
            if "express" in deps:
                frameworks.append("Express.js")
            if "fastify" in deps:
                frameworks.append("Fastify")
            if "tailwindcss" in deps:
                frameworks.append("Tailwind CSS")
            frameworks.append(f"Node.js (Name: {pkg.get('name', 'app')})")
        except Exception:
            pass

    # .NET / C#
    sln_files = list(repo_dir.glob("*.sln"))
    csproj_files = list(repo_dir.rglob("*.csproj"))
    if sln_files or csproj_files or list(repo_dir.rglob("*.cs")):
        languages.append("C# / .NET")
        frameworks.append("ASP.NET Core")
        for csproj in csproj_files[:4]:
            manifests.append(csproj.name)
            try:
                xml_text = csproj.read_text(encoding="utf-8", errors="ignore")
                if "Microsoft.AspNetCore" in xml_text:
                    frameworks.append("Web API")
                if "Blazor" in xml_text or list(repo_dir.rglob("*.razor")):
                    frameworks.append("Blazor UI")
            except Exception:
                pass

    if (repo_dir / "appsettings.json").exists():
        manifests.append("appsettings.json")

    # Python
    pyproject = repo_dir / "pyproject.toml"
    reqs = repo_dir / "requirements.txt"
    if pyproject.exists() or reqs.exists() or list(repo_dir.glob("*.py")):
        languages.append("Python")
        if reqs.exists():
            manifests.append("requirements.txt")
            txt = reqs.read_text(encoding="utf-8", errors="ignore").lower()
            if "fastapi" in txt:
                frameworks.append("FastAPI")
            elif "django" in txt:
                frameworks.append("Django")
            elif "flask" in txt:
                frameworks.append("Flask")

    # Go
    if (repo_dir / "go.mod").exists() or list(repo_dir.rglob("*.go")):
        languages.append("Go")
        if (repo_dir / "go.mod").exists():
            manifests.append("go.mod")
        frameworks.append("Go")

    # Rust
    if (repo_dir / "Cargo.toml").exists() or list(repo_dir.rglob("*.rs")):
        languages.append("Rust")
        if (repo_dir / "Cargo.toml").exists():
            manifests.append("Cargo.toml")
        frameworks.append("Rust")

    # 2. Key entrypoint files
    entrypoint_candidates = [
        "Program.cs", "Startup.cs", "main.py", "app.py", "index.js",
        "index.ts", "server.js", "main.go", "App.razor", "App.tsx", "App.jsx"
    ]
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_SCAN_DIRS and not d.startswith(".")]
        for f in files:
            if f in entrypoint_candidates:
                try:
                    rel_p = (Path(root) / f).relative_to(repo_dir).as_posix()
                    entrypoints.append(rel_p)
                except Exception:
                    pass

    return {
        "languages": list(dict.fromkeys(languages)),
        "frameworks": list(dict.fromkeys(frameworks)),
        "manifests": list(dict.fromkeys(manifests))[:8],
        "entrypoints": entrypoints[:6]
    }


def extract_repo_context_for_readme(repo_name: str, repo_dir: Path) -> str:
    """Collects representative code, existing docs, and routes from target repository."""
    tech = detect_repo_tech_stack(repo_dir)
    context_lines = [
        f"### Target Repository: {repo_name}",
        f"- **Primary Tech Stack & Frameworks:** {', '.join(tech['frameworks']) or 'Universal Codebase'}",
        f"- **Key Manifests & Project Files:** {', '.join(tech['manifests']) or 'None'}",
        f"- **Application Entrypoints:** {', '.join(tech['entrypoints']) or 'Standard'}",
        ""
    ]

    # 1. Existing README preview if present
    readme_path = repo_dir / "README.md"
    if readme_path.exists():
        try:
            content = readme_path.read_text(encoding="utf-8", errors="ignore").strip()
            if content:
                context_lines.append(f"### Existing README Preview (Context):\n{content[:2000]}\n")
        except Exception:
            pass

    # 2. Discovered API Routes
    try:
        route_info = skill_inspect_routes(repo_name)
        if route_info.get("endpoints"):
            context_lines.append("### Discovered API Routes in Project:")
            for ep in route_info["endpoints"][:25]:
                context_lines.append(f"- `{ep['method']}` `{ep['path']}` in `{ep['file']}` (Auth: {'Yes' if ep['has_auth'] else 'No'})")
            context_lines.append("")
    except Exception:
        pass

    # 3. Key source code samples
    code_samples = []
    allowed_exts = {".cs", ".razor", ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java"}
    chars_collected = 0

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_SCAN_DIRS and not d.startswith(".")]
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in allowed_exts:
                file_path = Path(root) / f
                try:
                    rel_name = file_path.relative_to(repo_dir).as_posix()
                    # Prioritize controllers, services, models, and entrypoints
                    is_core = any(k in rel_name.lower() for k in ["controller", "service", "model", "program", "api", "route"])
                    if not is_core and len(code_samples) >= 4:
                        continue
                    text = file_path.read_text(encoding="utf-8", errors="ignore")[:1500]
                    sample = f"=== File: {rel_name} ===\n{text}\n"
                    code_samples.append(sample)
                    chars_collected += len(sample)
                    if chars_collected >= 10000:
                        break
                except Exception:
                    continue
        if chars_collected >= 10000:
            break

    context_lines.append("### Representative Code Excerpts from Target Project:")
    context_lines.extend(code_samples)
    return "\n".join(context_lines)


def scan_repo_environment_variables(repo_dir: Path) -> List[Dict[str, str]]:
    """Scans all files in target repository for environment variables, config keys, and connection strings."""
    discovered = {}

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_SCAN_DIRS and not d.startswith(".")]
        for f in files:
            fpath = Path(root) / f
            fname = f.lower()

            # 1. Parse appsettings.json or config.json for .NET / Web
            if fname.endswith("appsettings.json") or fname.endswith("appsettings.development.json"):
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8", errors="ignore"))
                    # Connection Strings
                    if isinstance(data.get("ConnectionStrings"), dict):
                        for k, v in data["ConnectionStrings"].items():
                            discovered[f"ConnectionStrings__{k}"] = f"Database connection string for {k}"
                    # JWT Settings
                    if isinstance(data.get("JwtSettings"), dict):
                        for k, v in data["JwtSettings"].items():
                            discovered[f"JwtSettings__{k}"] = f"JWT authentication configuration ({k})"
                    # SMTP / Email
                    if isinstance(data.get("SmtpSettings"), dict):
                        for k, v in data["SmtpSettings"].items():
                            discovered[f"SmtpSettings__{k}"] = f"SMTP Email setting ({k})"
                    # OAuth
                    if isinstance(data.get("GoogleOAuth"), dict):
                        for k, v in data["GoogleOAuth"].items():
                            discovered[f"GoogleOAuth__{k}"] = f"OAuth credential ({k})"
                except Exception:
                    pass

            # 2. Parse existing .env or .env.example in target repo
            if fname.startswith(".env"):
                try:
                    for line in fpath.read_text(encoding="utf-8", errors="ignore").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            var_name = line.split("=", 1)[0].strip()
                            if var_name:
                                discovered[var_name] = f"Configured in {fname}"
                except Exception:
                    pass

            # 3. Source code regex search across .py, .js, .ts, .cs, .go
            ext = fpath.suffix.lower()
            if ext in {".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".go", ".java", ".php"}:
                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                    rel_p = fpath.relative_to(repo_dir).as_posix()

                    # process.env.VAR_NAME
                    for m in re.finditer(r'process\.env\.([A-Za-z0-9_]+)', text):
                        var = m.group(1)
                        if var not in discovered:
                            discovered[var] = f"Used in {rel_p}"

                    # os.getenv("VAR_NAME") or os.environ["VAR_NAME"]
                    for m in re.finditer(r'os\.(?:getenv|environ(?:\[|\.get\())\s*[\'"]([A-Za-z0-9_]+)[\'"]', text):
                        var = m.group(1)
                        if var not in discovered:
                            discovered[var] = f"Used in {rel_p}"

                    # C# Configuration["..."]
                    for m in re.finditer(r'(?:Configuration|builder\.Configuration)\[[\'"]([A-Za-z0-9_:]+)[\'"]\]', text):
                        var = m.group(1).replace(":", "__")
                        if var not in discovered:
                            discovered[var] = f"Used in {rel_p}"

                    # C# GetConnectionString("...")
                    for m in re.finditer(r'GetConnectionString\([\'"]([A-Za-z0-9_]+)[\'"]\)', text):
                        var = f"ConnectionStrings__{m.group(1)}"
                        if var not in discovered:
                            discovered[var] = f"Connection string referenced in {rel_p}"
                except Exception:
                    continue

    result = []
    for k, v in discovered.items():
        result.append({"variable": k, "description": v})
    return result


def generate_project_asset(repo_name: str, asset_type: str) -> Dict[str, Any]:
    """
    Generates tailored, high-grade project deliverables (.README.md or .env.example)
    specifically for the inspected target repository.
    """
    clean_repo = sanitize_name(repo_name)
    if not clean_repo:
        raise HTTPException(status_code=400, detail="Repository name is required.")

    candidates = get_candidate_directories(clean_repo)
    if not candidates:
        raise HTTPException(status_code=404, detail=f"Repository '{clean_repo}' not found on disk.")

    repo_dir = candidates[0]
    asset_clean = (asset_type or "readme").strip().lower()

    # ==================== 1. GENERATE POLISHED README.MD ====================
    if asset_clean == "readme":
        context_data = extract_repo_context_for_readme(clean_repo, repo_dir)
        tech = detect_repo_tech_stack(repo_dir)
        frameworks_str = ", ".join(tech["frameworks"]) or "Multi-platform application"

        system_prompt = (
            "You are an elite software architect and technical writer. "
            f"Your job is to generate a comprehensive, polished, and production-grade GitHub README.md "
            f"specifically and exclusively for the user's project: '{clean_repo}'.\n\n"
            "CRITICAL CONSTRAINTS:\n"
            f"1. You must write 100% about '{clean_repo}' based on its actual files, routes, and technologies provided.\n"
            "2. DO NOT write about CodeGraph Brain, Preflight, or this scanner platform.\n"
            "3. Format cleanly in GitHub Flavored Markdown with badges, tables, and code snippets."
        )

        user_prompt = f"""Generate a world-class GitHub README.md for the following project:

{context_data}

Structure your README.md with the following sections:
1. # {clean_repo} (Title, engaging tagline, and technology badges for {frameworks_str})
2. 🌟 Key Features (Comprehensive list of features found in the codebase and controllers)
3. 🏛️ Architecture & Clean Design (Explain project layers, structure, and design patterns used)
4. 🛠️ Tech Stack (Languages, frameworks, ORM, database, and libraries detected)
5. 🌐 Discovered API Endpoints (Clean markdown table listing discovered HTTP routes and their roles)
6. 🚀 Prerequisites & Installation (Tailored step-by-step setup commands for {frameworks_str})
7. ⚙️ Configuration (.env or appsettings setup instructions)
8. 📜 License

Generate the complete, production-ready README.md:"""

        content = call_llm(user_prompt, system_prompt, timeout=120)
        return {
            "success": True,
            "repo_name": clean_repo,
            "asset_type": "readme",
            "filename": "README.md",
            "title": f"Polished README.md for {clean_repo}",
            "content": content.strip()
        }

    # ==================== 2. GENERATE SAFE .ENV.EXAMPLE ====================
    elif asset_clean == "env":
        env_vars = scan_repo_environment_variables(repo_dir)
        tech = detect_repo_tech_stack(repo_dir)
        frameworks_str = ", ".join(tech["frameworks"]) or "Application"

        vars_summary = "\n".join([f"- `{v['variable']}`: {v['description']}" for v in env_vars])
        if not vars_summary:
            vars_summary = "No explicit environment variables extracted via static patterns. Generate standard configuration keys for this tech stack."

        system_prompt = (
            "You are an expert DevOps and Application Security Engineer. "
            f"Your task is to generate a clean, safe, and production-grade `.env.example` file "
            f"strictly for the target application: '{clean_repo}'.\n\n"
            "CRITICAL CONSTRAINTS:\n"
            f"1. Include ONLY variables relevant to '{clean_repo}' ({frameworks_str}).\n"
            "2. DO NOT include variables from CodeGraph Brain (e.g. NEO4J_URI, CELERY_BROKER_URL, VLLM) unless the target repository itself requires them.\n"
            "3. Group variables logically with comments (# Database, # Authentication & JWT, # OAuth, # Server).\n"
            "4. Provide safe placeholder values (e.g. your_db_password_here) and helpful comments explaining where to get each key.\n"
            "5. Return ONLY the raw .env.example content."
        )

        user_prompt = f"""Generate a safe, complete `.env.example` for project '{clean_repo}'.

Target Project Tech Stack: {frameworks_str}
Discovered Configuration Keys & Variables in '{clean_repo}':
{vars_summary}

Generate the complete `.env.example` file now:"""

        content = call_llm(user_prompt, system_prompt, timeout=90)
        
        # Strip markdown fences if LLM enclosed them
        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            cleaned = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:]).strip()

        return {
            "success": True,
            "repo_name": clean_repo,
            "asset_type": "env",
            "filename": ".env.example",
            "title": f"Safe .env.example for {clean_repo}",
            "content": cleaned
        }

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported asset type '{asset_type}'. Supported: 'readme', 'env'.")
