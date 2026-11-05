import os
import socket
from urllib.parse import urlparse
from app.core.task_manager import (
    record_local_task,
    get_local_task,
    submit_local_task,
    LocalAsyncResult,
    LocalTaskContext,
)

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "")


def is_celery_broker_active() -> bool:
    """
    Checks if a real external Celery broker (Redis / RabbitMQ) is explicitly configured and reachable.
    In standard 2-service deployment (API + Dashboard), Celery is unconfigured by default,
    enabling lightweight in-process background worker execution without external services.
    """
    if os.getenv("DISABLE_CELERY", "").lower() in ("1", "true", "yes"):
        return False
    broker_url = os.getenv("CELERY_BROKER_URL")
    if not broker_url:
        return False
    try:
        parsed = urlparse(broker_url)
        if parsed.scheme in ("redis", "rediss"):
            host = parsed.hostname or "localhost"
            port = parsed.port or 6379
            with socket.create_connection((host, port), timeout=0.5):
                return True
        elif parsed.scheme in ("amqp", "amqps"):
            host = parsed.hostname or "localhost"
            port = parsed.port or 5672
            with socket.create_connection((host, port), timeout=0.5):
                return True
        return True
    except Exception:
        return False


class ResilientTaskWrapper:
    """
    Wraps a task function so that .delay() and .apply_async() work unconditionally:
    - If Celery broker is online and reachable, dispatches via Celery.
    - If Celery broker is offline or unconfigured, executes asynchronously via local thread pool.
    """
    def __init__(self, raw_fn, celery_task=None, bind: bool = False):
        self.raw_fn = raw_fn
        self.celery_task = celery_task
        self.bind = bind

    def __call__(self, *args, **kwargs):
        if self.celery_task:
            return self.celery_task(*args, **kwargs)
        return self.raw_fn(*args, **kwargs)

    def delay(self, *args, **kwargs):
        if is_celery_broker_active() and self.celery_task:
            try:
                return self.celery_task.delay(*args, **kwargs)
            except Exception as e:
                print(f"[Celery Warning] Could not dispatch to Celery broker ({e}). Falling back to local background execution.", flush=True)
        return submit_local_task(self.raw_fn, *args, bind=self.bind, **kwargs)

    def apply_async(self, args=None, kwargs=None, **options):
        args = args or ()
        kwargs = kwargs or {}
        return self.delay(*args, **kwargs)


class _MockControl:
    def revoke(self, *args, **kwargs):
        pass


class ResilientCeleryApp:
    def __init__(self):
        self._real_celery = None
        if os.getenv("CELERY_BROKER_URL"):
            try:
                from celery import Celery
                self._real_celery = Celery(
                    "graphrag_worker",
                    broker=os.getenv("CELERY_BROKER_URL"),
                    backend=os.getenv("CELERY_RESULT_BACKEND", os.getenv("CELERY_BROKER_URL")),
                    include=["app.tasks.ingestion", "app.tasks.autodoc"]
                )
                self._real_celery.conf.update(
                    task_serializer="json",
                    accept_content=["json"],
                    result_serializer="json",
                    timezone="UTC",
                    enable_utc=True,
                )
            except Exception:
                self._real_celery = None

    def task(self, *dargs, **dkwargs):
        bind = dkwargs.get("bind", False)
        def decorator(fn):
            celery_t = None
            if self._real_celery:
                try:
                    celery_t = self._real_celery.task(*dargs, **dkwargs)(fn)
                except Exception:
                    celery_t = None
            return ResilientTaskWrapper(fn, celery_task=celery_t, bind=bind)
        if len(dargs) == 1 and callable(dargs[0]):
            fn = dargs[0]
            dargs = ()
            return decorator(fn)
        return decorator

    def AsyncResult(self, task_id: str):
        local_t = get_local_task(task_id)
        if local_t:
            return LocalAsyncResult(task_id)
        if self._real_celery and is_celery_broker_active():
            try:
                return self._real_celery.AsyncResult(task_id)
            except Exception:
                pass
        return LocalAsyncResult(task_id)

    @property
    def control(self):
        if self._real_celery and is_celery_broker_active():
            try:
                return self._real_celery.control
            except Exception:
                pass
        return _MockControl()


celery_app = ResilientCeleryApp()
