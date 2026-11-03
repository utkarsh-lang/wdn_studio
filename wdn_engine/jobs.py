"""Background jobs for long analyses (one worker: predictable CPU use on small hosts)."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

_executors = {"analysis": ThreadPoolExecutor(max_workers=1, thread_name_prefix="wdn-job"),
              "chat": ThreadPoolExecutor(max_workers=2, thread_name_prefix="wdn-chat")}


class Job:
    def __init__(self, name: str):
        self.id = uuid.uuid4().hex[:10]
        self.name = name
        self.status = "queued"   # queued | running | done | error
        self.progress = 0.0
        self.message = "Waiting for the analysis worker…"
        self.result: Any = None
        self.error: str | None = None
        self.started = time.time()
        self.steps: list[str] = []
        self._lock = threading.Lock()

    def update(self, progress: float, message: str) -> None:
        with self._lock:
            self.progress = max(self.progress, min(1.0, float(progress)))
            self.message = message
            if message and (not self.steps or self.steps[-1] != message):
                self.steps.append(message)

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {"job_id": self.id, "name": self.name, "status": self.status, "progress": round(self.progress, 3),
                    "message": self.message, "steps": list(self.steps[-30:]), "error": self.error,
                    "elapsed_s": round(time.time() - self.started, 1)}


def submit(session, name: str, fn: Callable[..., Any], pool: str = "analysis") -> Job:
    """Run ``fn(progress)`` in the background; keep the 10 most recent jobs on the session."""
    job = Job(name)
    jobs = getattr(session, "jobs", None)
    if jobs is None:
        jobs = session.jobs = {}
    jobs[job.id] = job
    for old in list(jobs)[:-10]:
        jobs.pop(old, None)

    def run():
        job.status = "running"
        try:
            job.result = fn(job.update)
            job.progress, job.message, job.status = 1.0, "Done", "done"
        except Exception as error:  # surfaced in the UI
            job.error = f"{type(error).__name__}: {error}"
            job.status = "error"
            traceback.print_exc()

    import contextvars
    _executors[pool].submit(contextvars.copy_context().run, run)
    return job
