"""Telegram handlers."""

from app.bot.handlers.callbacks import handle_callback
from app.bot.handlers.media import handle_media, watch_job
from app.bot.handlers.start import handle_command

__all__ = ["handle_media", "handle_command", "handle_callback", "watch_job", "register_handlers"]


def register_handlers(client) -> None:
    """Attach every handler to a Pyrogram client."""
    from pyrogram import filters
    from pyrogram.handlers import CallbackQueryHandler, MessageHandler

    client.add_handler(MessageHandler(handle_command, filters.command(["start", "help", "about", "queue", "status", "cancel", "creator"])))
    client.add_handler(MessageHandler(handle_media, filters.video | filters.animation | filters.document))
    client.add_handler(CallbackQueryHandler(handle_callback))
