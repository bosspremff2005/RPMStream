"""Project identity and the creator's official links.

These URLs are only ever attached to inline buttons — they are never dumped as
raw text inside a chat message.
"""

from __future__ import annotations

from typing import Any

__all__ = ["CREATOR", "CREATOR_LINKS", "PROJECT_COPYRIGHT", "PROJECT_NAME", "PROJECT_TAGLINE"]

PROJECT_NAME = "RPMStream"
PROJECT_TAGLINE = "Telegram → RPMShare Streaming"
CREATOR = "Salman Biswas"
PROJECT_COPYRIGHT = "© Salman Biswas — RPMStream"

#: Official links, in the order the About screen renders them.
CREATOR_LINKS: tuple[dict[str, str], ...] = (
    {"label": "👨‍💻 Developer Portfolio", "url": "https://profile.vrozek.xyz/", "short": "🌐 Portfolio"},
    {"label": "💬 Telegram", "url": "https://t.me/Otakuosenpai", "short": "💬 Telegram"},
    {"label": "📢 Channel", "url": "https://t.me/salmandevapp", "short": "📢 Channel"},
    {"label": "🐙 GitHub", "url": "https://github.com/salman-dev-app", "short": "🐙 GitHub"},
    {"label": "📸 Instagram", "url": "https://www.instagram.com/mdsalman.010", "short": "📸 Instagram"},
    {"label": "🛍️ Store", "url": "https://vrozek.xyz/", "short": "🛍️ Store"},
    {"label": "📘 Facebook", "url": "https://facebook.com/salmandevapp", "short": "📘 Facebook"},
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
