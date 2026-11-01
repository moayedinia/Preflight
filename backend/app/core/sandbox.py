import os
import sys
import re
import json
import socket
import logging
import threading
import subprocess
import shutil
import shlex
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from collections import deque
from datetime import datetime

from app.core.security import ALLOWED_REPO_DIR, sanitize_name
from app.core.llm import call_llm, verify_llm_connection

logger = logging.getLogger("codegraph.sandbox")

# Global registry of active sandboxes: repo_name -> SandboxSession
_ACTIVE_SANDBOXES: Dict[str, "SandboxSession"] = {}
_SANDBOX_LOCK = threading.Lock()

# Directories to skip when scanning file trees
_IGNORE_DIRS = {
    ".git", ".github", ".vscode", ".idea", "node_modules", "bin", "obj",
    "__pycache__", ".venv", "venv", "env", "target", "dist", "build",
    ".next", ".nuxt", ".output", "coverage", ".pytest_cache"
}


def find_available_port(start_port: int = 9100, max_port: int = 9990) -> int:
    """Finds an unoccupied TCP port on localhost within the designated range."""
    for port in range(start_port, max_port + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    # Fallback to ephemeral port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _read_snippet_safe(file_path: Path, max_bytes: int = 16384) -> str:
    """Safely reads up to max_bytes from a text file, handling various encodings."""
    if not file_path.exists() or not file_path.is_file():
        return ""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except Exception as e:
        logger.debug(f"Failed to read snippet from {file_path}: {e}")
        return ""


def _collect_repo_file_tree(repo_dir: Path, max_files: int = 80, max_depth: int = 3) -> List[str]:
    """Collects relative paths of key files in the repository up to max_depth."""
    tree = []
    try:
        def _walk(current: Path, depth: int):
            if depth > max_depth or len(tree) >= max_files:
                return
            for item in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if item.name in _IGNORE_DIRS or item.name.startswith("."):
                    continue
                rel = item.relative_to(repo_dir).as_posix()
                tree.append(rel + ("/" if item.is_dir() else ""))
                if item.is_dir():
                    _walk(item, depth + 1)
                if len(tree) >= max_files:
                    break
        _walk(repo_dir, 1)
    except Exception as e:
        logger.debug(f"Error collecting file tree: {e}")
    return tree


def detect_heuristic_launch_plan(repo_dir: Path, file_tree: List[str], readme_content: str, readme_file_used: str) -> Dict[str, Any]:
    """
    Comprehensive multi-language heuristic scanner supporting:
    - C# / .NET (Blazor, WebAPI, MVC, Console)
    - Node.js / TypeScript (Next.js, Vite, React, Express, Nest, etc.)
    - Python (FastAPI, Flask, Django, etc.)
    - Java / Kotlin (Maven pom.xml, Gradle)
    - Go (go.mod)
    - Rust (Cargo.toml)
    - PHP (Artisan, Composer)
    - Docker / Static Web
    """
    detected_type = "Generic"
    detected_cmd = ""
    detected_install = ""
    detected_port = 5000
    detected_from = "heuristics"

    file_names_lower = [f.lower() for f in file_tree]

    # 1. C# / .NET Detection
    sln_files = [f for f in file_tree if f.endswith(".sln")]
    csproj_files = [f for f in file_tree if f.endswith(".csproj")]
    fsproj_files = [f for f in file_tree if f.endswith(".fsproj")]

    if sln_files or csproj_files or fsproj_files or any(f.endswith(".cs") for f in file_tree):
        detected_type = "C# (.NET)"
        detected_install = "dotnet restore"
        detected_port = 5000
        detected_from = ".NET Solution / Project"

        # Check for Blazor / ASP.NET / Web project
        web_project = None
        for proj in csproj_files:
            proj_path = repo_dir / proj
            proj_txt = _read_snippet_safe(proj_path, 8000).lower()
            if "blazor" in proj_txt or "microsoft.net.sdk.web" in proj_txt or "web" in proj.lower():
                web_project = proj
                detected_type = "C# (.NET Blazor / Web)"
                break
        
        target_proj = web_project or (csproj_files[0] if csproj_files else None)
        if target_proj:
            detected_cmd = f"dotnet run --project {target_proj}"
        elif sln_files:
            detected_cmd = "dotnet run"
        else:
            detected_cmd = "dotnet run"

    # 2. Node.js / JavaScript / TypeScript Detection
    elif any(f == "package.json" or f.endswith("/package.json") for f in file_tree):
        pkg_json_rel = next((f for f in file_tree if f == "package.json" or f.endswith("/package.json")), "package.json")
        pkg_json_path = repo_dir / pkg_json_rel
        try:
            with open(pkg_json_path, "r", encoding="utf-8", errors="replace") as f:
                pkg_data = json.load(f)
            scripts = pkg_data.get("scripts", {})
            deps = {**pkg_data.get("dependencies", {}), **pkg_data.get("devDependencies", {})}

            if "next" in deps:
                detected_type = "Node.js (Next.js)"
                detected_port = 3000
            elif "vite" in deps:
                detected_type = "Node.js (Vite)"
                detected_port = 5173
            elif "react-scripts" in deps:
                detected_type = "Node.js (Create React App)"
                detected_port = 3000
            elif "nuxt" in deps:
                detected_type = "Node.js (Nuxt)"
                detected_port = 3000
            elif "express" in deps or "fastify" in deps or "nest" in deps:
                detected_type = "Node.js (Server API)"
                detected_port = 3000
            else:
                detected_type = "Node.js"
                detected_port = 3000

            pm = "npm"
            if (repo_dir / "pnpm-lock.yaml").exists():
                pm = "pnpm"
            elif (repo_dir / "yarn.lock").exists():
                pm = "yarn"
            elif (repo_dir / "bun.lockb").exists():
                pm = "bun"

            detected_install = f"{pm} install"
            sub_prefix = f"cd {Path(pkg_json_rel).parent} && " if "/" in pkg_json_rel else ""

            if "dev" in scripts:
                detected_cmd = f"{sub_prefix}{pm} run dev"
            elif "start" in scripts:
                detected_cmd = f"{sub_prefix}{pm} start"
            elif "serve" in scripts:
                detected_cmd = f"{sub_prefix}{pm} run serve"
            else:
                main_file = pkg_data.get("main", "index.js")
                detected_cmd = f"{sub_prefix}node {main_file}"

            detected_from = "package.json scripts"
        except Exception as e:
            logger.debug(f"Error parsing package.json: {e}")

    # 3. Python Detection (FastAPI, Flask, Django, etc.)
    elif (repo_dir / "requirements.txt").exists() or (repo_dir / "pyproject.toml").exists() or any(f.endswith(".py") for f in file_tree):
        detected_type = "Python"
        detected_install = "pip install -r requirements.txt" if (repo_dir / "requirements.txt").exists() else "pip install -e ."
        detected_port = 8000

        is_fastapi = False
        fastapi_target = ""
        for py_f in [repo_dir / "main.py", repo_dir / "app.py", repo_dir / "app" / "main.py"]:
            if py_f.exists():
                txt = _read_snippet_safe(py_f, 4000)
                if "FastAPI" in txt or "uvicorn" in txt:
                    is_fastapi = True
                    mod_name = "app.main:app" if "app" in py_f.parts else f"{py_f.stem}:app"
                    fastapi_target = mod_name
                    break

        if is_fastapi:
            detected_type = "Python (FastAPI)"
            detected_cmd = f"uvicorn {fastapi_target} --host 0.0.0.0 --port {{PORT}}"
            detected_from = "FastAPI entrypoint"
        elif (repo_dir / "manage.py").exists():
            detected_type = "Python (Django)"
            detected_cmd = "python manage.py runserver 0.0.0.0:{PORT}"
            detected_from = "Django manage.py"
        elif (repo_dir / "app.py").exists():
            detected_type = "Python (Flask / App)"
            detected_cmd = "python app.py"
            detected_from = "app.py"
        elif (repo_dir / "main.py").exists():
            detected_type = "Python (Main)"
            detected_cmd = "python main.py"
            detected_from = "main.py"
        else:
            py_candidates = [f for f in file_tree if f.endswith(".py") and "/" not in f]
            detected_cmd = f"python {py_candidates[0]}" if py_candidates else "python -m http.server {PORT}"
            detected_from = "Python script"

    # 4. Java / Kotlin Detection (Maven / Gradle)
    elif (repo_dir / "pom.xml").exists():
        detected_type = "Java (Maven)"
        detected_install = "mvn clean install -DskipTests"
        detected_cmd = "mvn spring-boot:run"
        detected_port = 8080
        detected_from = "Maven pom.xml"
    elif (repo_dir / "build.gradle").exists() or (repo_dir / "build.gradle.kts").exists():
        detected_type = "Java/Kotlin (Gradle)"
        gradle_cmd = "./gradlew" if sys.platform != "win32" and (repo_dir / "gradlew").exists() else "gradle"
        detected_install = f"{gradle_cmd} build -x test"
        detected_cmd = f"{gradle_cmd} bootRun"
        detected_port = 8080
        detected_from = "Gradle build file"

    # 5. Rust Detection (Cargo)
    elif (repo_dir / "Cargo.toml").exists():
        detected_type = "Rust"
        detected_install = "cargo build"
        detected_cmd = "cargo run"
        detected_port = 8080
        detected_from = "Cargo.toml"

    # 6. Go Detection (go.mod)
    elif (repo_dir / "go.mod").exists():
        detected_type = "Go"
        detected_install = "go mod download"
        detected_cmd = "go run ."
        detected_port = 8080
        detected_from = "go.mod"

    # 7. PHP Detection (Artisan / Composer)
    elif (repo_dir / "artisan").exists():
        detected_type = "PHP (Laravel)"
        detected_install = "composer install"
        detected_cmd = "php artisan serve --port={PORT}"
        detected_port = 8000
        detected_from = "Laravel artisan"
    elif (repo_dir / "composer.json").exists():
        detected_type = "PHP"
        detected_install = "composer install"
        detected_cmd = "php -S 0.0.0.0:{PORT}"
        detected_port = 8000
        detected_from = "composer.json"

    # 8. Static HTML fallback
    elif (repo_dir / "index.html").exists():
        detected_type = "Static HTML / Web"
        detected_install = ""
        detected_cmd = "python -m http.server {PORT}"
        detected_port = 8080
        detected_from = "index.html"
    else:
        detected_type = "Generic Codebase"
        detected_cmd = "python -m http.server {PORT}"
        detected_port = 8000

    # 9. Extract explicit CLI command from README if present
    if readme_content:
        patterns = [
            r"```(?:bash|sh|shell|powershell|cmd)?\s*[\r\n]+(?:\$\s*)?((?:dotnet\s+run[^\r\n]*))",
            r"```(?:bash|sh|shell)?\s*[\r\n]+(?:\$\s*)?((?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve)[^\r\n]*)",
            r"```(?:bash|sh|shell)?\s*[\r\n]+(?:\$\s*)?((?:uvicorn\s+[\w\.\:\-]+|python\s+[\w\.\/\-]+\.py|flask\s+run)[^\r\n]*)",
            r"```(?:bash|sh|shell)?\s*[\r\n]+(?:\$\s*)?((?:cargo\s+run|go\s+run\s+\.)[^\r\n]*)"
        ]
        for pat in patterns:
            m = re.search(pat, readme_content, re.IGNORECASE)
            if m:
                extracted = m.group(1).strip()
                if extracted:
                    detected_cmd = extracted
                    detected_from = f"{readme_file_used} instructions"
                    break

    return {
        "project_type": detected_type,
        "command": detected_cmd,
        "install_command": detected_install,
        "default_port": detected_port,
        "detected_from": detected_from
    }


def analyze_launch_with_llm(
    repo_name: str,
    file_tree: List[str],
    readme_content: str,
    readme_file_used: str,
    heuristic_plan: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Leverages the configured AI model to analyze the codebase structure and README instructions,
    returning the precise language, framework, execution command, and port.
    """
    if os.getenv("TESTING") == "1":
        return None

    try:
        is_conn, _ = verify_llm_connection(timeout=3.0)
        if not is_conn:
            return None

        # Build clean representation of files
        files_str = "\n".join(f"- {f}" for f in file_tree[:45])
        readme_snippet = readme_content[:3500] if readme_content else "No README file found."

        system_prompt = (
            "You are an expert autonomous DevOps and systems engineer. "
            "Analyze the provided repository files and README to deduce the exact commands to run this application. "
            "Return valid JSON ONLY, without markdown fences or additional conversational text."
        )

        user_prompt = f"""Target Repository: '{repo_name}'

Files present in repository:
{files_str}

README instructions snippet ({readme_file_used or 'N/A'}):
```
{readme_snippet}
```

Preliminary Heuristic Detection:
- Type: {heuristic_plan.get('project_type')}
- Default Command: {heuristic_plan.get('command')}
- Default Port: {heuristic_plan.get('default_port')}

Instructions:
1. Identify the exact programming language and framework (e.g., "C# (.NET 8 Blazor WASM)", "Node.js (Next.js)", "Python (FastAPI)", "Go", "Rust", etc.).
2. Determine the exact single CLI command to run this project in a terminal.
   - For C# / .NET projects with .sln or .csproj, use the appropriate `dotnet run --project <path>` or `dotnet run`.
   - For Node.js, use `npm run dev` or `npm start`.
   - For Python, use `python <file>.py` or `uvicorn ...`.
3. Provide the install/build command (e.g. `dotnet restore`, `npm install`, `pip install -r requirements.txt`).
4. Provide the expected HTTP port number (integer).
5. Give a 1-sentence explanation of why this command is correct based on the files/README.

Return strictly valid JSON with these keys:
{{
  "project_type": "string",
  "command": "string",
  "install_command": "string",
  "port": 5000,
  "explanation": "string"
}}"""

        raw_resp = call_llm(user_prompt, system_prompt, timeout=20)
        # Parse JSON from response
        match = re.search(r"\{[\s\S]*\}", raw_resp)
        if match:
            data = json.loads(match.group(0))
            cmd = (data.get("command") or "").strip()
            if cmd:
                port_val = data.get("port")
                if not isinstance(port_val, int) or port_val <= 0 or port_val > 65535:
                    port_val = heuristic_plan.get("default_port", 5000)

                return {
                    "project_type": data.get("project_type") or heuristic_plan.get("project_type"),
                    "command": cmd,
                    "install_command": data.get("install_command") or heuristic_plan.get("install_command", ""),
                    "default_port": port_val,
                    "detected_from": "AI Architecture Analysis (LLM)",
                    "readme_summary": data.get("explanation") or f"Synthesized from {readme_file_used or 'codebase files'} via AI model."
                }
    except Exception as e:
        logger.debug(f"AI launch plan analysis skipped: {e}")

    return None


def discover_project_launch_plan(repo_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Deeply scans the repository to discover how to launch and execute the application.
    Combines:
    1. Multi-language manifest and directory tree inspection (C#, Node, Python, Java, Go, Rust, etc.).
    2. README setup/run instructions extraction.
    3. AI Model analysis via LLM to synthesize the exact launch parameters.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    if clean_repo:
        repo_dir = ALLOWED_REPO_DIR / clean_repo
    elif ALLOWED_REPO_DIR.exists():
        subdirs = [p for p in ALLOWED_REPO_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")]
        repo_dir = subdirs[0] if subdirs else ALLOWED_REPO_DIR
    else:
        repo_dir = ALLOWED_REPO_DIR

    if not repo_dir.exists():
        return {
            "repo_name": clean_repo or "unknown",
            "project_type": "Unknown",
            "command": "python -m http.server 9100",
            "install_command": "",
            "default_port": 9100,
            "assigned_port": 9100,
            "working_dir": str(repo_dir),
            "confidence": "low",
            "detected_from": "default",
            "readme_file": "None",
            "readme_summary": "Repository directory not found on disk."
        }

    # 1. Read README files
    readme_candidates = [
        repo_dir / "README.md",
        repo_dir / "readme.md",
        repo_dir / "README",
        repo_dir / "SETUP.md",
        repo_dir / "CONTRIBUTING.md"
    ]
    readme_content = ""
    readme_file_used = ""
    for r_file in readme_candidates:
        if r_file.exists():
            readme_content = _read_snippet_safe(r_file, max_bytes=24000)
            readme_file_used = r_file.name
            break

    # 2. Collect Directory Tree
    file_tree = _collect_repo_file_tree(repo_dir)

    # 3. Comprehensive Multi-Language Heuristics
    plan = detect_heuristic_launch_plan(repo_dir, file_tree, readme_content, readme_file_used)

    # 4. AI-Enhanced Model Analysis
    ai_plan = analyze_launch_with_llm(
        repo_name=clean_repo or repo_dir.name,
        file_tree=file_tree,
        readme_content=readme_content,
        readme_file_used=readme_file_used,
        heuristic_plan=plan
    )

    if ai_plan:
        plan.update(ai_plan)

    assigned_port = find_available_port(start_port=9100)

    readme_summary = plan.get("readme_summary")
    if not readme_summary:
        if readme_file_used:
            readme_summary = f"Discovered launch configuration from {readme_file_used} and project files."
        else:
            readme_summary = f"Discovered launch configuration for {plan['project_type']} project."

    return {
        "repo_name": clean_repo or repo_dir.name,
        "project_type": plan["project_type"],
        "command": plan["command"],
        "install_command": plan.get("install_command", ""),
        "default_port": plan["default_port"],
        "assigned_port": assigned_port,
        "working_dir": str(repo_dir),
        "detected_from": plan["detected_from"],
        "readme_file": readme_file_used or "None",
        "readme_summary": readme_summary
    }


def _is_host_proxy_reachable(host: str = "host.docker.internal", port: int = 10808, timeout: float = 1.0) -> bool:
    """Checks if the host or local proxy is reachable."""
    for target in [host, "127.0.0.1", "localhost"]:
        try:
            with socket.create_connection((target, port), timeout=timeout):
                return True
        except Exception:
            continue
    return False


def ensure_runtime_prerequisites(cmd_str: str, session: "SandboxSession", env: Dict[str, str]) -> bool:
    """
    Verifies that the required CLI runtime for the command exists in the sandbox environment.
    If missing, attempts auto-provisioning or emits clear diagnostic guidance into session logs.
    """
    clean_cmd = cmd_str.strip()
    try:
        tokens = shlex.split(clean_cmd)
    except Exception:
        tokens = clean_cmd.split()
    if not tokens:
        return True

    idx = 0
    while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("./") and not tokens[idx].startswith("/"):
        idx += 1
    binary = tokens[idx] if idx < len(tokens) else tokens[0]
    binary_name = Path(binary).name.lower()

    # 1. Handle C# / .NET
    if binary_name in ("dotnet", "dotnet.exe"):
        dotnet_candidates = [
            shutil.which("dotnet"),
            "/usr/local/bin/dotnet",
            "/usr/share/dotnet/dotnet",
            str(Path.home() / ".dotnet" / "dotnet"),
            "/root/.dotnet/dotnet"
        ]
        found_dotnet = next((p for p in dotnet_candidates if p and Path(p).exists()), None)
        if found_dotnet:
            dotnet_dir = str(Path(found_dotnet).parent)
            current_path = env.get("PATH", os.environ.get("PATH", ""))
            if dotnet_dir not in current_path.split(os.pathsep):
                env["PATH"] = f"{dotnet_dir}{os.pathsep}{current_path}"
            env["DOTNET_ROOT"] = dotnet_dir
            return True

        session.log("⚠️ .NET SDK ('dotnet') is not found in the sandbox environment.")
        session.log("⏳ Attempting automatic on-demand installation of .NET 8 SDK... please wait.")

        try:
            proxy_args = []
            install_env = dict(os.environ)
            if _is_host_proxy_reachable("host.docker.internal", 10808):
                proxy_url = "http://host.docker.internal:10808"
                proxy_args = ["-x", proxy_url]
                install_env["http_proxy"] = proxy_url
                install_env["https_proxy"] = proxy_url
                install_env["HTTP_PROXY"] = proxy_url
                install_env["HTTPS_PROXY"] = proxy_url

            script_path = Path("/tmp/dotnet-install.sh")
            if not script_path.exists():
                session.log("📥 Downloading official .NET install script...")
                dl_cmd = ["curl", "-sSL"] + proxy_args + ["https://dot.net/v1/dotnet-install.sh", "-o", str(script_path)]
                subprocess.run(dl_cmd, check=True, timeout=30, capture_output=True)

            target_install_dir = Path.home() / ".dotnet"
            target_install_dir.mkdir(parents=True, exist_ok=True)
            session.log(f"📦 Installing .NET 8 SDK into {target_install_dir}...")
            install_cmd = ["bash", str(script_path), "--channel", "8.0", "--install-dir", str(target_install_dir)]
            proc = subprocess.run(install_cmd, env=install_env, timeout=300, capture_output=True, text=True)

            installed_dotnet = target_install_dir / "dotnet"
            if installed_dotnet.exists():
                session.log("✅ .NET 8 SDK installed successfully into sandbox!")
                dotnet_dir = str(target_install_dir)
                current_path = env.get("PATH", os.environ.get("PATH", ""))
                env["PATH"] = f"{dotnet_dir}{os.pathsep}{current_path}"
                env["DOTNET_ROOT"] = dotnet_dir
                return True
            else:
                err_preview = (proc.stderr or proc.stdout or "Unknown installer error")[:250]
                session.log(f"❌ .NET auto-installation failed: {err_preview}")
        except Exception as ex:
            session.log(f"❌ Failed to auto-install .NET SDK: {str(ex)}")

        session.log("💡 Tip: To run C# projects, install .NET 8 SDK inside the container or rebuild the image.")
        return False

    # 2. Handle Node.js / npm
    if binary_name in ("node", "npm", "npx", "yarn", "pnpm"):
        node_candidates = [
            shutil.which(binary_name),
            f"/usr/local/bin/{binary_name}",
            f"/usr/bin/{binary_name}"
        ]
        found_node = next((p for p in node_candidates if p and Path(p).exists()), None)
        if found_node:
            node_dir = str(Path(found_node).parent)
            current_path = env.get("PATH", os.environ.get("PATH", ""))
            if node_dir not in current_path.split(os.pathsep):
                env["PATH"] = f"{node_dir}{os.pathsep}{current_path}"
            return True

        session.log(f"⚠️ Node.js runtime ('{binary_name}') was not found in the sandbox environment.")
        session.log("💡 Tip: Install Node.js & npm inside the container to execute JavaScript/TypeScript applications.")
        return False

    # 3. Handle Python
    if binary_name in ("python", "python3", "uvicorn", "gunicorn"):
        return True

    # 4. Handle other generic commands
    if shutil.which(binary_name) is None and not Path(binary_name).exists():
        session.log(f"⚠️ Required executable '{binary_name}' was not found in system PATH.")
        session.log("💡 If this command requires specific packages, please ensure they are installed in the container.")
        return False

    return True


class SandboxSession:
    """Manages an isolated execution instance for a target codebase."""

    def __init__(self, repo_name: str, command: str, port: int, working_dir: Path):
        self.repo_name = repo_name
        self.command = command
        self.port = port
        self.working_dir = working_dir
        self.process: Optional[subprocess.Popen] = None
        self.logs: deque = deque(maxlen=2000)
        self.status: str = "starting"
        self.started_at: Optional[datetime] = None
        self.stopped_at: Optional[datetime] = None
        self.exit_code: Optional[int] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_requested = False

    def log(self, text: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {text}")

    def start(self):
        """Spawns the sandbox process with port and environment isolation."""
        self._stop_requested = False
        self.started_at = datetime.now()
        self.status = "starting"
        self.log(f"🚀 Initializing isolated sandbox for '{self.repo_name}' on port {self.port}...")
        self.log(f"📁 Working directory: {self.working_dir}")

        # Substitute {PORT} placeholder in command
        cmd_str = self.command.replace("{PORT}", str(self.port)).replace("{port}", str(self.port))
        self.log(f"💻 Execution command: {cmd_str}")

        # Build isolated environment variables
        env = dict(os.environ)
        env["PORT"] = str(self.port)
        env["HOST"] = "0.0.0.0"
        env["ASPNETCORE_URLS"] = f"http://0.0.0.0:{self.port}"
        env["PYTHONUNBUFFERED"] = "1"
        env["CI"] = "true"
        # Avoid loopback proxy interference in sandbox
        env["NO_PROXY"] = "localhost,127.0.0.1,host.docker.internal"
        env["no_proxy"] = "localhost,127.0.0.1,host.docker.internal"

        kwargs: Dict[str, Any] = {
            "cwd": str(self.working_dir),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
        }

        # Check language runtime prerequisites (e.g. dotnet, node, python)
        prereq_ok = ensure_runtime_prerequisites(cmd_str, self, env)
        if not prereq_ok:
            self.status = "crashed"
            self.exit_code = 127
            self.log("🛑 Launch halted: Required language runtime is missing in the sandbox environment.")
            return

        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["preexec_fn"] = os.setsid

        try:
            self.process = subprocess.Popen(cmd_str, shell=True, **kwargs)
            self.status = "running"
            self.log(f"✅ Sandbox process started (PID: {self.process.pid}). URL: http://localhost:{self.port}")

            def _stream_logs():
                try:
                    for line in iter(self.process.stdout.readline, ""):
                        if line:
                            self.logs.append(line.rstrip())
                    self.process.stdout.close()
                except Exception as ex:
                    self.log(f"⚠️ Log stream closed: {ex}")
                finally:
                    self.process.poll()
                    self.exit_code = self.process.returncode
                    if not self._stop_requested:
                        self.status = "stopped" if self.exit_code == 0 else "crashed"
                        self.log(f"🛑 Sandbox process exited with code {self.exit_code}")
                    else:
                        self.status = "stopped"

            self._reader_thread = threading.Thread(target=_stream_logs, daemon=True)
            self._reader_thread.start()

        except Exception as e:
            self.status = "crashed"
            self.exit_code = -1
            self.log(f"❌ Failed to launch sandbox process: {str(e)}")
            raise

    def stop(self):
        """Gracefully terminates the sandbox process and its children."""
        self._stop_requested = True
        self.status = "stopped"
        self.stopped_at = datetime.now()
        self.log("🛑 Termination requested. Shutting down sandbox process...")

        if not self.process:
            return

        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False
                )
            else:
                import signal
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                time.sleep(0.5)
                if self.process.poll() is None:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        except Exception as e:
            logger.debug(f"Error terminating sandbox process: {e}")
            try:
                self.process.kill()
            except Exception:
                pass

        self.log("Sandbox process terminated.")

    def to_dict(self) -> Dict[str, Any]:
        """Serializes current sandbox status and latest logs."""
        uptime = 0
        if self.started_at:
            end_t = self.stopped_at or datetime.now()
            uptime = int((end_t - self.started_at).total_seconds())

        return {
            "repo_name": self.repo_name,
            "status": self.status,
            "port": self.port,
            "url": f"http://localhost:{self.port}",
            "command": self.command,
            "pid": self.process.pid if self.process else None,
            "uptime_seconds": uptime,
            "exit_code": self.exit_code,
            "logs": list(self.logs)[-200:]
        }


def start_sandbox_session(repo_name: Optional[str] = None, custom_command: Optional[str] = None, custom_port: Optional[int] = None) -> Dict[str, Any]:
    """Starts or restarts a sandbox session for the given repository."""
    clean_repo = sanitize_name(repo_name) if repo_name else None
    plan = discover_project_launch_plan(clean_repo)

    cmd = (custom_command or "").strip() or plan["command"]
    port = custom_port or plan["assigned_port"]
    working_dir = Path(plan["working_dir"])

    with _SANDBOX_LOCK:
        key = clean_repo or "default"
        existing = _ACTIVE_SANDBOXES.get(key)
        if existing and existing.status in ("starting", "running"):
            existing.stop()

        session = SandboxSession(
            repo_name=key,
            command=cmd,
            port=port,
            working_dir=working_dir
        )
        session.start()
        _ACTIVE_SANDBOXES[key] = session
        return session.to_dict()


def stop_sandbox_session(repo_name: Optional[str] = None) -> Dict[str, Any]:
    """Stops the active sandbox session for the given repository."""
    clean_repo = sanitize_name(repo_name) if repo_name else None
    key = clean_repo or "default"

    with _SANDBOX_LOCK:
        session = _ACTIVE_SANDBOXES.get(key)
        if not session:
            return {"repo_name": key, "status": "stopped", "message": "No active sandbox running for this repository."}

        session.stop()
        return session.to_dict()


def get_sandbox_session_status(repo_name: Optional[str] = None) -> Dict[str, Any]:
    """Returns the current status, port, and live logs of the sandbox session."""
    clean_repo = sanitize_name(repo_name) if repo_name else None
    key = clean_repo or "default"

    with _SANDBOX_LOCK:
        session = _ACTIVE_SANDBOXES.get(key)
        if not session:
            plan = discover_project_launch_plan(clean_repo)
            return {
                "repo_name": key,
                "status": "stopped",
                "port": plan["assigned_port"],
                "url": f"http://localhost:{plan['assigned_port']}",
                "command": plan["command"],
                "pid": None,
                "uptime_seconds": 0,
                "exit_code": None,
                "logs": [f"Sandbox ready. Detected launch command: {plan['command']}"],
                "plan": plan
            }
        return session.to_dict()
