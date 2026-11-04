import os
import re
import shutil
import zipfile
import subprocess
from pathlib import Path
from typing import Optional
from fastapi import HTTPException
from urllib.parse import urlparse, urlunparse

from app.core.security import ALLOWED_REPO_DIR, sanitize_name

MAX_REPO_SIZE_MB = int(os.getenv("MAX_REPO_SIZE_MB", "50"))
MAX_REPO_FILE_COUNT = int(os.getenv("MAX_REPO_FILE_COUNT", "1000"))

def verify_repo_limits(target_dir: Path) -> None:
    """Enforces repository size cap (50MB) and file count cap (1000 files) per Item 7."""
    total_size = 0
    file_count = 0
    max_bytes = MAX_REPO_SIZE_MB * 1024 * 1024

    for root, dirs, files in os.walk(target_dir):
        for f in files:
            fp = Path(root) / f
            try:
                size = fp.stat().st_size
                total_size += size
                if ".git" not in root.split(os.sep):
                    file_count += 1
            except OSError:
                continue

            if total_size > max_bytes:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"Repository exceeds maximum allowed size ({MAX_REPO_SIZE_MB}MB). Operation aborted for safety."
                )
            if file_count > MAX_REPO_FILE_COUNT:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"Repository exceeds maximum allowed file count ({MAX_REPO_FILE_COUNT} files). Operation aborted for safety."
                )


def sanitize_git_url(url: str, token: Optional[str] = None) -> str:
    """
    Validates and formats Git URL. Optionally injects auth token for private repos.
    """
    url = url.strip()
    if not (url.startswith("https://") or url.startswith("http://") or url.startswith("git@")):
        raise HTTPException(status_code=400, detail="Invalid Git URL. Must start with https://, http://, or git@")

    # Prevent command injection in arguments
    if any(char in url for char in [";", "&", "|", "`", "$", "\n", "\r", " "]):
        raise HTTPException(status_code=400, detail="Disallowed characters in Git URL.")

    if token and url.startswith("https://"):
        parsed = urlparse(url)
        # Inject token into URL e.g. https://oauth2:TOKEN@github.com/...
        netloc = f"oauth2:{token}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        url = urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

    return url

def infer_repo_name_from_url(url: str) -> str:
    """Extracts default repository name from Git URL."""
    clean_url = url.split("?")[0].rstrip("/")
    if clean_url.endswith(".git"):
        clean_url = clean_url[:-4]
    name = clean_url.split("/")[-1].split(":")[-1]
    return sanitize_name(name) if name else "ClonedRepo"

def clone_git_repo(
    git_url: str,
    repo_name: Optional[str] = None,
    branch: Optional[str] = None,
    token: Optional[str] = None,
    full_history: bool = True,
) -> Path:
    """
    Clones a remote Git repository into the sandboxed repos directory.
    Uses subprocess with direct argument list (no shell injection).

    full_history: by default clone the COMPLETE history. The Git Hotspots / churn
    analysis (`git rev-list --count`) needs the real commit history to be meaningful;
    a shallow clone (`--depth 1`) makes every file report churn=1 and breaks hotspots.
    Set full_history=False only for very large repos where analysis is not required.
    """
    safe_url = sanitize_git_url(git_url, token=token)
    if not repo_name:
        repo_name = infer_repo_name_from_url(git_url)
    clean_name = sanitize_name(repo_name)

    target_dir = ALLOWED_REPO_DIR / clean_name
    try:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        # If repo already exists, remove it for a clean fresh clone
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
    except PermissionError as pe:
        raise HTTPException(
            status_code=500,
            detail=f"Permission denied on repos directory ({ALLOWED_REPO_DIR}): {pe}. Run 'chmod -R 777 ./repos' on the host server."
        )

    cmd = ["git", "-c", "core.quotepath=false", "clone"]
    if not full_history:
        cmd.extend(["--depth", "1"])
    if branch:
        clean_branch = re.sub(r"[^a-zA-Z0-9_\-\./]", "", branch)
        cmd.extend(["--branch", clean_branch])
    
    cmd.extend([safe_url, str(target_dir)])

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1200 if full_history else 180, # full history can be large; allow 20 min
            check=True
        )
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.replace(token, "***") if token else e.stderr
        if "permission denied" in err_msg.lower():
            err_msg += " -> Suggestion: Run 'chmod -R 777 ./repos' on your host machine to allow Docker write access."
        raise HTTPException(status_code=500, detail=f"Git clone failed: {err_msg}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Git clone timed out after 3 minutes.")

    verify_repo_limits(target_dir)
    return target_dir

def decode_zip_filename(raw_name: str, flag_bits: int) -> str:
    """
    Recovers proper UTF-8 or Windows CP1256 (Persian/Arabic) filenames from ZIP archives.
    When bit 11 (0x800) is unset, standard ZIP decodes raw bytes using CP437.
    """
    if flag_bits & 0x800:
        return raw_name

    try:
        raw_bytes = raw_name.encode("cp437")
    except UnicodeEncodeError:
        return raw_name

    for enc in ("utf-8", "cp1256", "utf-8-sig", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue

    return raw_name

def extract_safe_zip(zip_path: Path, repo_name: str) -> Path:
    """
    Safely extracts a ZIP archive into the sandboxed repository folder,
    preserving non-ASCII and Persian filenames and protecting against Zip Slip.
    """
    clean_name = sanitize_name(repo_name)
    target_dir = ALLOWED_REPO_DIR / clean_name
    try:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        target_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as pe:
        raise HTTPException(
            status_code=500,
            detail=f"Permission denied on repos directory ({ALLOWED_REPO_DIR}): {pe}. Run 'chmod -R 777 ./repos' on the host server."
        )

    target_resolved = target_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            decoded_filename = decode_zip_filename(member.filename, member.flag_bits)
            # Normalize path separators (handle Windows backslashes)
            norm_path = decoded_filename.replace("\\", "/").strip("/")
            if not norm_path or ".." in norm_path.split("/"):
                continue

            extracted_path = (target_dir / norm_path).resolve()
            # Zip Slip check: ensure extracted path does not escape sandbox
            if not str(extracted_path).startswith(str(target_resolved)):
                raise HTTPException(status_code=400, detail="Malicious ZIP detected (Zip Slip vulnerability).")

            if member.is_dir() or decoded_filename.endswith("/"):
                extracted_path.mkdir(parents=True, exist_ok=True)
            else:
                extracted_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src_file, open(extracted_path, "wb") as dst_file:
                    shutil.copyfileobj(src_file, dst_file)

    verify_repo_limits(target_dir)
    return target_dir
