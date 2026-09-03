"""The pipeline: Telegram chunk → RPMShare, with retries and live progress.

This module knows nothing about Telegram message editing; it only mutates the
job's :class:`~app.ui.progress.UploadProgress`, which the bot layer renders.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from collections.abc import Callable

from app.config.settings import Settings
from app.queue.upload_queue import UploadJob
from app.rpmshare.client import FileLinks, RPMShareClient
from app.telegram.streamer import FileReferenceExpired, MediaSource, TelegramChunkStreamer
from app.ui.animations import Stage
from app.utils.errors import RPMStreamError, UploadCancelled
from app.utils.logger import get_logger

__all__ = ["UploadService"]

log = get_logger("service.upload")

_SPEED_EWMA = 0.35  # smoothing factor for the displayed speed
_TICKER_INTERVAL = 1.0


class UploadService:
    """Runs one upload job from start to finish."""

    def __init__(
        self,
        settings: Settings,
        rpm: RPMShareClient,
        streamer: TelegramChunkStreamer,
        *,
        on_stage: Callable[[UploadJob, Stage], Any] | None = None,
        source_refresher: Callable[[MediaSource], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._rpm = rpm
        self._streamer = streamer
        self._on_stage = on_stage
        self._refresh = source_refresher

    # ------------------------------------------------------------------
    def size_limit_bytes(self) -> int:
        return self._settings.max_file_size_mb * 1024 * 1024

    def exceeds_size_limit(self, source: MediaSource) -> bool:
        limit = self.size_limit_bytes()
        return bool(limit) and source.size > limit

    def title_for(self, source: MediaSource) -> str:
        template = self._settings.rpmshare_title_template or "{file_name}"
        try:
            return template.format(file_name=source.file_name, file_code=source.file_unique_id)
        except (KeyError, IndexError):  # pragma: no cover - bad template in .env
            return source.file_name

    # ------------------------------------------------------------------
    async def run(self, job: UploadJob) -> FileLinks:
        """Stream ``job`` to RPMShare. Raises on permanent failure."""
        progress = job.progress
        progress.max_retries = self._settings.max_retries
        ticker = asyncio.create_task(self._ticker(job), name=f"ticker-{job.id}")

        try:
            attempt = 0
            while True:
                attempt += 1
                job.attempts = attempt
                progress.attempt = attempt
                try:
                    links = await self._attempt(job)
                    job.result = links
                    progress.file_code = links.file_code
                    progress.transferred = progress.total or progress.transferred
                    progress.speed = progress.speed or (progress.total / max(1.0, job.elapsed))
                    self._set_stage(job, Stage.COMPLETE)
                    log.info("Completed %s → %s in %.1fs", job.file_name, links.file_code, job.elapsed)
                    return links
                except UploadCancelled:
                    self._set_stage(job, Stage.CANCELLED)
                    raise
                except RPMStreamError as exc:
                    log.warning("Attempt %d/%d for %s failed: %s", attempt, self._settings.max_retries + 1, job.file_name, exc)
                    if not exc.retryable:
                        progress.error = exc.user_message
                        self._set_stage(job, Stage.FAILED)
                        raise
                    if attempt > self._settings.max_retries:
                        progress.error = exc.user_message
                        self._set_stage(job, Stage.FAILED)
                        log.error("Giving up on %s after %d attempts", job.file_name, attempt)
                        raise
                    if isinstance(exc, FileReferenceExpired):
                        await self._refresh_file_reference(job)
                    delay = self._backoff_delay(attempt)
                    progress.detail = f"🔁 Retrying in {delay:.0f}s…"
                    await self._cancellable_sleep(delay, job)
                    progress.detail = ""
                    progress.transferred = 0
                    progress.speed = 0.0
                except asyncio.CancelledError:
                    self._set_stage(job, Stage.CANCELLED)
                    raise
                except Exception as exc:  # noqa: BLE001 - unexpected, must not kill the worker
                    log.exception("Unexpected failure while uploading %s", job.file_name)
                    progress.error = "Unexpected error while streaming."
                    self._set_stage(job, Stage.FAILED)
                    raise RPMStreamError(str(exc), user_message="Unexpected error while streaming.") from exc
        finally:
            ticker.cancel()

    # ------------------------------------------------------------------
    async def _attempt(self, job: UploadJob) -> FileLinks:
        progress = job.progress
        source: MediaSource = job.source

        self._set_stage(job, Stage.STARTING)
        upload_url = await self._rpm.get_upload_server(force=job.attempts > 1)

        self._set_stage(job, Stage.PREPARING)
        content_type = source.mime_type or "video/mp4"
        title = self.title_for(source)

        self._set_stage(job, Stage.CONNECTING)

        def on_chunk(byte_count: int) -> None:
            progress.advance(byte_count)

        def chunk_factory():
            # A brand new generator per attempt: retries always restart cleanly.
            return self._streamer.stream(
                source,
                cancel=job.cancel_event,
                on_chunk=on_chunk,
            )

        self._set_stage(job, Stage.TRANSFERRING)
        uploaded = await self._rpm.upload_stream(
            chunk_factory=chunk_factory,
            file_name=source.file_name,
            file_size=source.size,
            content_type=content_type,
            title=title,
            tags=self._settings.rpmshare_tags,
            folder_id=self._settings.rpmshare_folder_id,
            category_id=self._settings.rpmshare_category_id,
            public=self._settings.rpmshare_public,
            adult=self._settings.rpmshare_adult,
            upload_url=upload_url,
        )

        self._set_stage(job, Stage.PROCESSING)
        links = await self._rpm.resolve_links(
            uploaded.filecode,
            watch_url_template=self._settings.rpmshare_file_url_template,
        )

        self._set_stage(job, Stage.FINALIZING)
        return links

    # ------------------------------------------------------------------
    async def _refresh_file_reference(self, job: UploadJob) -> None:
        """Ask Telegram for a fresh file reference and swap it into the job."""
        if self._refresh is None:
            return
        try:
            fresh = await self._refresh(job.source)
        except Exception as exc:  # noqa: BLE001 - keep the original source on failure
            log.warning("Could not refresh the file reference for %s: %s", job.file_name, exc)
            return
        job.source = fresh
        job.progress.file_name = fresh.file_name
        job.progress.total = fresh.size
        job.progress.transferred = 0
        log.info("Refreshed the Telegram file reference for %s", fresh.file_name)

    def _set_stage(self, job: UploadJob, stage: Stage) -> None:
        job.progress.stage = stage
        if self._on_stage is not None:
            try:
                self._on_stage(job, stage)
            except Exception:  # noqa: BLE001 - UI hooks must never break uploads
                log.debug("on_stage hook failed", exc_info=True)

    def _backoff_delay(self, attempt: int) -> float:
        base = self._settings.retry_base_delay * (2 ** (attempt - 1))
        delay = min(base, self._settings.retry_max_delay)
        return delay + random.uniform(0, min(2.0, delay * 0.15))

    async def _cancellable_sleep(self, delay: float, job: UploadJob) -> None:
        """Sleep for ``delay`` but wake immediately when the user cancels."""
        try:
            await asyncio.wait_for(job.cancel_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return
        raise UploadCancelled("Upload cancelled during retry backoff")

    # ------------------------------------------------------------------
    async def _ticker(self, job: UploadJob) -> None:
        """Sample transferred bytes once a second to derive speed / ETA."""
        progress = job.progress
        last_bytes = 0
        last_time = time.monotonic()
        stall_since = last_time

        try:
            while True:
                await asyncio.sleep(_TICKER_INTERVAL)
                now = time.monotonic()
                elapsed = max(0.001, now - last_time)
                delta = progress.transferred - last_bytes
                progress.elapsed = job.elapsed

                if delta < 0:  # a retry reset the counter
                    delta = 0

                if delta > 0:
                    instant = delta / elapsed
                    progress.speed = instant if progress.speed <= 0 else (_SPEED_EWMA * instant + (1 - _SPEED_EWMA) * progress.speed)
                    stall_since = now
                elif now - stall_since > 5:
                    progress.speed *= 0.5  # decaying display while stalled

                last_bytes = progress.transferred
                last_time = now
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown path
            raise


async def refresh_source(client: Any, source: MediaSource) -> MediaSource:
    """Re-resolve a media object when Telegram rotated its file reference."""
    from app.telegram.streamer import detect_media

    message = await client.get_messages(source.chat_id, source.message_id)
    fresh = detect_media(message)
    if fresh is None:  # pragma: no cover - message vanished
        raise FileReferenceExpired("The source message is no longer available")
    log.info("Refreshed the Telegram file reference for %s", source.file_name)
    return fresh
