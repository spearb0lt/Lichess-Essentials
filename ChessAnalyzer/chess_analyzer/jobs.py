"""Long work, run in the background, watchable from the browser.

Two things here take minutes: reviewing a game and downloading an engine.
Doing either inside a request means a browser that spins with no idea how far
along it is, and a timeout on anything genuinely slow.

So both become jobs.  A job runs on its own thread, reports progress into a
dictionary the browser polls, and can be cancelled.  Cancellation is
cooperative -- the worker is handed a ``should_stop`` and checks it at a
sensible boundary -- because killing a thread mid-way through an engine
conversation leaves a Stockfish process orphaned and a half-written file on
disk.

Finished jobs are kept briefly so a browser that polls a moment late still
sees the result, then swept.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid

#: How long a finished job's result stays readable.
KEEP = 900.0


class Job:
    """One unit of background work and everything a poller wants to know."""

    def __init__(self, kind: str, label: str = ""):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.label = label
        self.state = "queued"          # queued | running | done | failed | cancelled
        self.done = 0
        self.total = 0
        self.message = ""
        self.result = None
        self.error = ""
        self.started = time.time()
        self.finished_at: float | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------- worker-facing

    def progress(self, done: int, total: int, message: str | None = None) -> None:
        with self._lock:
            self.done = done
            self.total = total
            if message is not None:
                self.message = message

    def should_stop(self) -> bool:
        return self._stop.is_set()

    # -------------------------------------------------------- caller-facing

    def cancel(self) -> None:
        self._stop.set()
        with self._lock:
            if self.state in ("queued", "running"):
                self.message = "Stopping..."

    def json(self) -> dict:
        with self._lock:
            share = (self.done / self.total) if self.total else 0.0
            return {
                "id": self.id,
                "kind": self.kind,
                "label": self.label,
                "state": self.state,
                "done": self.done,
                "total": self.total,
                "percent": round(min(1.0, max(0.0, share)) * 100, 1),
                "message": self.message,
                "error": self.error,
                "elapsed": round((self.finished_at or time.time()) - self.started, 1),
                "result": self.result,
            }


class JobRunner:
    """The process's background jobs."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, work, *, label: str = "") -> Job:
        """Run ``work(job)`` on a thread. Whatever it returns becomes the result."""
        job = Job(kind, label=label)
        with self._lock:
            self._jobs[job.id] = job

        def run() -> None:
            job.state = "running"
            try:
                job.result = work(job)
                job.state = "cancelled" if job.should_stop() else "done"
            except Exception as exc:                # noqa: BLE001
                # Any failure belongs to the job, never to the server: an
                # unhandled exception on a background thread would otherwise
                # vanish into the log with the browser still spinning.
                job.state = "cancelled" if job.should_stop() else "failed"
                job.error = str(exc) or exc.__class__.__name__
                job.message = ""
                if job.state == "failed":
                    traceback.print_exc()
            finally:
                job.finished_at = time.time()
                self.sweep()

        threading.Thread(target=run, name=f"job-{kind}-{job.id}",
                         daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def listing(self) -> list[dict]:
        """Every job, without its result.

        A finished review is a hundred-odd kilobytes; the listing is polled to
        draw progress bars and has no use for them. Fetch one job by id to get
        its result.
        """
        with self._lock:
            return [{key: value for key, value in job.json().items()
                     if key != "result"}
                    for job in self._jobs.values()]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.cancel()
        return True

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            stale = [
                key for key, job in self._jobs.items()
                if job.finished_at is not None and now - job.finished_at > KEEP
            ]
            for key in stale:
                self._jobs.pop(key, None)


RUNNER = JobRunner()

__all__ = ["Job", "JobRunner", "RUNNER"]
