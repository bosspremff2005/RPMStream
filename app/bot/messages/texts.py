"""Every text the bot can send.

All templates are HTML (that is what makes animated emoji and formatting work)
and every user supplied value is escaped before it is interpolated.
"""

from __future__ import annotations

from typing import Any
from collections.abc import Iterable

from app.bot.branding import CREATOR, CREATOR_LINKS, PROJECT_COPYRIGHT
from app.queue.upload_queue import JobState, QueueSnapshot, UploadJob
from app.rpmshare.client import FileLinks
from app.ui.animations import emoji, separator
from app.utils.fmt import ellipsis, format_duration, html_escape, human_size, human_speed

__all__ = [
    "welcome",
    "help_text",
    "about",
    "creator_screen",
    "unsupported",
    "not_allowed",
    "queue_full",
    "queue_screen",
    "cancel_confirm",
    "cancelled",
    "success",
    "failure",
    "details",
    "link_reveal",
    "cancel_done",
]


def _line(text: str = "") -> str:
    return text


def _banner(title: str, subtitle: str = "", *, icon: str = "🌊", animated: str = "") -> str:
    head = emoji(icon, animated) if icon else ""
    parts = [f"<b>{separator}</b>"]
    if head:
        parts.append(f"{head} <b>{html_escape(title)}</b>")
    else:
        parts.append(f"<b>{html_escape(title)}</b>")
    if subtitle:
        parts.append(f"<i>{subtitle}</i>")
    parts.append(f"<b>{separator}</b>")
    return "\n".join(parts)


# ----------------------------------------------------------------------
def welcome(*, user_name: str, bot_title: str = "RPMStream", tagline: str = "", animated: str = "") -> str:
    name = html_escape(user_name or "there")
    return "\n".join(
        [
            _banner(bot_title, html_escape(tagline) or "Telegram → RPMShare Streaming", animated=animated),
            "",
            f"👋 Welcome, <b>{name}</b>!",
            "",
            "Send or forward me any video and I will stream it straight to "
            "RPMShare — chunk by chunk, without ever filling up a disk.",
            "",
            "🎬 <b>What I accept</b>",
            "• Videos and animations",
            "• Videos sent as documents 📄",
            "• Forwarded media from any chat 🔁",
            "• Very large files — streamed, never buffered",
            "",
            f"<i>{PROJECT_COPYRIGHT}</i>",
            f"<b>{separator}</b>",
            "🔘 Choose an option below",
        ]
    )


def help_text(*, bot_title: str = "RPMStream") -> str:
    return "\n".join(
        [
            _banner(bot_title, "How it works"),
            "",
            "<b>1️⃣ Send a video</b> — or forward one, or send it as a document.",
            "<b>2️⃣ Watch the stream</b> — one live message shows speed, ETA and progress.",
            "<b>3️⃣ Get the result</b> — buttons open the player, never a wall of links.",
            "",
            "🌊 <b>Under the hood</b>",
            "Telegram → chunk → chunk → RPMShare. The file is read in small pieces and "
            "pushed onward immediately, so a 30 GB movie uses only a few MB of memory "
            "and zero permanent disk space.",
            "",
            "🔁 <b>Reliability</b>",
            "Temporary network problems are retried automatically with smart backoff.",
            "❌ <b>Cancel</b> stops a transfer instantly and cleans up.",
            "",
            "🔘 <b>Buttons</b>",
            "• 📊 Queue — everything running and waiting",
            "• 🔄 Refresh — update the status right now",
            "• ❌ Cancel — stop an upload",
            "• 👨‍💻 Creator — official links",
            "",
            f"<b>{separator}</b>",
        ]
    )


def about(*, bot_title: str = "RPMStream", tagline: str = "", creator: str = CREATOR, animated: str = "") -> str:
    return "\n".join(
        [
            f"<b>{separator}</b>",
            f"🤖 <b>{html_escape(bot_title)}</b>",
            f"🌊 {html_escape(tagline or 'Telegram → RPMShare Streaming')}",
            "⚡ Fast • Interactive • Lightweight",
            f"<b>{separator}</b>",
            "👨‍💻 <b>Created &amp; Developed by</b>",
            f"✨ <b>{html_escape(creator)}</b>",
            f"<b>{separator}</b>",
            "🔘 Choose an option below",
        ]
    )


def creator_screen(*, creator: str = CREATOR) -> str:
    links = "  ".join(f"• {item['short']}" for item in CREATOR_LINKS)
    return "\n".join(
        [
            _banner("Creator", "Official links"),
            "",
            "👨‍💻 <b>Created &amp; Developed by</b>",
            f"✨ <b>{html_escape(creator)}</b>",
            "",
            f"<i>{html_escape(PROJECT_COPYRIGHT)}</i>",
            "",
            "🌐 <b>Official destinations</b>",
            f"<i>{html_escape(links)}</i>",
            "",
            "🔗 Tap a button below to open one.",
            f"<b>{separator}</b>",
        ]
    )


# ----------------------------------------------------------------------
def unsupported(*, bot_title: str = "RPMStream") -> str:
    return "\n".join(
        [
            _banner(bot_title, "", icon="🚫"),
            "",
            "🚫 <b>Unsupported media</b>",
            "",
            "I can only stream video files to RPMShare.",
            "",
            "✅ <b>Try one of these</b>",
            "• 🎬 A regular video",
            "• 📄 A video sent as a document",
            "• 🔁 Forward the media from another chat",
            "",
            f"<b>{separator}</b>",
        ]
    )


def not_allowed() -> str:
    return "\n".join(
        [
            _banner("Access Restricted", "", icon="🔒"),
            "",
            "This bot is running in private mode.",
            "Ask the owner to add your Telegram user id to <code>ALLOWED_USERS</code>.",
            f"<b>{separator}</b>",
        ]
    )


def queue_full(*, limit: int) -> str:
    return "\n".join(
        [
            _banner("Queue Full", "", icon="📥"),
            "",
            f"The queue already holds <b>{limit}</b> uploads.",
            "Please wait for a slot and send the video again.",
            f"<b>{separator}</b>",
        ]
    )


# ----------------------------------------------------------------------
def queue_screen(snapshot: QueueSnapshot, *, page: int = 0, per_page: int = 6, bot_title: str = "RPMStream") -> tuple[str, int]:
    """Render the queue; returns ``(text, page_count)``."""
    jobs = list(snapshot.active) + list(snapshot.waiting)
    pages = max(1, -(-len(jobs) // per_page))
    page = max(0, min(page, pages - 1))
    start = page * per_page
    window = jobs[start : start + per_page]

    lines = [_banner(f"{bot_title} Queue", "Live upload pipeline"), ""]

    if not jobs:
        lines += ["😌 <b>The queue is empty.</b>", "", "Send me a video to get started 🎬", ""]
    else:
        for job in window:
            lines.append(_job_line(job, jobs.index(job) + 1))
            lines.append("")

    stats = (
        f"📈 Done <b>{snapshot.processed}</b> · ❌ Failed <b>{snapshot.failed}</b> · "
        f"🛑 Cancelled <b>{snapshot.cancelled}</b>"
    )
    lines += [
        f"<i>⚙️ {snapshot.workers} worker(s) · {len(snapshot.active)} active · {len(snapshot.waiting)} waiting</i>",
        stats,
    ]
    if pages > 1:
        lines.append(f"📄 Page {page + 1}/{pages}")
    lines.append(f"<b>{separator}</b>")
    return "\n".join(lines), pages


def _job_line(job: UploadJob, position: int) -> str:
    progress = job.progress
    name = html_escape(ellipsis(job.file_name, 34))
    if job.state is JobState.RUNNING:
        return (
            f"⚡ <b>#{position}</b> <code>{name}</code>\n"
            f"    <b>{progress.percent}%</b> · {human_size(progress.transferred)}/{human_size(job.size)} · {human_speed(progress.speed)}"
        )
    if job.state is JobState.QUEUED:
        return f"⏳ <b>#{position}</b> <code>{name}</code>\n    {human_size(job.size)} · waiting"
    icon = {JobState.DONE: "✅", JobState.FAILED: "❌", JobState.CANCELLED: "🛑"}.get(job.state, "•")
    return f"{icon} <b>#{position}</b> <code>{name}</code>\n    {job.state.value}"


# ----------------------------------------------------------------------
def cancel_confirm(*, file_name: str, size: int) -> str:
    return "\n".join(
        [
            _banner("Cancel Upload?", "", icon="⚠️"),
            "",
            f"📁 <code>{html_escape(ellipsis(file_name, 42))}</code>",
            f"📦 {human_size(size)}",
            "",
            "The transfer stops immediately and temporary data is cleaned up.",
            f"<b>{separator}</b>",
        ]
    )


def cancel_done(*, file_name: str) -> str:
    return "\n".join(
        [
            _banner("Upload Cancelled", "", icon="🛑"),
            "",
            f"📁 <code>{html_escape(ellipsis(file_name, 42))}</code>",
            "🧹 Resources cleaned up.",
            f"<b>{separator}</b>",
        ]
    )


def cancelled(*, file_name: str) -> str:
    return cancel_done(file_name=file_name)


# ----------------------------------------------------------------------
def success(*, file_name: str, size: int, elapsed: float, links: FileLinks, job_id: str, bot_title: str = "RPMStream") -> str:
    lines = [
        f"<b>{separator}</b>",
        "🎉 <b>UPLOAD COMPLETE!</b>",
        "",
        f"📁 <code>{html_escape(ellipsis(file_name, 46))}</code>",
        f"📦 {human_size(size)}",
        f"⏱️ {format_duration(int(elapsed))}",
        f"🆔 <code>{html_escape(job_id)}</code>",
        "",
        "✨ Successfully transferred to RPMShare",
        f"<b>{separator}</b>",
    ]
    return "\n".join(lines)


def failure(*, file_name: str, reason: str, attempts: int, retryable: bool = True) -> str:
    lines = [
        f"<b>{separator}</b>",
        "⚠️ <b>Upload Failed</b>",
        "",
        f"📁 <code>{html_escape(ellipsis(file_name, 46))}</code>",
        html_escape(reason or "Something interrupted the transfer."),
    ]
    if retryable:
        lines.append("<i>The system retries temporary errors automatically.</i>")
    if attempts > 1:
        lines.append(f"<i>🔁 Attempts made: {attempts}</i>")
    lines += ["", f"<b>{separator}</b>"]
    return "\n".join(lines)


# ----------------------------------------------------------------------
def details(job: UploadJob, *, account: Any | None = None) -> str:
    progress = job.progress
    source = job.source
    lines = [
        _banner("Upload Details", ""),
        "",
        f"📁 <b>File:</b> <code>{html_escape(ellipsis(job.file_name, 46))}</code>",
        f"📦 <b>Size:</b> {human_size(job.size)}",
    ]
    if getattr(source, "duration", 0):
        lines.append(f"⏱️ <b>Duration:</b> {format_duration(int(source.duration))}")
    if getattr(source, "width", 0) and getattr(source, "height", 0):
        lines.append(f"📐 <b>Resolution:</b> {source.width}×{source.height}")
    if getattr(source, "mime_type", ""):
        lines.append(f"🧬 <b>Type:</b> <code>{html_escape(source.mime_type)}</code>")
    lines += [
        f"🆔 <b>Job:</b> <code>{html_escape(job.id)}</code>",
        f"🧭 <b>State:</b> {job.state.value} · stage {progress.stage.value}",
        f"🔁 <b>Attempts:</b> {job.attempts}",
        f"⏱️ <b>Elapsed:</b> {format_duration(int(job.elapsed))}",
        f"📶 <b>Speed:</b> {human_speed(progress.speed)}",
    ]
    if progress.file_code:
        lines.append(f"🔑 <b>RPMShare code:</b> <code>{html_escape(progress.file_code)}</code>")
    if account is not None:
        lines += [
            "",
            f"👤 <b>Account:</b> <code>{html_escape(account.login or 'unknown')}</code>",
            f"💾 <b>Storage left:</b> {human_size(account.storage_left)}",
            f"🗂️ <b>Files:</b> {account.files_total}",
        ]
    lines += ["", f"<b>{separator}</b>"]
    return "\n".join(lines)


def link_reveal(*, links: FileLinks) -> str:
    rows = [
        _banner("Links", "Copy friendly", icon="📋"),
        "",
        f"🔑 <b>File code:</b> <code>{html_escape(links.file_code)}</code>",
    ]
    if links.watch_url:
        rows.append(f"🎬 <code>{html_escape(links.watch_url)}</code>")
    if links.hls_url:
        rows.append(f"📺 <code>{html_escape(links.hls_url)}</code>")
    for name, url in links.qualities.items():
        rows.append(f"📥 <b>{html_escape(name or '?')}</b> <code>{html_escape(url)}</code>")
    rows += ["", f"<b>{separator}</b>"]
    return "\n".join(rows)


def cancelled_job(job: UploadJob) -> str:  # pragma: no cover - alias used by handlers
    return cancel_done(file_name=job.file_name)


def render_job_list(jobs: Iterable[UploadJob]) -> str:  # pragma: no cover - helper
    return "\n".join(_job_line(job, i + 1) for i, job in enumerate(jobs))
