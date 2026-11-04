"""Isolated Docker runner for untrusted, user-supplied repositories.

This service is intentionally separate from the public API. It is the only
application container allowed to access the Docker socket.
"""
import io
import os
import re
import tarfile
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import docker
import requests
from docker.errors import DockerException, ImageNotFound, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import Response, StreamingResponse


app = FastAPI(title="Preflight Sandbox Runner", docs_url=None, redoc_url=None)
client = docker.from_env()
def _resolve_repos_dir() -> Path:
    env_dir = os.getenv("ALLOWED_REPO_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    container_dir = Path("/app/repos")
    if container_dir.exists():
        return container_dir.resolve()
    file_path = Path(__file__).resolve()
    for parent in [file_path.parent.parent.parent, file_path.parent.parent, Path.cwd()]:
        candidate = parent / "repos"
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()
    return container_dir.resolve()

REPOS_DIR = _resolve_repos_dir()
RUNNER_TOKEN = os.getenv("SANDBOX_RUNNER_TOKEN", "")
DOCKER_NETWORK = os.getenv("SANDBOX_DOCKER_NETWORK", "preflight_sandbox_net")
MEMORY_LIMIT = os.getenv("SANDBOX_MEMORY_LIMIT", "256m")
CPU_LIMIT = int(os.getenv("SANDBOX_NANO_CPUS", "500000000"))  # 0.5 CPU
PIDS_LIMIT = int(os.getenv("SANDBOX_PIDS_LIMIT", "64"))
MAX_LIFETIME_SECONDS = int(os.getenv("SANDBOX_MAX_LIFETIME", "120"))
MAX_LOG_LINES = int(os.getenv("SANDBOX_MAX_LOG_LINES", "500"))

RUNTIME_IMAGES = {
    "node": os.getenv("SANDBOX_NODE_IMAGE", "node:20-bookworm-slim"),
    "python": os.getenv("SANDBOX_PYTHON_IMAGE", "python:3.11-slim"),
    "dotnet": os.getenv("SANDBOX_DOTNET_IMAGE", "mcr.microsoft.com/dotnet/sdk:8.0"),
    "maven": os.getenv("SANDBOX_MAVEN_IMAGE", "maven:3.9-eclipse-temurin-21"),
    "gradle": os.getenv("SANDBOX_GRADLE_IMAGE", "gradle:8.10-jdk21"),
    "go": os.getenv("SANDBOX_GO_IMAGE", "golang:1.23-bookworm"),
    "rust": os.getenv("SANDBOX_RUST_IMAGE", "rust:1.83-bookworm"),
    "php": os.getenv("SANDBOX_PHP_IMAGE", "composer:2"),
    "static": os.getenv("SANDBOX_STATIC_IMAGE", "python:3.11-slim"),
}


class StartRequest(BaseModel):
    repo_name: str = Field(min_length=1, max_length=120)
    project_type: str = "Generic"
    command: str = Field(min_length=1, max_length=4096)
    install_command: str = Field(default="", max_length=4096)
    port: int = Field(default=8000, ge=1024, le=65535)


class StopRequest(BaseModel):
    repo_name: str


class Session:
    def __init__(self, name: str, container: Any, volume: Any, port: int, command: str):
        self.name = name
        self.container = container
        self.volume = volume
        self.port = port
        self.command = command
        self.started_at = datetime.now(timezone.utc)
        self.logs = deque(maxlen=MAX_LOG_LINES)
        self.reader = threading.Thread(target=self._read_logs, daemon=True)
        self.reader.start()

    def _read_logs(self):
        try:
            for raw in self.container.logs(stream=True, follow=True):
                self.logs.append(raw.decode("utf-8", errors="replace").rstrip())
        except Exception as exc:
            self.logs.append(f"Log stream ended: {exc}")

    def status(self) -> Dict[str, Any]:
        self.container.reload()
        state = self.container.attrs.get("State", {})
        docker_status = state.get("Status", "unknown")
        mapped = {"created": "starting", "running": "running", "exited": "stopped"}.get(docker_status, docker_status)
        if docker_status == "exited" and state.get("ExitCode", 0) != 0:
            mapped = "crashed"
        host_config = self.container.attrs.get("HostConfig", {})
        config = self.container.attrs.get("Config", {})
        applied_containment = {
            "readonly_rootfs": host_config.get("ReadonlyRootfs", False),
            "memory": host_config.get("Memory", 0),
            "memory_human": MEMORY_LIMIT,
            "nano_cpus": host_config.get("NanoCpus", 0),
            "cpus_human": "0.5 CPU",
            "pids_limit": host_config.get("PidsLimit", 0),
            "cap_drop": host_config.get("CapDrop", []),
            "security_opt": host_config.get("SecurityOpt", []),
            "network_mode": host_config.get("NetworkMode", "unknown"),
            "user": config.get("User", "root"),
            "all_enforced": bool(
                host_config.get("ReadonlyRootfs")
                and host_config.get("CapDrop")
                and host_config.get("PidsLimit") == PIDS_LIMIT
            )
        }
        return {
            "repo_name": self.name,
            "status": mapped,
            "port": self.port,
            "url": f"http://{self.container.name}:{self.port}",
            "command": self.command,
            "pid": state.get("Pid"),
            "uptime_seconds": int((datetime.now(timezone.utc) - self.started_at).total_seconds()),
            "exit_code": state.get("ExitCode") if docker_status == "exited" else None,
            "stopped_reason": getattr(self, "stopped_reason", None),
            "applied_containment": applied_containment,
            "logs": list(self.logs),
        }


sessions: Dict[str, Session] = {}
lock = threading.RLock()


def authorize(x_sandbox_token: Optional[str] = Header(default=None)):
    if RUNNER_TOKEN and x_sandbox_token != RUNNER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid sandbox runner token")


def clean_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value).strip("._")
    if not cleaned or cleaned != value or ".." in cleaned:
        raise HTTPException(status_code=400, detail="Invalid repository name")
    return cleaned


def runtime_for(project_type: str) -> str:
    value = project_type.lower()
    if "node" in value:
        return "node"
    if ".net" in value or "c#" in value:
        return "dotnet"
    if "maven" in value:
        return "maven"
    if "java" in value or "kotlin" in value or "gradle" in value:
        return "gradle"
    if "rust" in value:
        return "rust"
    if value == "go" or value.startswith("go "):
        return "go"
    if "php" in value:
        return "php"
    if "static" in value or "generic" in value:
        return "static"
    return "python"


def source_archive(repo_dir: Path) -> bytes:
    payload = io.BytesIO()
    ignored = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for path in repo_dir.rglob("*"):
            relative = path.relative_to(repo_dir)
            if any(part in ignored for part in relative.parts):
                continue
            archive.add(path, arcname=relative.as_posix(), recursive=False)
    payload.seek(0)
    return payload.read()


def prepare_command(project_type: str, command: str) -> str:
    """Makes common development servers listen on the sandbox interface/port."""
    kind = project_type.lower()
    if "vite" in kind and "--host" not in command:
        return f"{command} -- --host 0.0.0.0 --port $PORT"
    if "next.js" in kind and " -p " not in command and "--port" not in command:
        return f"{command} -- -H 0.0.0.0 -p $PORT"
    if "nuxt" in kind and "--host" not in command:
        return f"{command} -- --host 0.0.0.0 --port $PORT"
    if "flask" in kind and command.strip().startswith("python "):
        return command
    return command


def remove_session(name: str):
    session = sessions.pop(name, None)
    if not session:
        return
    try:
        session.container.remove(force=True)
    except NotFound:
        pass
    finally:
        try:
            session.volume.remove(force=True)
        except (NotFound, DockerException):
            pass


@app.get("/health", dependencies=[Depends(authorize)])
def health():
    try:
        client.ping()
        return {"status": "ok"}
    except DockerException as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/sessions/start", dependencies=[Depends(authorize)])
def start(payload: StartRequest):
    name = clean_name(payload.repo_name)
    repo_dir = (REPOS_DIR / name).resolve()
    if REPOS_DIR not in repo_dir.parents or not repo_dir.is_dir():
        raise HTTPException(status_code=404, detail="Repository directory not found")

    runtime = runtime_for(payload.project_type)
    image = RUNTIME_IMAGES[runtime]
    container_name = f"preflight-sandbox-{name.lower()}"
    volume_name = f"preflight-sandbox-{name.lower()}-workspace"
    run_command = prepare_command(payload.project_type, payload.command)
    shell_command = run_command
    if payload.install_command:
        shell_command = f"{payload.install_command} && exec {run_command}"

    with lock:
        remove_session(name)
        try:
            try:
                client.images.get(image)
            except ImageNotFound:
                client.images.pull(image)
            try:
                old = client.containers.get(container_name)
                old.remove(force=True)
            except NotFound:
                pass
            try:
                old_volume = client.volumes.get(volume_name)
                old_volume.remove(force=True)
            except NotFound:
                pass

            volume = client.volumes.create(name=volume_name, labels={"preflight.sandbox": "true"})
            container = client.containers.create(
                image=image,
                name=container_name,
                command=["sh", "-lc", shell_command],
                working_dir="/workspace",
                read_only=True,
                user="1000:1000",
                environment={
                    "PORT": str(payload.port), "HOST": "0.0.0.0",
                    "ASPNETCORE_URLS": f"http://0.0.0.0:{payload.port}",
                    "SERVER_PORT": str(payload.port),
                    "FLASK_RUN_HOST": "0.0.0.0", "FLASK_RUN_PORT": str(payload.port),
                    "CI": "true", "PYTHONUNBUFFERED": "1",
                },
                volumes={volume.name: {"bind": "/workspace", "mode": "rw"}},
                network=DOCKER_NETWORK,
                mem_limit=MEMORY_LIMIT,
                nano_cpus=CPU_LIMIT,
                pids_limit=PIDS_LIMIT,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                tmpfs={
                    "/tmp": "rw,noexec,nosuid,size=128m",
                    "/root/.cache": "rw,size=64m",
                    "/home/node/.npm": "rw,size=64m"
                },
                labels={"preflight.sandbox": "true", "preflight.repo": name},
                detach=True,
            )
            container.put_archive("/workspace", source_archive(repo_dir))
            container.start()
            session = Session(name, container, volume, payload.port, run_command)
            sessions[name] = session
            result = session.status()
            result["runtime"] = runtime
            result["image"] = image
            return result
        except DockerException as exc:
            remove_session(name)
            raise HTTPException(status_code=502, detail=f"Docker runner error: {exc}")


@app.post("/sessions/stop", dependencies=[Depends(authorize)])
def stop(payload: StopRequest):
    name = clean_name(payload.repo_name)
    with lock:
        session = sessions.get(name)
        previous = session.status() if session else {"repo_name": name}
        remove_session(name)
    return {**previous, "status": "stopped", "pid": None}


@app.get("/sessions/{repo_name}/status", dependencies=[Depends(authorize)])
def status(repo_name: str):
    name = clean_name(repo_name)
    session = sessions.get(name)
    if not session:
        return {"repo_name": name, "status": "stopped", "logs": [], "pid": None, "exit_code": None}
    return session.status()


@app.api_route(
    "/sessions/{repo_name}/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    dependencies=[Depends(authorize)],
)
async def proxy(repo_name: str, path: str, request: Request):
    name = clean_name(repo_name)
    session = sessions.get(name)
    if not session:
        raise HTTPException(status_code=503, detail="Sandbox is not running")
    target = f"http://{session.container.name}:{session.port}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "connection", "content-length", "x-sandbox-token"}}
    headers["Host"] = f"localhost:{session.port}"
    body = await request.body()
    try:
        upstream = requests.request(request.method, target, headers=headers, data=body, stream=True, allow_redirects=False, timeout=30)
    except requests.RequestException as exc:
        return Response(content=f"Sandbox application unavailable: {exc}", status_code=502)
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in {"content-encoding", "content-length", "transfer-encoding", "connection"}}
    return StreamingResponse(upstream.iter_content(32768), status_code=upstream.status_code, headers=response_headers, media_type=upstream.headers.get("content-type"))


def _watchdog_loop():
    import time
    while True:
        try:
            time.sleep(3)
            now = datetime.now(timezone.utc)
            to_reap = []
            with lock:
                for name, sess in list(sessions.items()):
                    age = (now - sess.started_at).total_seconds()
                    if age > MAX_LIFETIME_SECONDS:
                        to_reap.append(name)
                for name in to_reap:
                    sess = sessions.get(name)
                    if sess:
                        sess.stopped_reason = "watchdog"
                        remove_session(name)
        except Exception:
            pass

_watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True, name="sandbox_watchdog")
_watchdog_thread.start()
