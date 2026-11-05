"""
In-process Background Task Manager for Preflight.

Enables asynchronous execution of background jobs (git cloning, indexing,
auto-documentation) without hard dependency on external Redis or Celery workers.
Provides full progress tracking, percentage completion, and status queries
compatible with both the API endpoints and the frontend dashboard.
"""
import os
import time
import uuid
import threading
from typing import Any, Dict, Optional, Callable
from concurrent.futures import ThreadPoolExecutor

_LOCAL_TASKS: Dict[str, Dict[str, Any]] = {}
_LOCAL_TASKS_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="preflight_task")


def record_local_task(
    task_id: str,
    status: str = "PENDING",
    result: Any = None,
    error: Optional[str] = None,
    percent: Optional[int] = None,
    progress: Optional[str] = None,
    repo_name: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Thread-safe update of local task state and progress."""
    with _LOCAL_TASKS_LOCK:
        prev = _LOCAL_TASKS.get(task_id, {})
        merged_meta = dict(prev.get("meta") or {})
        if meta and isinstance(meta, dict):
            merged_meta.update(meta)

        p = percent
        if p is None and meta and isinstance(meta, dict):
            p = meta.get("percent")
        if p is None:
            p = prev.get("percent", 100 if status == "SUCCESS" else 0)

        prog = progress
        if prog is None and meta and isinstance(meta, dict):
            prog = meta.get("status") or meta.get("progress")
        if prog is None:
            prog = prev.get("progress", "")

        rn = repo_name
        if rn is None and meta and isinstance(meta, dict):
            rn = meta.get("repo_name")
        if rn is None:
            rn = prev.get("repo_name")

        res = result if result is not None else prev.get("result")
        err = error if error is not None else prev.get("error")

        _LOCAL_TASKS[task_id] = {
            "task_id": task_id,
            "status": status,
            "state": status,
            "result": res,
            "error": err,
            "percent": p,
            "progress": prog,
            "repo_name": rn,
            "meta": merged_meta,
            "updated_at": time.time(),
        }


def get_local_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve in-memory task dictionary if it exists."""
    with _LOCAL_TASKS_LOCK:
        task = _LOCAL_TASKS.get(task_id)
        if task:
            return dict(task)
        return None


class LocalTaskContext:
    """Mock Celery task context passed as `self` when `bind=True` is used."""
    def __init__(self, task_id: str, repo_name: Optional[str] = None):
        self.task_id = task_id
        self.repo_name = repo_name
        self.request = type("Req", (), {"id": task_id})()

    def update_state(self, state: str = "PROGRESS", meta: Optional[Dict[str, Any]] = None) -> None:
        record_local_task(
            task_id=self.task_id,
            status=state,
            meta=meta or {}
        )


class LocalAsyncResult:
    """Drop-in replacement for Celery's AsyncResult."""
    def __init__(self, task_id: str):
        self.id = task_id
        self.task_id = task_id

    @property
    def status(self) -> str:
        t = get_local_task(self.id)
        return t["status"] if t else "PENDING"

    @property
    def state(self) -> str:
        return self.status

    @property
    def result(self) -> Any:
        t = get_local_task(self.id)
        return t.get("result") if t else None

    @property
    def info(self) -> Dict[str, Any]:
        t = get_local_task(self.id)
        return t.get("meta") or {} if t else {}

    def ready(self) -> bool:
        return self.status in ("SUCCESS", "FAILURE", "REVOKED", "STOPPED")

    def successful(self) -> bool:
        return self.status == "SUCCESS"

    def failed(self) -> bool:
        return self.status == "FAILURE"


def submit_local_task(
    fn: Callable,
    *args,
    bind: bool = False,
    task_id: Optional[str] = None,
    **kwargs
) -> LocalAsyncResult:
    """
    Submits a task for asynchronous execution in the local thread pool.
    Updates task state through PENDING -> PROGRESS -> SUCCESS / FAILURE.
    """
    tid = task_id or f"task_{uuid.uuid4().hex[:12]}"

    # Extract initial repository name if available in arguments
    initial_repo = kwargs.get("repo_name")
    if not initial_repo and len(args) > 1 and isinstance(args[1], str):
        initial_repo = args[1]

    record_local_task(
        task_id=tid,
        status="PENDING",
        percent=0,
        progress="Task queued for processing...",
        repo_name=initial_repo
    )

    ctx = LocalTaskContext(tid, repo_name=initial_repo)

    def _worker():
        try:
            record_local_task(
                task_id=tid,
                status="PROGRESS",
                percent=5,
                progress="Processing task...",
                repo_name=initial_repo
            )
            if bind:
                res = fn(ctx, *args, **kwargs)
            else:
                res = fn(*args, **kwargs)

            final_repo = initial_repo
            if isinstance(res, dict) and res.get("repo_name"):
                final_repo = res["repo_name"]

            record_local_task(
                task_id=tid,
                status="SUCCESS",
                percent=100,
                progress="Completed successfully.",
                result=res,
                repo_name=final_repo
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            record_local_task(
                task_id=tid,
                status="FAILURE",
                percent=100,
                progress=f"Failed: {e}",
                error=str(e),
                repo_name=initial_repo
            )

    _EXECUTOR.submit(_worker)
    return LocalAsyncResult(tid)
