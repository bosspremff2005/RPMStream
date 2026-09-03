"""Every inline keyboard in the bot.

The whole interface is button driven: callback data uses short, namespaced
prefixes (Telegram caps the payload at 64 bytes) and *all* destinations —
including the creator's links — live behind URL buttons instead of raw text.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.branding import CREATOR_LINKS
from app.rpmshare.client import FileLinks

__all__ = [
    "Callback",
    "home_keyboard",
    "status_keyboard",
    "queue_keyboard",
    "queue_navigation",
    "cancel_confirm_keyboard",
    "success_keyboard",
    "error_keyboard",
    "about_keyboard",
    "creator_links_keyboard",
    "link_reveal_keyboard",
    "details_keyboard",
    "row",
]


class Callback:
    """Callback data prefixes shared by the keyboards and the dispatcher."""

    HOME = "home"
    HELP = "help"
    ABOUT = "about"
    CREATOR = "creator"
    QUEUE = "queue"  # queue:<page>
    STATUS = "status"  # status:<job_id>
    DETAILS = "details"  # details:<job_id>
    CANCEL = "cancel"  # cancel:<job_id>
    CANCEL_YES = "cyes"  # cyes:<job_id>
    CANCEL_NO = "cno"  # cno:<job_id>
    RETRY = "retry"  # retry:<job_id>
    LINK = "link"  # link:<job_id>
    CLOSE = "close"  # close[:<message-scope>]
    NOOP = "noop"


def cb(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def url(label: str, link: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, url=link)


def row(*buttons: InlineKeyboardButton) -> list[InlineKeyboardButton]:
    return list(buttons)


def build(*rows: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def chunked(items: Sequence, size: int = 2) -> list[list]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


# ----------------------------------------------------------------------
def home_keyboard(*, queue_count: int = 0) -> InlineKeyboardMarkup:
    label = f"📊 Queue ({queue_count})" if queue_count else "📊 Queue"
    return build(
        row(cb("🚀 How It Works", Callback.HELP)),
        row(cb(label, f"{Callback.QUEUE}:0"), cb("ℹ️ About", Callback.ABOUT)),
        row(cb("👨‍💻 Creator", Callback.CREATOR)),
    )


def status_keyboard(job_id: str, *, running: bool = True) -> InlineKeyboardMarkup:
    rows = [row(cb("🔄 Refresh Status", f"{Callback.STATUS}:{job_id}"))]
    if running:
        rows.append(row(cb("❌ Cancel Upload", f"{Callback.CANCEL}:{job_id}")))
    rows.append(row(cb("📊 Queue", f"{Callback.QUEUE}:0"), cb("🏠 Home", Callback.HOME)))
    return build(*rows)


def queue_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    return build(row(cb("🔄 Refresh", f"{Callback.QUEUE}:{page}"), cb("🏠 Home", Callback.HOME)))


def queue_navigation(page: int, pages: int) -> InlineKeyboardMarkup:
    nav: list[InlineKeyboardButton] = []
    if pages > 1:
        nav.append(cb("◀️ Previous", f"{Callback.QUEUE}:{max(0, page - 1)}"))
    nav.append(cb("🔄 Refresh", f"{Callback.QUEUE}:{page}"))
    if pages > 1:
        nav.append(cb("Next ▶️", f"{Callback.QUEUE}:{min(pages - 1, page + 1)}"))
    return build(row(*nav), row(cb("🏠 Home", Callback.HOME)))


def cancel_confirm_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return build(
        row(cb("✅ Yes, Cancel", f"{Callback.CANCEL_YES}:{job_id}")),
        row(cb("↩️ Go Back", f"{Callback.STATUS}:{job_id}")),
    )


def success_keyboard(links: FileLinks, job_id: str) -> InlineKeyboardMarkup:
    """Buttons only for URLs RPMShare actually returned."""
    buttons = links.as_buttons()
    rows: list[list[InlineKeyboardButton]] = []
    if buttons:
        # First link gets its own full width row, the rest pair up.
        rows.append(row(url(buttons[0][0], buttons[0][1])))
        for pair in chunked(buttons[1:], 2):
            rows.append(row(*[url(label, link) for label, link in pair]))
    rows.append(row(cb("📋 View Link", f"{Callback.LINK}:{job_id}"), cb("ℹ️ Details", f"{Callback.DETAILS}:{job_id}")))
    rows.append(row(cb("🏠 Home", Callback.HOME), cb("🗑️ Close", f"{Callback.CLOSE}:{job_id}")))
    return build(*rows)


def error_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return build(
        row(cb("🔄 Retry", f"{Callback.RETRY}:{job_id}")),
        row(cb("🏠 Home", Callback.HOME), cb("🗑️ Close", f"{Callback.CLOSE}:{job_id}")),
    )


def link_reveal_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return build(row(cb("↩️ Back to Result", f"{Callback.STATUS}:{job_id}")), row(cb("🗑️ Close", f"{Callback.CLOSE}:{job_id}")))


def details_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return build(row(cb("🔄 Refresh", f"{Callback.DETAILS}:{job_id}"), cb("↩️ Back", f"{Callback.STATUS}:{job_id}")))


def about_keyboard() -> InlineKeyboardMarkup:
    return build(
        row(cb("👨‍💻 Creator Links", Callback.CREATOR)),
        row(cb("📊 Queue", f"{Callback.QUEUE}:0"), cb("🏠 Home", Callback.HOME)),
    )


def creator_links_keyboard() -> InlineKeyboardMarkup:
    """Two link buttons per row, exactly like the brief."""
    rows: list[list[InlineKeyboardButton]] = []
    first = CREATOR_LINKS[0]
    rows.append(row(url(first["label"], first["url"])))
    for pair in chunked(CREATOR_LINKS[1:], 2):
        rows.append(row(*[url(item["label"], item["url"]) for item in pair]))
    rows.append(row(cb("⬅️ Back", Callback.ABOUT)))
    return build(*rows)


def buttons_from(pairs: Iterable[tuple[str, str]]) -> InlineKeyboardMarkup:  # pragma: no cover - helper
    return build(row(*[url(label, link) for label, link in pairs]))
