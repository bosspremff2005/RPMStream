"""Screen builders shared by the watcher loop and the callback dispatcher."""

from __future__ import annotations

from typing import Any

from app.bot.keyboards.inline import Callback, build, cb, error_keyboard, status_keyboard, success_keyboard
from app.bot.messages import texts
from app.config.settings import Settings
from app.queue.upload_queue import JobState, UploadJob
from app.ui.progress import render_progress

__all__ = ["job_screen", "sticker_for_stage"]


def job_screen(job: UploadJob, settings: Settings, *, tick: int = 0) -> tuple[str, Any]:
    """Return ``(text, reply_markup)`` for the job's current state."""
    if job.state is JobState.DONE and job.result is not None:
        text = texts.success(
            file_name=job.file_name,
            size=job.size,
            elapsed=job.elapsed,
            links=job.result,
            job_id=job.id,
            bot_title=settings.bot_title,
        )
        return text, success_keyboard(job.result, job.id)

    if job.state is JobState.CANCELLED:
        return (
            texts.cancelled(file_name=job.file_name),
            build([cb("🏠 Home", Callback.HOME)], [cb("📊 Queue", f"{Callback.QUEUE}:0")]),
        )

    if job.state is JobState.FAILED:
        text = texts.failure(
            file_name=job.file_name,
            reason=job.progress.error or job.error or "Something interrupted the transfer.",
            attempts=max(1, job.attempts),
            retryable=True,
        )
        return text, error_keyboard(job.id)

    text = render_progress(
        job.progress,
        tick=tick,
        bar_width=settings.progress_bar_width,
        bot_title=settings.bot_title,
        animated_emoji_id=settings.animated_emoji_id,
    )
    return text, status_keyboard(job.id, running=True)


def sticker_for_stage(job: UploadJob, settings: Settings) -> str:
    """Sticker file id for the job's terminal state ('' when not configured)."""
    if not settings.send_stage_stickers:
        return ""
    if job.state is JobState.DONE:
        return settings.success_sticker_id
    if job.state is JobState.FAILED:
        return settings.error_sticker_id
    return ""
