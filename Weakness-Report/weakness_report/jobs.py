"""Long work, run in the background, watchable from the browser.

Reviewing is the slow thing here, and it is slow on a different scale from
anything else in this repository: four hundred games at a fixed depth is
minutes at best and an hour or two at worst.  Nothing like that can happen
inside an HTTP request.

So a run is a job: its own thread, progress into a dictionary the browser
polls, and cancellable.  Cancellation is cooperative -- the worker is handed a
``should_stop`` and checks it between games and inside each review -- because
a cancelled batch must keep every review it has already finished, and because
killing a thread mid-search orphans a Stockfish process.

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
        self.state = "queued"        # queued | running | done | failed | cancelled
        self.done = 0
        self.total = 0
        self.message = ""
        self.result = None
        self.error = ""
        self.started = time.time()
        self.finished_at: float | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # --------------------------------------------------------- worker-facing

    def progress(self, done: int, total: int, message: str | None = None) -> None:
        with self._lock:
            self.done = done
            self.total = total
            if message is not None:
                self.message = message

    def say(self, message: str) -> None:
        with self._lock:
            self.message = message

    def should_stop(self) -> bool:
        return self._stop.is_set()

    # --------------------------------------------------------- caller-facing

    def cancel(self) -> None:
        self._stop.set()
        with self._lock:
            if self.state in ("queued", "running"):
                self.message = "Stopping..."

    def json(self, *, with_result: bool = True) -> dict:
        with self._lock:
            share = (self.done / self.total) if self.total else 0.0
            data = {
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
            }
            if with_result:
                data["result"] = self.result
            return data


class JobRunner:
    """The process's background jobs."""

    def __init__(self):
        self._jobs: dict = {}
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
            except Exception as exc:                          # noqa: BLE001
                # A failure belongs to the job, never to the server: an
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

    def get(self, job_id: str):
        with self._lock:
            return self._jobs.get(job_id)

    def listing(self) -> list:
        """Every job without its result -- a report is far too big to poll."""
        with self._lock:
            return [job.json(with_result=False) for job in self._jobs.values()]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.cancel()
        return True

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            stale = [key for key, job in self._jobs.items()
                     if job.finished_at is not None and now - job.finished_at > KEEP]
            for key in stale:
                self._jobs.pop(key, None)


RUNNER = JobRunner()

__all__ = ["Job", "JobRunner", "RUNNER"]
