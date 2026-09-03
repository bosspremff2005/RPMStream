"""Live upload progress rendering.

Everything here is pure text building — the actual (throttled) Telegram edits
live in :mod:`app.ui.status`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ui.animations import Stage, animate, emoji, separator, stage_timeline
from app.utils.fmt import ellipsis, format_eta, html_escape, human_size, human_speed, progress_bar

__all__ = ["UploadProgress", "render_progress", "render_queue_added", "render_queued_line"]


@dataclass
class UploadProgress:
    """Mutable snapshot of a single upload, shared by the workers and the UI."""

    job_id: str
    file_name: str
    total: int = 0
    transferred: int = 0
    speed: float = 0.0
    stage: Stage = Stage.STARTING
    attempt: int = 1
    max_retries: int = 3
    elapsed: float = 0.0
    queue_position: int | None = None
    queue_length: int = 0
    detail: str = ""
    error: str = ""
    file_code: str = ""
    bytes_per_chunk: int = 0

    # ------------------------------------------------------------------
    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.transferred / self.total)

    @property
    def percent(self) -> int:
        """Rounded percentage that never claims 100% before the last byte."""
        if self.total <= 0:
            return 0
        value = round(self.fraction * 100)
        if value >= 100 and self.transferred < self.total:
            return 99
        return value

    @property
    def eta(self) -> float | None:
        if self.speed <= 0 or self.total <= 0:
            return None
        remaining = max(0, self.total - self.transferred)
        return remaining / self.speed

    def advance(self, byte_count: int) -> None:
        """Record ``byte_count`` freshly streamed bytes (clamped to the total)."""
        self.transferred += max(0, int(byte_count))
        if self.total:
            self.transferred = min(self.transferred, self.total)


def render_progress(
    progress: UploadProgress,
    *,
    tick: int = 0,
    bar_width: int = 16,
    bot_title: str = "RPMStream",
    animated_emoji_id: str = "",
) -> str:
    """Build the single status message that is edited while an upload runs."""
    name = html_escape(ellipsis(progress.file_name, 46))
    bar = progress_bar(progress.fraction, bar_width)
    wave = emoji("🌊", animated_emoji_id)

    lines = [
        f"<b>{separator}</b>",
        f"{wave} <b>{html_escape(bot_title)}</b>",
        f"📁 <code>{name}</code>",
        "",
        f"<code>{bar}</code> <b>{progress.percent}%</b>",
        "",
        f"📦 {human_size(progress.transferred)} / {human_size(progress.total)}",
        f"⚡ {human_speed(progress.speed)}",
        f"⏳ ETA: {format_eta(progress.eta)}",
    ]

    timeline = stage_timeline(progress.stage)
    if timeline:
        lines += ["", timeline]

    lines += ["", animate(progress.stage, tick)]

    if progress.attempt > 1:
        lines.append(f"<i>🔁 Retry {progress.attempt - 1}/{progress.max_retries}</i>")
    if progress.detail:
        lines.append(f"<i>{html_escape(progress.detail)}</i>")

    lines.append(f"<b>{separator}</b>")
    return "\n".join(lines)


def render_queued_line(position: int, total: int) -> str:
    """One line describing where a job sits in the queue."""
    if total > 1:
        return f"⏳ Queue position: <b>#{position}</b> of {total}"
    return f"⏳ Queue position: <b>#{position}</b>"


def render_queue_added(
    *,
    job_id: str,
    file_name: str,
    size: int,
    position: int,
    queue_length: int,
    bot_title: str = "RPMStream",
    animated_emoji_id: str = "",
) -> str:
    """The screen shown the moment a video enters the queue."""
    wave = emoji("🌊", animated_emoji_id)
    return "\n".join(
        [
            f"<b>{separator}</b>",
            f"{wave} <b>{html_escape(bot_title)}</b>",
            "📥 <b>Added to Queue</b>",
            "",
            f"📁 <b>Filename:</b> <code>{html_escape(ellipsis(file_name, 46))}</code>",
            f"📦 <b>Size:</b> {human_size(size)}",
            f"🆔 <b>Job:</b> <code>{html_escape(job_id)}</code>",
            render_queued_line(position, queue_length),
            "",
            "<i>You will get live updates below 👇</i>",
            f"<b>{separator}</b>",
        ]
    )
