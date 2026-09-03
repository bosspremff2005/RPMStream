"""Project identity and the creator's official links.

These URLs are only ever attached to inline buttons — they are never dumped as
raw text inside a chat message.
"""

from __future__ import annotations

from typing import Any

__all__ = ["CREATOR", "CREATOR_LINKS", "PROJECT_COPYRIGHT", "PROJECT_NAME", "PROJECT_TAGLINE"]

PROJECT_NAME = "RPMStream"
PROJECT_TAGLINE = "Telegram → RPMShare Streaming"
CREATOR = "Woojoo"
PROJECT_COPYRIGHT = "© Prem ChandraVanshi — RPMStream"

#: Official links, in the order the About screen renders them.
CREATOR_LINKS: tuple[dict[str, str], ...] = (
    {"label": "👨‍💻 Developer Profile", "url": "https://t.me/bosspremff", "short": "🌐 Owner"}
)


def creator_badges() -> list[str]:
    """Markdown badges used by the README creator section."""
    return [f"[{link['label']}]({link['url']})" for link in CREATOR_LINKS]


def as_dict() -> dict[str, Any]:  # pragma: no cover - convenience for tools
    return {
        "project": PROJECT_NAME,
        "tagline": PROJECT_TAGLINE,
        "creator": CREATOR,
        "copyright": PROJECT_COPYRIGHT,
        "links": list(CREATOR_LINKS),
    }
