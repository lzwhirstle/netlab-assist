from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import traceback
import uuid
from typing import Any, Callable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    progress: float = 0.0
    samples: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return deepcopy({key: value for key, value in self.__dict__.items() if key != "cancel"})


class JobContext:
    def __init__(self, manager: "JobManager", job_id: str):
        self.manager = manager
        self.job_id = job_id

    @property
    def cancelled(self) -> bool:
        return self.manager._jobs[self.job_id].cancel.is_set()

    def update(self, *, progress: float | None = None, sample: dict[str, Any] | None = None) -> None:
        with self.manager._lock:
            job = self.manager._jobs[self.job_id]
            if progress is not None:
                job.progress = max(0.0, min(1.0, float(progress)))
            if sample is not None:
                job.samples.append(deepcopy(sample))
                if len(job.samples) > 600:
                    del job.samples[:-600]


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()

    def start(self, kind: str, target: Callable[[JobContext], dict[str, Any]]) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, kind=kind)
        with self._lock:
            self._jobs[job_id] = job

        def runner() -> None:
            with self._lock:
                job.status = "running"
                job.started_at = utc_now()
            try:
                result = target(JobContext(self, job_id))
                with self._lock:
                    job.status = "cancelled" if job.cancel.is_set() else "completed"
                    job.result = result
                    job.progress = 1.0 if not job.cancel.is_set() else job.progress
            except Exception as exc:  # report boundary; traceback remains local
                with self._lock:
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.result = {"traceback": traceback.format_exc(limit=8)}
            finally:
                with self._lock:
                    job.finished_at = utc_now()

        threading.Thread(target=runner, name=f"job-{kind}-{job_id}", daemon=True).start()
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.public() if job else None

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.cancel.set()
            return True

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._lock:
            for job in self._jobs.values():
                counts[job.status] = counts.get(job.status, 0) + 1
        return counts
