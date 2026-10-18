"""
Single-pass git history (author + churn) for an entire repository.

D6b fix: the scanner previously spawned two `git` subprocesses (log + rev-list)
per file, i.e. 2N processes for N files. This module runs ONE `git log` over the
whole tree with a NUL-delimited author marker and collects, in a single pass, the
last author/email and the commit count (churn) for every file. The parsing is a
pure function so it can be unit-tested without git.
"""
import subprocess
from typing import Dict, Tuple, Optional

# Delimiter placed at the start of each commit line by `--format`. It must be a
# printable ASCII token (control bytes such as NUL cannot be passed in a Windows
# command-line argument), and it will not appear in an author name, email, or path.
_MARK = "@@@GITLOG@@@"


def parse_git_log(output: str, mark: str = _MARK) -> Dict[str, Dict[str, object]]:
    """
    Parse `git log --all --name-only --format=%an|%ae|%H<mark>` output.

    Returns {rel_path: {"author": str, "email": str, "churn": int}}. For each file
    the LAST (most recent) author seen wins, and churn counts every commit that
    touched the file (git prints files in newest-first order).
    """
    result: Dict[str, Dict[str, object]] = {}
    current_author: Optional[Tuple[str, str]] = None
    # Tracks which files were touched by the commit currently being read, so we
    # increment churn exactly once per commit even if a path repeats in one commit.
    commit_files: set = set()

    lines = output.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\n")
        if line.startswith(mark):
            # New commit header.
            payload = line[len(mark):].strip()
            if "|" in payload:
                an, ae = payload.split("|", 1)
                current_author = (an, ae)
            # Reset per-commit file tracking.
            commit_files.clear()
            i += 1
            continue
        if line == "":
            # Blank line ends a file list (in case of a trailing blank).
            i += 1
            continue
        # Otherwise it's a file path belonging to the current commit.
        path = line.strip()
        if path and current_author is not None:
            entry = result.get(path)
            if entry is None:
                entry = {"author": current_author[0], "email": current_author[1], "churn": 0}
                result[path] = entry
            # Most recent commit appears first -> only overwrite author on first sight.
            if path not in commit_files:
                entry["churn"] = entry["churn"] + 1
                commit_files.add(path)
                # Keep the FIRST (newest) author we saw for this file.
                # (We overwrite only when creating the entry above, since the log
                # is ordered newest-first.)
        i += 1

    return result


def get_git_history_map(
    repo_path: str,
    timeout: int = 300,
) -> Dict[str, Dict[str, object]]:
    """
    Compute {rel_path: {author, email, churn}} for a repository in a single git
    invocation. Returns {} if git is unavailable or the directory is not a repo
    (callers degrade gracefully to defaults).
    """
    try:
        res = subprocess.run(
            [
                "git", "-c", "core.quotepath=false",
                "-c", "i18n.logoutputencoding=utf-8",
                "log", "--all", "--name-only",
                "--format=" + _MARK + "%an|%ae",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}
    if res.returncode != 0:
        return {}
    return parse_git_log(res.stdout, _MARK)
