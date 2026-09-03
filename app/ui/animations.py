"""Stage visuals for the streaming pipeline.

Two layers of "motion" are used deliberately:

* **Text animation** — a spinner frame appended to the active stage line. It is
  free, instant and never costs an extra Telegram request.
* **Optional media** — animated emoji (``<emoji document_id=...>``, requires a
  Fragment linked bot username) and sticker file ids. Both are *opt-in* through
  the environment so nothing copyrighted is ever hard coded.
"""

from __future__ import annotations

from enum import Enum

from app.utils.fmt import html_escape

__all__ = [
    "Stage",
    "STAGES",
    "stage_label",
    "stage_frame",
    "animate",
    "emoji",
    "separator",
]


class Stage(str, Enum):
    """Ordered pipeline stages shown to the user."""

    QUEUED = "queued"
    STARTING = "starting"
    PREPARING = "preparing"
    CONNECTING = "connecting"
    TRANSFERRING = "transferring"
    PROCESSING = "processing"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return STAGE_META[self]["label"]

    @property
    def icon(self) -> str:
        return STAGE_META[self]["icon"]


STAGE_META: dict[Stage, dict[str, object]] = {
    Stage.QUEUED: {
        "icon": "📥",
        "label": "Waiting in queue…",
        "frames": ("◐", "◓", "◑", "◒"),
        "tone": "pending",
    },
    Stage.STARTING: {
        "icon": "🚀",
        "label": "Starting…",
        "frames": ("▹▹▹▹▹", "▸▹▹▹▹", "▹▸▹▹▹", "▹▹▸▹▹", "▹▹▹▸▹", "▹▹▹▹▸"),
        "tone": "active",
    },
    Stage.PREPARING: {
        "icon": "🌊",
        "label": "Preparing stream…",
        "frames": ("≈≈≈≈≈", "≋≈≈≈≈", "≈≋≈≈≈", "≈≈≋≈≈", "≈≈≈≋≈", "≈≈≈≈≋"),
        "tone": "active",
    },
    Stage.CONNECTING: {
        "icon": "📡",
        "label": "Connecting to Telegram…",
        "frames": ("📡", "📡✨", "📡⚡"),
        "tone": "active",
    },
    Stage.TRANSFERRING: {
        "icon": "⚡",
        "label": "Transferring video…",
        "frames": ("▰▱▱▱", "▰▰▱▱", "▰▰▰▱", "▰▰▰▰", "▱▰▰▰", "▱▱▰▰", "▱▱▱▰"),
        "tone": "active",
    },
    Stage.PROCESSING: {
        "icon": "🎬",
        "label": "Processing upload…",
        "frames": ("🎬", "🎞️", "🎬", "📽️"),
        "tone": "active",
    },
    Stage.FINALIZING: {
        "icon": "✨",
        "label": "Finalizing…",
        "frames": ("✨", "✨💫", "💫✨", "✨⭐"),
        "tone": "active",
    },
    Stage.COMPLETE: {
        "icon": "🎉",
        "label": "Completed!",
        "frames": ("🎉",),
        "tone": "success",
    },
    Stage.FAILED: {
        "icon": "⚠️",
        "label": "Upload failed",
        "frames": ("⚠️",),
        "tone": "error",
    },
    Stage.CANCELLED: {
        "icon": "🛑",
        "label": "Cancelled",
        "frames": ("🛑",),
        "tone": "error",
    },
}

#: Ordered list used to render the stage timeline.
STAGES: tuple[Stage, ...] = (
    Stage.STARTING,
    Stage.PREPARING,
    Stage.CONNECTING,
    Stage.TRANSFERRING,
    Stage.PROCESSING,
    Stage.FINALIZING,
)

separator = "━" * 20


def stage_label(stage: Stage) -> str:
    """``⚡ Transferring video…``"""
    meta = STAGE_META[stage]
    return f"{meta['icon']} {meta['label']}"


def stage_frame(stage: Stage, tick: int) -> str:
    """Current spinner frame for ``stage`` (cycles with ``tick``)."""
    frames = STAGE_META[stage]["frames"]  # type: ignore[index]
    if len(frames) == 1:
        return str(frames[0])
    return str(frames[max(0, int(tick)) % len(frames)])


def animate(stage: Stage, tick: int, *, bold: bool = False) -> str:
    """Stage line including its live spinner frame."""
    line = f"{stage_label(stage)} <i>{stage_frame(stage, tick)}</i>"
    return f"<b>{line}</b>" if bold else line


def emoji(fallback: str, document_id: str = "") -> str:
    """Render an animated custom emoji when configured, else plain ``fallback``.

    ``<emoji document_id="…">`` is only honoured by Telegram for bots that own a
    Fragment username, therefore it is strictly opt-in via ``ANIMATED_EMOJI_ID``.
    """
    document_id = (document_id or "").strip()
    if not document_id.isdigit():
        return fallback
    return f'<emoji document_id="{html_escape(document_id)}">{fallback}</emoji>'


def stage_timeline(current: Stage) -> str:
    """Compact ✅/▶/▫ timeline of the whole pipeline."""
    order = list(STAGES)
    if current not in order:
        return ""
    index = order.index(current)
    parts = []
    for position, stage in enumerate(order):
        if position < index:
            parts.append("✅")
        elif position == index:
            parts.append(stage.icon)
        else:
            parts.append("▫️")
    return " ".join(parts)
