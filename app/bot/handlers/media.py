"""Media intake + the single live status message per upload.

The watcher loop is the only thing that edits a job's message, so an upload can
never turn into message spam.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.bot.context import get_context
from app.bot.keyboards.inline import Callback, build, cb, queue_keyboard, status_keyboard
from app.bot.messages import texts
from app.bot.screens import job_screen, sticker_for_stage
from app.queue.upload_queue import JobState, UploadJob
from app.telegram.streamer import detect_media
from app.ui.animations import Stage
from app.ui.progress import render_progress, render_queue_added
from app.ui.status import StatusEditor
from app.utils.logger import get_logger

__all__ = ["handle_media", "watch_job", "send_startup_sticker"]

log = get_logger("bot.media")

_POLL = 1.0  # how often the watcher wakes up (edits stay throttled inside StatusEditor)


async def send_startup_sticker(client: Any, chat_id: int, sticker_id: str) -> None:
    """Send the optional animated startup sticker exactly once per chat."""
    if not sticker_id:
        return
    try:
        await client.send_sticker(chat_id, sticker_id)
    except Exception as exc:  # noqa: BLE001 - a bad sticker id must not break the bot
        log.debug("Startup sticker not sent: %s", exc)


async def handle_media(client: Any, message: Any) -> None:
    """Detect the incoming media and put it on the upload queue."""
    ctx = get_context()
    settings = ctx.settings

    user = getattr(message, "from_user", None)
    user_id = getattr(user, "id", None) or getattr(getattr(message, "chat", None), "id", 0)
    chat_id = getattr(getattr(message, "chat", None), "id", 0)

    if not settings.user_allowed(user_id):
        await message.reply_text(texts.not_allowed(), reply_markup=build())
        return

    source = detect_media(message, allow_any_document=settings.allow_any_document)
    if source is None:
        log.info("Ignoring unsupported media from user %s", user_id)
        await message.reply_text(texts.unsupported(bot_title=settings.bot_title), reply_markup=queue_keyboard(0))
        return

    if ctx.service.exceeds_size_limit(source):
        await message.reply_text(
            "\n".join(
                [
                    "⚠️ <b>File too large</b>",
                    "",
                    f"This bot is capped at <b>{settings.max_file_size_mb} MB</b> per file.",
                ]
            ),
            reply_markup=queue_keyboard(0),
        )
        return

    job = UploadJob(user_id=user_id, chat_id=chat_id, source=source)
    position = ctx.queue.add(job)
    if position is None:
        await message.reply_text(texts.queue_full(limit=settings.queue_max_items), reply_markup=queue_keyboard(0))
        return

    job.progress.queue_position = position
    job.progress.queue_length = ctx.queue.waiting_count

    markup = build(
        [cb("📊 View Queue", f"{Callback.QUEUE}:0"), cb("❌ Cancel", f"{Callback.CANCEL}:{job.id}")],
        [cb("🏠 Home", Callback.HOME)],
    )
    text = render_queue_added(
        job_id=job.id,
        file_name=source.file_name,
        size=source.size,
        position=position,
        queue_length=ctx.queue.waiting_count,
        bot_title=settings.bot_title,
        animated_emoji_id=settings.animated_emoji_id,
    )

    status_message = await message.reply_text(text, reply_markup=markup)
    log.info("Accepted %s (%d bytes) from user %s as job %s", source.file_name, source.size, user_id, job.id)

    watcher = asyncio.create_task(watch_job(client, status_message, job), name=f"watch-{job.id}")
    ctx.watchers[job.id] = watcher
    watcher.add_done_callback(lambda _task, job_id=job.id: ctx.watchers.pop(job_id, None))


# ----------------------------------------------------------------------
async def watch_job(client: Any, status_message: Any, job: UploadJob) -> None:
    """Keep one message in sync with the job until it finishes."""
    ctx = get_context()
    settings = ctx.settings
    editor = StatusEditor(status_message, min_interval=settings.progress_update_interval, bot_title=settings.bot_title)
    ctx.remember(job.id, editor)

    last_stage: Stage | None = None
    try:
        while not job.state.is_finished:
            await asyncio.sleep(_POLL)
            stage = job.progress.stage
            forced = stage is not last_stage
            last_stage = stage
            await editor.render(
                render_progress(
                    job.progress,
                    tick=editor.tick,
                    bar_width=settings.progress_bar_width,
                    bot_title=settings.bot_title,
                    animated_emoji_id=settings.animated_emoji_id,
                ),
                status_keyboard(job.id, running=job.state is JobState.RUNNING or job.state is JobState.QUEUED),
                force=forced,
            )
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        raise
    except Exception:  # noqa: BLE001 - never let the UI loop kill a running upload
        log.exception("Status watcher for job %s crashed", job.id)

    await render_final_screen(client, editor, job)


async def render_final_screen(client: Any, editor: StatusEditor, job: UploadJob) -> bool:
    """Replace the progress message with the success / failure / cancelled screen."""
    ctx = get_context()
    settings = ctx.settings

    text, markup = job_screen(job, settings)
    sticker = sticker_for_stage(job, settings)

    if sticker and settings.send_stage_stickers:
        try:
            await client.send_sticker(job.chat_id, sticker)
        except Exception as exc:  # noqa: BLE001
            log.debug("Stage sticker not sent: %s", exc)

    if editor.alive and await editor.replace(text, markup):
        return True

    # The status message is gone (deleted, chat lost): deliver a fresh message
    # instead of losing the result.
    log.info("Status message unavailable for job %s, sending the result as a new message", job.id)
    try:
        await client.send_message(job.chat_id, text, reply_markup=markup)
        return True
    except Exception:  # noqa: BLE001 - the chat may be gone
        log.warning("Could not deliver the final screen for job %s", job.id)
        return False
