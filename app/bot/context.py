"""Shared runtime state for the handlers.

Handlers are plain functions registered with Pyrogram, so instead of relying on
framework specific dependency injection the running application registers a
single :class:`BotContext` at startup. Tests can build one by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config.settings import Settings
from app.queue.upload_queue import UploadJob, UploadQueue
from app.rpmshare.client import RPMShareClient
from app.services.upload_service import UploadService
from app.telegram.streamer import TelegramChunkStreamer
from app.ui.status import StatusEditor

__all__ = ["BotContext", "set_context", "get_context"]


@dataclass
class BotContext:
    settings: Settings
    queue: UploadQueue
    service: UploadService
    rpm: RPMShareClient
    streamer: TelegramChunkStreamer
    client: Any = None

    #: job id → the (single) status message being edited for that job
    editors: dict[str, StatusEditor] = field(default_factory=dict)
    #: job id → watcher task
    watchers: dict[str, Any] = field(default_factory=dict)
    #: chats that already saw the startup animation
    greeted: set[int] = field(default_factory=set)
    #: (monotonic timestamp, AccountInfo) — cached for the ℹ️ Details screen
    account_cache: tuple[float, Any] | None = None
    started: bool = False

    #: how many finished-job editors are kept for the buttons that still work
    editor_history: int = 64

    # ------------------------------------------------------------------
    def remember(self, job_id: str, editor: StatusEditor) -> None:
        """Track the status message of a job, keeping the map bounded."""
        self.editors[job_id] = editor
        overflow = len(self.editors) - self.editor_history
        for stale in list(self.editors)[: max(0, overflow)]:
            self.editors.pop(stale, None)

    def forget(self, job_id: str) -> None:
        self.watchers.pop(job_id, None)

    def reset_job(self, job: UploadJob) -> None:
        """Prepare a finished job for a retry from the ❌/🔄 buttons."""
        import asyncio
        import time

        from app.ui.animations import Stage
        from app.ui.progress import UploadProgress

        job.state = job.state.__class__.QUEUED
        job.cancel_event = asyncio.Event()
        job.result = None
        job.error = ""
        job.attempts = 0
        job.started_at = None
        job.finished_at = None
        job.created_at = time.time()
        job.progress = UploadProgress(job_id=job.id, file_name=job.file_name, total=job.size)
        job.progress.stage = Stage.QUEUED


_CONTEXT: BotContext | None = None


def set_context(context: BotContext) -> None:
    global _CONTEXT
    _CONTEXT = context


def get_context() -> BotContext:
    if _CONTEXT is None:  # pragma: no cover - only when misused
        raise RuntimeError("BotContext is not initialised — call set_context() first")
    return _CONTEXT
