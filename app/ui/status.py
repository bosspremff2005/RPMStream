"""A single Telegram message that is *edited* — never spammed.

The editor is deliberately defensive: progress updates are throttled, stale
edits are skipped, and Telegram's expected errors (``MessageNotModified``,
``FloodWait``, ``MessageIdInvalid``) are swallowed instead of killing an upload.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.utils.logger import get_logger

__all__ = ["StatusEditor"]

log = get_logger("ui.status")

#: after this many consecutive failed edits the message is treated as gone
_MAX_SILENT_FAILURES = 3


class StatusEditor:
    """Owns one message and keeps it in sync with the upload state."""

    def __init__(
        self,
        message: Any,
        *,
        min_interval: float = 3.0,
        bot_title: str = "RPMStream",
    ) -> None:
        self._message = message
        self._chat_id = getattr(message, "chat", None) and message.chat.id
        self._message_id = getattr(message, "id", None)
        self._min_interval = max(0.5, float(min_interval))
        self._bot_title = bot_title
        self._last_edit = 0.0
        self._last_text: str | None = None
        self._lock = asyncio.Lock()
        self._alive = True
        self._tick = 0
        self._failures = 0

    # ------------------------------------------------------------------
    @property
    def message(self) -> Any:
        return self._message

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def tick(self) -> int:
        return self._tick

    def due(self) -> bool:
        """Whether enough time passed for a non-forced edit."""
        return (time.monotonic() - self._last_edit) >= self._min_interval

    # ------------------------------------------------------------------
    async def render(self, text: str, reply_markup: Any = None, *, force: bool = True) -> bool:
        """Edit the message; returns ``True`` only when the edit really landed.

        ``force=False`` respects the throttle window. Callers use the return
        value to decide whether a fallback (a brand new message) is needed.
        """
        if not self._alive:
            return False
        if not force and not self.due():
            return False
        async with self._lock:
            if not self._alive:
                return False
            if text == self._last_text and not force:
                return False
            try:
                self._message = await self._message.edit_text(text, reply_markup=reply_markup)
            except Exception as exc:  # noqa: BLE001 - Telegram raises many types
                if self._is_unmodified(exc):
                    self._last_text = text
                    self._failures = 0
                    return True
                return self._handle_edit_error(exc)
            else:
                self._last_text = text
                self._failures = 0
            finally:
                self._last_edit = time.monotonic()
                self._tick += 1
            return True

    async def replace(self, text: str, reply_markup: Any = None) -> bool:
        """Force an edit (used for stage changes and final screens)."""
        return await self.render(text, reply_markup, force=True)

    async def delete(self) -> None:
        """Delete the message (the 🗑️ Close button)."""
        if not self._alive:
            return
        try:
            await self._message.delete()
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not delete status message: %s", exc)
        self._alive = False

    # ------------------------------------------------------------------
    @staticmethod
    def _is_unmodified(exc: Exception) -> bool:
        """Telegram's way of saying "the message already looks like that"."""
        if type(exc).__name__ == "MessageNotModified":
            return True
        text = str(exc).lower()
        return "message is not modified" in text or "not modified" in text

    def _handle_edit_error(self, exc: Exception) -> bool:
        """Record a failed edit. Returns ``True`` when it is safe to ignore."""
        name = type(exc).__name__
        text = str(exc).lower()

        # The message (or the chat) is gone — stop editing, let the caller fall
        # back to sending a fresh message so the user still gets the result.
        if name in {"MessageIdInvalid", "PeerIdInvalid", "ChatWriteForbidden", "UserIsBlocked"}:
            log.warning("Status message is gone (%s): switching to fallback delivery", name)
            self._alive = False
            return False
        if "message to edit not found" in text or "message not found" in text:
            self._alive = False
            return False

        if name == "FloodWait":
            delay = float(getattr(exc, "value", 1) or 1)
            log.warning("FloodWait %.1fs while editing status message", delay)
            self._min_interval = max(self._min_interval, delay + 1.0)
            return True

        self._failures += 1
        if self._failures >= _MAX_SILENT_FAILURES:
            log.warning("Status edits keep failing (%s), switching to fallback delivery", name)
            self._alive = False
            return False

        log.debug("Ignored status edit error (%s): %s", name, exc)
        return True
