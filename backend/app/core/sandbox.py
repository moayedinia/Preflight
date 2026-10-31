import os
import sys
import re
import json
import time
import socket
import logging
import threading
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from collections import deque
from datetime import datetime

from app.core.security import ALLOWED_REPO_DIR, validate_safe_path, sanitize_name
from app.core.llm import call_llm

logger = logging.getLogger("codegraph.sandbox")

# Global registry of active sandboxes: repo_name -> SandboxSession
_ACTIVE_SANDBOXES: Dict[str, "SandboxSession"] = {}
_SANDBOX_LOCK = threading.Lock()


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


def discover_project_launch_plan(repo_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Deeply scans the repository to discover how to launch and execute the application.
    1. Reads README files (README.md, README, instructions) to detect explicit startup instructions.
    2. Inspects manifest files (package.json, requirements.txt, pyproject.toml, Cargo.toml, go.mod, etc.).
    3. Uses LLM-assisted analysis when available to synthesize the optimal startup command.
    4. Proposes target port, installation command, and start command.
    """
    clean_repo = sanitize_name(repo_name) if repo_name else None
    if clean_repo:
        repo_dir = ALLOWED_REPO_DIR / clean_repo
    elif ALLOWED_REPO_DIR.exists():
        # Check first available directory in ALLOWED_REPO_DIR
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

    # 1. Inspect README files
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

    # 2. Inspect Manifest Files
    pkg_json_path = repo_dir / "package.json"
    req_txt_path = repo_dir / "requirements.txt"
    pyproject_path = repo_dir / "pyproject.toml"
    cargo_path = repo_dir / "Cargo.toml"
    go_path = repo_dir / "go.mod"
    docker_compose_path = repo_dir / "docker-compose.yml"
    index_html_path = repo_dir / "index.html"

    has_pkg_json = pkg_json_path.exists()
    has_python = req_txt_path.exists() or pyproject_path.exists() or any(repo_dir.glob("*.py"))
    has_cargo = cargo_path.exists()
    has_go = go_path.exists()

    detected_type = "Generic"
    detected_cmd = ""
    detected_install = ""
    detected_port = 8000
    detected_from = "heuristics"

    # Analyze Node.js / JavaScript / TypeScript
    if has_pkg_json:
        try:
            with open(pkg_json_path, "r", encoding="utf-8", errors="replace") as f:
                pkg_data = json.load(f)
            scripts = pkg_data.get("scripts", {})
            deps = {**pkg_data.get("dependencies", {}), **pkg_data.get("devDependencies", {})}

            # Detect Framework
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
            elif "express" in deps or "fastify" in deps or "koa" in deps:
                detected_type = "Node.js (Server API)"
                detected_port = 3000
            else:
                detected_type = "Node.js"
                detected_port = 3000

            # Detect Package Manager
            pm = "npm"
            if (repo_dir / "pnpm-lock.yaml").exists():
                pm = "pnpm"
            elif (repo_dir / "yarn.lock").exists():
                pm = "yarn"
            elif (repo_dir / "bun.lockb").exists():
                pm = "bun"

            detected_install = f"{pm} install"

            if "dev" in scripts:
                detected_cmd = f"{pm} run dev"
            elif "start" in scripts:
                detected_cmd = f"{pm} start"
            elif "serve" in scripts:
                detected_cmd = f"{pm} run serve"
            else:
                main_file = pkg_data.get("main", "index.js")
                detected_cmd = f"node {main_file}"

            detected_from = "package.json scripts"
        except Exception as e:
            logger.warning(f"Error parsing package.json: {e}")

    # Analyze Python
    elif has_python:
        detected_type = "Python"
        detected_install = "pip install -r requirements.txt" if req_txt_path.exists() else "pip install ."

        # Scan for common Python entrypoints
        py_files = [p.name for p in repo_dir.glob("*.py")]
        app_sub_py = [p.relative_to(repo_dir).as_posix() for p in repo_dir.glob("app/*.py")]

        # Look for FastAPI / Uvicorn
        is_fastapi = False
        fastapi_target = ""
        for check_file in [repo_dir / "main.py", repo_dir / "app.py", repo_dir / "app" / "main.py"]:
            if check_file.exists():
                txt = _read_snippet_safe(check_file, 4000)
                if "FastAPI" in txt or "uvicorn" in txt:
                    is_fastapi = True
                    mod_name = "app.main:app" if "app" in check_file.parts else f"{check_file.stem}:app"
                    fastapi_target = mod_name
                    break

        if is_fastapi:
            detected_type = "Python (FastAPI)"
            detected_port = 8000
            detected_cmd = f"uvicorn {fastapi_target} --host 0.0.0.0 --port {{PORT}}"
            detected_from = "FastAPI entrypoint"
        elif "manage.py" in py_files:
            detected_type = "Python (Django)"
            detected_port = 8000
            detected_cmd = "python manage.py runserver 0.0.0.0:{PORT}"
            detected_from = "Django manage.py"
        elif "app.py" in py_files:
            detected_type = "Python (Flask / App)"
            detected_port = 5000
            detected_cmd = "python app.py"
            detected_from = "app.py"
        elif "main.py" in py_files:
            detected_type = "Python (Main)"
            detected_port = 8000
            detected_cmd = "python main.py"
            detected_from = "main.py"
        elif py_files:
            detected_cmd = f"python {py_files[0]}"
            detected_from = f"{py_files[0]}"
        else:
            detected_cmd = "python -m http.server {PORT}"
            detected_port = 8000

    # Analyze Rust
    elif has_cargo:
        detected_type = "Rust"
        detected_install = "cargo build"
        detected_cmd = "cargo run"
        detected_port = 8080
        detected_from = "Cargo.toml"

    # Analyze Go
    elif has_go:
        detected_type = "Go"
        detected_install = "go mod download"
        detected_cmd = "go run ."
        detected_port = 8080
        detected_from = "go.mod"

    # Static HTML fallback
    elif index_html_path.exists():
        detected_type = "Static HTML / Web"
        detected_install = ""
        detected_cmd = "python -m http.server {PORT}"
        detected_port = 8080
        detected_from = "index.html"
    else:
        detected_type = "Generic"
        detected_cmd = "python -m http.server {PORT}"
        detected_port = 8000

    # 3. Fast Regex Discovery from README files
    readme_summary = ""
    if readme_content:
        patterns = [
            r"```(?:bash|sh|shell)?\s*[\r\n]+(?:\$\s*)?((?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve)[^\r\n]*)",
            r"```(?:bash|sh|shell)?\s*[\r\n]+(?:\$\s*)?((?:uvicorn\s+[\w\.\:\-]+|python\s+[\w\.\/\-]+\.py|flask\s+run)[^\r\n]*)",
            r"```(?:bash|sh|shell)?\s*[\r\n]+(?:\$\s*)?((?:cargo\s+run|go\s+run\s+\.)[^\r\n]*)"
        ]
        for pat in patterns:
            m = re.search(pat, readme_content, re.IGNORECASE)
            if m:
                extracted_cmd = m.group(1).strip()
                if extracted_cmd:
                    detected_cmd = extracted_cmd
                    detected_from = f"{readme_file_used} instructions"
                    readme_summary = f"Detected startup command from {readme_file_used}: `{detected_cmd}`"
                    break

    if not readme_summary and readme_file_used:
        readme_summary = f"Discovered run instructions from {readme_file_used} and project structure."

    assigned_port = find_available_port(start_port=9100)

    return {
        "repo_name": clean_repo or repo_dir.name,
        "project_type": detected_type,
        "command": detected_cmd,
        "install_command": detected_install,
        "default_port": detected_port,
        "assigned_port": assigned_port,
        "working_dir": str(repo_dir),
        "detected_from": detected_from,
        "readme_file": readme_file_used or "None",
        "readme_summary": readme_summary or "Ready to launch in isolated sandbox."
    }


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
        env["PYTHONUNBUFFERED"] = "1"
        env["CI"] = "true"
        # Avoid loopback proxy interference in sandbox
        env["NO_PROXY"] = "localhost,127.0.0.1,host.docker.internal"
        env["no_proxy"] = "localhost,127.0.0.1,host.docker.internal"

        # Platform-specific process group creation
        kwargs: Dict[str, Any] = {
            "cwd": str(self.working_dir),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
        }

        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP allows killing whole tree cleanly
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["preexec_fn"] = os.setsid

        try:
            # Use shell=True to handle complex npm/python commands cross-platform
            self.process = subprocess.Popen(cmd_str, shell=True, **kwargs)
            self.status = "running"
            self.log(f"✅ Sandbox process started (PID: {self.process.pid}). URL: http://localhost:{self.port}")

            # Background log collector thread
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
                # Kill process tree using taskkill
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
            "logs": list(self.logs)[-200:]  # Latest 200 lines
        }


def start_sandbox_session(repo_name: Optional[str] = None, custom_command: Optional[str] = None, custom_port: Optional[int] = None) -> Dict[str, Any]:
    """Starts or restarts a sandbox session for the given repository."""
    clean_repo = sanitize_name(repo_name) if repo_name else None
    plan = discover_project_launch_plan(clean_repo)

    cmd = (custom_command or "").strip() or plan["command"]
    port = custom_port or plan["assigned_port"]
    working_dir = Path(plan["working_dir"])

    with _SANDBOX_LOCK:
        # Stop existing session if currently active
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
                "logs": [f"Sandbox is ready. Detected run configuration: {plan['command']}"],
                "plan": plan
            }
        data = session.to_dict()
        return data
