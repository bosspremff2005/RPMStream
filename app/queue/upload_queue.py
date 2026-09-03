"""A small, dependency free upload queue built on :mod:`asyncio`.

* jobs are processed in arrival order
* at most ``MAX_CONCURRENT_UPLOADS`` run at the same time
* cancelling is cooperative (an event) *and* hard (task cancellation), so a
  stuck transfer can always be stopped
* nothing here touches Telegram or RPMShare, which keeps it easy to test
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from collections.abc import Awaitable, Callable

from app.ui.progress import UploadProgress
from app.utils.errors import UploadCancelled
from app.utils.logger import get_logger

__all__ = ["UploadQueue", "UploadJob", "QueueSnapshot", "JobState"]

log = get_logger("queue")

Runner = Callable[["UploadJob"], Awaitable[None]]


def _worker_was_cancelled() -> bool:
    """Did *this* task receive a cancellation, or only the child job task?

    Swallowing a cancellation that was meant for the worker would leave it
    blocked on ``queue.get()`` forever and hang shutdown, so when in doubt the
    answer is "yes, propagate".
    """
    current = asyncio.current_task()
    if current is None:  # pragma: no cover - defensive
        return True
    cancelling = getattr(current, "cancelling", None)
    if cancelling is None:  # pragma: no cover - Python < 3.11
        return True
    if cancelling() > 0:
        return True
    # We are swallowing a cancellation meant for the child task: clear the
    # pending-cancel accounting so the worker stays a healthy task.
    uncancel = getattr(current, "uncancel", None)
    if uncancel is not None:  # pragma: no branch
        uncancel()
    return False


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_finished(self) -> bool:
        return self in {JobState.DONE, JobState.FAILED, JobState.CANCELLED}


@dataclass
class UploadJob:
    """One video travelling through the pipeline."""

    user_id: int
    chat_id: int
    source: Any  # app.telegram.streamer.MediaSource
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: JobState = JobState.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    progress: UploadProgress | None = None
    result: Any = None
    error: str = ""
    attempts: int = 0
    _task: asyncio.Task | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.progress is None:
            self.progress = UploadProgress(
                job_id=self.id,
                file_name=self.source.file_name,
                total=int(getattr(self.source, "size", 0) or 0),
            )

    # ------------------------------------------------------------------
    @property
    def file_name(self) -> str:
        return self.source.file_name

    @property
    def size(self) -> int:
        return int(getattr(self.source, "size", 0) or 0)

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - (self.started_at or self.created_at))

    def cancel(self) -> None:
        """Ask (and force) the worker to stop."""
        self.cancel_event.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()


@dataclass
class QueueSnapshot:
    active: list[UploadJob] = field(default_factory=list)
    waiting: list[UploadJob] = field(default_factory=list)
    processed: int = 0
    failed: int = 0
    cancelled: int = 0
    workers: int = 1

    @property
    def is_empty(self) -> bool:
        return not self.active and not self.waiting

    @property
    def total(self) -> int:
        return len(self.active) + len(self.waiting)


class UploadQueue:
    """FIFO queue with a fixed pool of workers."""

    def __init__(self, runner: Runner, *, max_workers: int = 1, max_items: int = 250) -> None:
        self._runner = runner
        self._max_workers = max(1, int(max_workers))
        self._max_items = max(1, int(max_items))
        self._internal: asyncio.Queue[UploadJob] = asyncio.Queue()
        self._waiting: list[UploadJob] = []
        self._active: list[UploadJob] = []
        self._known: dict[str, UploadJob] = {}
        self._workers: list[asyncio.Task] = []
        self._processed = 0
        self._failed = 0
        self._cancelled = 0
        self._stopping = False

    # ------------------------------------------------------------------
    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    @property
    def active_count(self) -> int:
        return len(self._active)

    def __len__(self) -> int:
        return len(self._known)

    # ------------------------------------------------------------------
    def add(self, job: UploadJob) -> int | None:
        """Enqueue a job; returns its 1-based position or ``None`` when full."""
        if self._stopping:
            return None
        if len(self._waiting) >= self._max_items:
            log.warning("Queue is full (%d waiting items)", len(self._waiting))
            return None
        self._known[job.id] = job
        self._waiting.append(job)
        self._internal.put_nowait(job)
        log.info("Queued %s (%d bytes) as #%d", job.file_name, job.size, len(self._waiting))
        return len(self._waiting)

    def get(self, job_id: str) -> UploadJob | None:
        return self._known.get(job_id)

    def jobs(self, user_id: int | None = None) -> list[UploadJob]:
        """Every job seen so far, newest first (optionally for one user)."""
        found = list(self._known.values())
        if user_id is not None:
            found = [job for job in found if job.user_id == user_id]
        return sorted(found, key=lambda job: job.created_at, reverse=True)

    def position(self, job_id: str) -> int:
        for index, job in enumerate(self._waiting):
            if job.id == job_id:
                return index + 1
        return 0

    def snapshot(self) -> QueueSnapshot:
        return QueueSnapshot(
            active=list(self._active),
            waiting=list(self._waiting),
            processed=self._processed,
            failed=self._failed,
            cancelled=self._cancelled,
            workers=self._max_workers,
        )

    # ------------------------------------------------------------------
    async def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job. Returns ``True`` when something was cancelled."""
        job = self._known.get(job_id)
        if job is None or job.state.is_finished:
            return False
        log.info("Cancelling job %s (%s)", job.id, job.file_name)
        job.cancel()
        if job in self._waiting:
            try:
                self._waiting.remove(job)
            except ValueError:  # pragma: no cover - already popped by a worker
                pass
            job.state = JobState.CANCELLED
            job.finished_at = time.time()
            self._cancelled += 1
        return True

    async def cancel_all(self, user_id: int | None = None) -> int:
        """Cancel every job (optionally only those of one user)."""
        count = 0
        for job in list(self._known.values()):
            if job.state.is_finished:
                continue
            if user_id is not None and job.user_id != user_id:
                continue
            if await self.cancel(job.id):
                count += 1
        return count

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Spawn the worker tasks (idempotent)."""
        if self._workers:
            return
        self._stopping = False
        loop = asyncio.get_running_loop()
        for index in range(self._max_workers):
            self._workers.append(loop.create_task(self._worker(index), name=f"rpmstream-worker-{index}"))
        log.info("Upload queue started with %d worker(s)", self._max_workers)

    async def stop(self, *, wait: bool = True, timeout: float = 10.0) -> None:
        """Stop accepting work and unwind the workers.

        Running uploads are cancelled: a half streamed file is useless anyway and
        RPMShare drops the incomplete upload.
        """
        self._stopping = True
        for job in list(self._waiting):
            job.cancel()
            if job.state is not JobState.CANCELLED:
                job.state = JobState.CANCELLED
                job.finished_at = time.time()
                self._cancelled += 1
        self._waiting.clear()

        workers, self._workers = self._workers, []
        for task in workers:
            task.cancel()
        if wait and workers:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), timeout=timeout)
        log.info("Upload queue stopped")

    async def join(self) -> None:
        """Wait until the queue is empty (used by tests and graceful shutdown)."""
        await self._internal.join()

    # ------------------------------------------------------------------
    async def _worker(self, index: int) -> None:
        while True:
            job = await self._internal.get()
            try:
                await self._process(job, index)
            finally:
                self._internal.task_done()

    async def _process(self, job: UploadJob, index: int) -> None:
        if job.cancel_event.is_set():
            if job.state is not JobState.CANCELLED:
                job.state = JobState.CANCELLED
                job.finished_at = time.time()
                self._cancelled += 1
            return

        try:
            self._waiting.remove(job)
        except ValueError:  # pragma: no cover - defensive
            pass

        job.state = JobState.RUNNING
        job.started_at = time.time()
        self._active.append(job)
        log.info("Worker %d started %s (%s)", index, job.file_name, job.id)

        # The job runs in its own task so cancelling an upload never kills the
        # worker that picked it up.
        task = asyncio.create_task(self._runner(job), name=f"rpmstream-job-{job.id}")
        job._task = task

        try:
            await task
            if job.cancel_event.is_set():
                job.state = JobState.CANCELLED
                self._cancelled += 1
            elif job.state is JobState.RUNNING:
                job.state = JobState.DONE
                self._processed += 1
        except asyncio.CancelledError:
            if not task.done():
                # The job was cancelled (user pressed ❌ or the queue is stopping).
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            job.state = JobState.CANCELLED
            job.cancel_event.set()
            self._cancelled += 1
            log.info("Job %s was cancelled", job.id)
            # Only stop the worker when *it* was cancelled — otherwise a user
            # cancelling one upload would silently kill the worker pool.
            if _worker_was_cancelled():
                raise
        except UploadCancelled as exc:
            job.state = JobState.CANCELLED
            job.cancel_event.set()
            job.error = exc.user_message
            self._cancelled += 1
            log.info("Job %s stopped: %s", job.id, exc)
        except Exception as exc:  # noqa: BLE001 - the runner reports the details
            job.state = JobState.FAILED
            job.error = str(exc)
            self._failed += 1
            log.exception("Job %s failed: %s", job.id, exc)
        finally:
            job.finished_at = time.time()
            job._task = None
            if job in self._active:
                self._active.remove(job)
            log.info(
                "Worker %d finished %s (%s) in %.1fs",
                index,
                job.file_name,
                job.state.value,
                job.elapsed,
            )
