"""Slash commands.

The bot is button first, but a handful of commands stay available because users
expect them. Each one renders exactly the same screen as its button.
"""

from __future__ import annotations

from typing import Any

from app.bot.context import get_context
from app.bot.keyboards.inline import (
    about_keyboard,
    creator_links_keyboard,
    home_keyboard,
    queue_navigation,
)
from app.bot.messages import texts
from app.bot.handlers.media import send_startup_sticker
from app.queue.upload_queue import JobState
from app.utils.logger import get_logger

__all__ = ["handle_command"]

log = get_logger("bot.commands")


async def handle_command(client: Any, message: Any) -> None:
    ctx = get_context()
    settings = ctx.settings

    user = getattr(message, "from_user", None)
    user_id = getattr(user, "id", None)
    chat_id = getattr(getattr(message, "chat", None), "id", 0)
    user_name = getattr(user, "first_name", "") or getattr(user, "username", "") or "there"

    command = "start"
    raw_command = getattr(message, "command", None)
    if raw_command:
        command = str(raw_command[0]).split("@")[0].lower()

    if not settings.user_allowed(user_id):
        await message.reply_text(texts.not_allowed())
        return

    if command == "start":
        if chat_id and chat_id not in ctx.greeted:
            ctx.greeted.add(chat_id)
            await send_startup_sticker(client, chat_id, settings.startup_sticker_id)
        await message.reply_text(
            texts.welcome(
                user_name=user_name,
                bot_title=settings.bot_title,
                tagline=settings.bot_tagline,
                animated=settings.animated_emoji_id,
            ),
            reply_markup=home_keyboard(queue_count=ctx.queue.snapshot().total),
            disable_web_page_preview=True,
        )
        return

    if command == "help":
        await message.reply_text(texts.help_text(bot_title=settings.bot_title), reply_markup=home_keyboard())
        return

    if command == "about":
        await message.reply_text(
            texts.about(
                bot_title=settings.bot_title,
                tagline=settings.bot_tagline,
                creator=settings.creator_name,
                animated=settings.animated_emoji_id,
            ),
            reply_markup=about_keyboard(),
        )
        return

    if command == "queue":
        snapshot = ctx.queue.snapshot()
        text, pages = texts.queue_screen(snapshot, page=0, bot_title=settings.bot_title)
        await message.reply_text(text, reply_markup=queue_navigation(0, pages))
        return

    if command == "status":
        jobs = [job for job in ctx.queue.jobs(user_id) if job.state in {JobState.QUEUED, JobState.RUNNING}]
        if not jobs:
            await message.reply_text(
                "😌 <b>Nothing is uploading right now.</b>\n\nSend me a video to start 🎬",
                reply_markup=home_keyboard(),
            )
            return
        from app.bot.screens import job_screen

        for job in jobs[:5]:
            text, markup = job_screen(job, settings)
            await message.reply_text(text, reply_markup=markup)
        return

    if command == "cancel":
        count = await ctx.queue.cancel_all(user_id)
        await message.reply_text(
            f"🛑 <b>Cancelled {count} upload(s).</b>" if count else "😌 There was nothing of yours to cancel.",
            reply_markup=home_keyboard(),
        )
        return

    if command == "creator":  # pragma: no cover - reachable through /creator
        await message.reply_text(texts.creator_screen(creator=settings.creator_name), reply_markup=creator_links_keyboard())
        return

    log.debug("Unhandled command: %s", command)
