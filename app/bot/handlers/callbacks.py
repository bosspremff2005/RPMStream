"""Callback query dispatcher — the heart of the button driven interface."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.bot.context import get_context
from app.bot.handlers.media import watch_job
from app.bot.keyboards.inline import (
    Callback,
    about_keyboard,
    cancel_confirm_keyboard,
    creator_links_keyboard,
    home_keyboard,
    link_reveal_keyboard,
    details_keyboard,
    queue_navigation,
)
from app.bot.messages import texts
from app.bot.screens import job_screen
from app.utils.logger import get_logger

__all__ = ["handle_callback"]

log = get_logger("bot.callbacks")


async def handle_callback(client: Any, query: Any) -> None:
    """Route ``callback_data`` to the right screen."""
    ctx = get_context()
    settings = ctx.settings
    data = (getattr(query, "data", "") or "").strip()
    action, _, arg = data.partition(":")
    user_id = getattr(getattr(query, "from_user", None), "id", None)

    if not settings.user_allowed(user_id):
        await _answer(query, "🔒 This bot is private.")
        return

    try:
        await _dispatch(client, query, action, arg, user_id)
    except Exception as exc:  # noqa: BLE001 - a callback must never crash the dispatcher
        log.exception("Callback %r failed: %s", data, exc)
        await _answer(query, "⚠️ Something went wrong, try again.")


# ----------------------------------------------------------------------
async def _dispatch(client: Any, query: Any, action: str, arg: str, user_id: int | None) -> None:
    ctx = get_context()
    settings = ctx.settings

    if action == Callback.HOME:
        await _edit(query, texts.welcome(
            user_name=getattr(getattr(query, "from_user", None), "first_name", "") or "there",
            bot_title=settings.bot_title,
            tagline=settings.bot_tagline,
            animated=settings.animated_emoji_id,
        ), home_keyboard(queue_count=ctx.queue.snapshot().total))
        return

    if action == Callback.HELP:
        await _edit(query, texts.help_text(bot_title=settings.bot_title), home_keyboard())
        return

    if action == Callback.ABOUT:
        await _edit(query, texts.about(
            bot_title=settings.bot_title,
            tagline=settings.bot_tagline,
            creator=settings.creator_name,
            animated=settings.animated_emoji_id,
        ), about_keyboard())
        return

    if action == Callback.CREATOR:
        await _edit(query, texts.creator_screen(creator=settings.creator_name), creator_links_keyboard())
        await _answer(query, "👨‍💻 Creator links")
        return

    if action == Callback.QUEUE:
        page = _to_int(arg, 0)
        snapshot = ctx.queue.snapshot()
        text, pages = texts.queue_screen(snapshot, page=page, bot_title=settings.bot_title)
        await _edit(query, text, queue_navigation(page, pages))
        await _answer(query, f"📊 {snapshot.total} job(s)")
        return

    if action == Callback.STATUS:
        job = await _owned_job(ctx, arg, user_id, query)
        if job is None:
            return
        text, markup = job_screen(job, settings, tick=_tick())
        await _edit(query, text, markup)
        await _answer(query, "🔄 Updated")
        return

    if action == Callback.DETAILS:
        job = await _owned_job(ctx, arg, user_id, query)
        if job is None:
            return
        await _edit(query, texts.details(job, account=await _account_info(ctx)), details_keyboard(job.id))
        await _answer(query, "ℹ️ Details")
        return

    if action == Callback.LINK:
        job = await _owned_job(ctx, arg, user_id, query)
        if job is None:
            return
        if job.result is None:
            await _answer(query, "⏳ No link yet")
            return
        await _edit(query, texts.link_reveal(links=job.result), link_reveal_keyboard(job.id))
        await _answer(query, "📋 Links revealed")
        return

    if action == Callback.CANCEL:
        job = await _owned_job(ctx, arg, user_id, query)
        if job is None:
            return
        if job.state.is_finished:
            await _answer(query, "This upload already finished")
            return
        await _edit(query, texts.cancel_confirm(file_name=job.file_name, size=job.size), cancel_confirm_keyboard(job.id))
        return

    if action == Callback.CANCEL_YES:
        job = await _owned_job(ctx, arg, user_id, query)
        if job is None:
            return
        await ctx.queue.cancel(job.id)
        await _edit(
            query,
            "🛑 <b>Cancelling…</b>\n\n<i>Stopping the transfer and cleaning up.</i>",
            None,
        )
        await _answer(query, "🛑 Cancelling")
        return

    if action == Callback.CANCEL_NO:
        job = await _owned_job(ctx, arg, user_id, query)
        if job is None:
            return
        text, markup = job_screen(job, settings, tick=_tick())
        await _edit(query, text, markup)
        await _answer(query, "↩️ Kept running")
        return

    if action == Callback.RETRY:
        job = await _owned_job(ctx, arg, user_id, query)
        if job is None:
            return
        if not job.state.is_finished:
            await _answer(query, "This upload is still running")
            return
        ctx.reset_job(job)
        position = ctx.queue.add(job)
        if position is None:
            await _edit(query, texts.queue_full(limit=settings.queue_max_items), home_keyboard())
            await _answer(query, "📥 Queue is full")
            return
        from app.ui.progress import render_queue_added

        await _edit(
            query,
            render_queue_added(
                job_id=job.id,
                file_name=job.file_name,
                size=job.size,
                position=position,
                queue_length=ctx.queue.waiting_count,
                bot_title=settings.bot_title,
                animated_emoji_id=settings.animated_emoji_id,
            ),
            home_keyboard(queue_count=ctx.queue.snapshot().total),
        )
        watcher = asyncio.create_task(watch_job(client, query.message, job), name=f"watch-{job.id}")
        ctx.watchers[job.id] = watcher
        watcher.add_done_callback(lambda _task, job_id=job.id: ctx.watchers.pop(job_id, None))
        await _answer(query, "🔁 Re-queued")
        return

    if action == Callback.CLOSE:
        try:
            await query.message.delete()
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not delete the message: %s", exc)
        ctx.forget(arg)
        return

    await _answer(query)


# ----------------------------------------------------------------------
async def _owned_job(ctx: Any, job_id: str, user_id: int | None, query: Any) -> Any | None:
    """Look a job up and check that the pressing user owns it."""
    job = ctx.queue.get(job_id)
    if job is None:
        await _answer(query, "🕓 This upload is no longer tracked")
        return None
    if user_id is not None and job.user_id != user_id:
        await _answer(query, "🙂 This is not your upload")
        return None
    return job


async def _account_info(ctx: Any) -> Any:
    """Cached account info for the ℹ️ Details screen (60 s)."""
    cached = getattr(ctx, "account_cache", None)
    if cached and (time.monotonic() - cached[0]) < 60:
        return cached[1]
    try:
        info = await ctx.rpm.account_info()
    except Exception as exc:  # noqa: BLE001 - optional enrichment
        log.debug("account/info unavailable: %s", exc)
        return cached[1] if cached else None
    ctx.account_cache = (time.monotonic(), info)
    return info


async def _edit(query: Any, text: str, markup: Any) -> None:
    """Edit the callback's message, falling back to a new message."""
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except Exception as exc:  # noqa: BLE001
        if "not modified" in str(exc).lower():
            return
        log.debug("Could not edit message (%s), sending a new one", type(exc).__name__)
        try:
            await query.message.reply_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 - nothing left to do
            pass


async def _answer(query: Any, text: str = "") -> None:
    """Dismiss the button spinner."""
    try:
        await query.answer(text or None, cache_time=2)
    except Exception:  # noqa: BLE001 - answering is best effort
        pass


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tick() -> int:
    return int(time.monotonic() * 4)
