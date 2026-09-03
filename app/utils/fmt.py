"""Human friendly formatting helpers.

Pure functions, no Telegram / network dependency — easy to unit test.
"""

from __future__ import annotations

import math
import re
from datetime import timedelta

__all__ = [
    "human_size",
    "human_speed",
    "format_eta",
    "format_duration",
    "progress_bar",
    "safe_filename",
    "ellipsis",
    "html_escape",
]

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

# Unicode eighth-block characters give a smooth looking bar.
_BLOCK_STEPS = " ▏▎▍▌▋▊▉█"


def human_size(num_bytes: float | int, precision: int = 1) -> str:
    """Return ``1073741824`` as ``1.0 GB``."""
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        return "—"

    if value <= 0:
        return "0 B"
    if value < 1024:
        return f"{int(value)} B"

    unit = min(int(math.floor(math.log(value, 1024))), len(_UNITS) - 1)
    scaled = value / (1024**unit)
    if scaled >= 100:
        return f"{scaled:.0f} {_UNITS[unit]}"
    return f"{scaled:.{precision}f} {_UNITS[unit]}"


def human_speed(bytes_per_second: float) -> str:
    """Return ``15518925`` as ``14.8 MB/s``."""
    if not bytes_per_second or bytes_per_second < 0:
        return "0 KB/s"
    return f"{human_size(bytes_per_second)}/s"


def format_eta(seconds: float | int | None) -> str:
    """Return ``252`` as ``04:12``; unknown values render as ``--:--``."""
    if seconds is None:
        return "--:--"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "--:--"
    if total < 0:
        return "--:--"
    return format_duration(total)


def format_duration(seconds: int) -> str:
    """``HH:MM:SS`` when an hour or more has passed, otherwise ``MM:SS``."""
    seconds = max(0, int(seconds))
    td = timedelta(seconds=seconds)
    hours, remainder = divmod(int(td.total_seconds()), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def progress_bar(fraction: float, width: int = 16) -> str:
    """Render a fractional unicode progress bar, e.g. ``████████▌░░░░░░░``."""
    width = max(4, int(width))
    try:
        value = min(1.0, max(0.0, float(fraction)))
    except (TypeError, ValueError):
        value = 0.0

    if value <= 0:
        return "░" * width

    exact = value * width
    full = int(exact)
    remainder = exact - full

    bar = "█" * full
    if full < width:
        step = int(round(remainder * (len(_BLOCK_STEPS) - 1)))
        bar += _BLOCK_STEPS[step]
        bar += "░" * (width - full - 1)
    return bar


_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\/*?:<>|]+')


def safe_filename(name: str | None, fallback: str = "video.mp4") -> str:
    """Sanitise a filename so it is safe inside a multipart header."""
    if not name:
        return fallback
    cleaned = _UNSAFE.sub("", str(name)).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > 120:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 8:
            cleaned = f"{stem[: 120 - len(ext) - 1]}.{ext}"
        else:
            cleaned = cleaned[:120]
    return cleaned or fallback


def ellipsis(text: str, limit: int = 42) -> str:
    """Shorten ``text`` keeping the tail (file names matter at the end)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    keep = limit - 1
    head = max(0, keep - 12)
    return f"{text[:head]}…{text[-(keep - head):]}"


_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def html_escape(text: str | None) -> str:
    """Escape text for Telegram HTML parse mode."""
    if not text:
        return ""
    out = str(text)
    for char, entity in _ESCAPES.items():
        out = out.replace(char, entity)
    return out
