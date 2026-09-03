"""Logging setup.

Secrets are scrubbed from every record so a leaked log file never leaks a token.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys

__all__ = ["setup_logging", "get_logger"]

_CONFIGURED = False
_IMPLICIT = False  # True while the active config came from an import-time fallback
_OUR_HANDLERS: list[logging.Handler] = []

#: ``api_key=…``, ``token: …``, ``bot_token=…`` — values may contain ':' (bot tokens).
_ASSIGNMENT = re.compile(r"(?i)\b((?:api|bot|access|auth|session)?[_-]?(?:key|token|hash|secret|password))\s*([=:])\s*(\S+)")
#: ``?key=…`` / ``&token=…`` inside a URL.
_QUERY = re.compile(r"(?i)([?&](?:key|token|api_key|access_token)=)([^&\s]+)")


class SecretScrubber(logging.Filter):
    """Replace credential looking substrings with ``***``."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        scrubbed = _QUERY.sub(lambda m: f"{m.group(1)}***", message)
        scrubbed = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}***", scrubbed)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def setup_logging(
    level: str = "INFO",
    log_file: str | None = "logs/rpmstream.log",
    to_file: bool = True,
    *,
    _implicit: bool = False,
) -> None:
    """Configure the root logger.

    Importing the app installs a console-only logger (``_implicit=True``) so that
    simply importing a module never creates files. The first explicit call —
    from :func:`app.main.main` — always wins and may add the rotating file
    handler. Calling it twice explicitly is a no-op.
    """
    global _CONFIGURED, _IMPLICIT

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if _CONFIGURED and not _IMPLICIT:
        return

    for handler in _OUR_HANDLERS:
        root.removeHandler(handler)
    _OUR_HANDLERS.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(SecretScrubber())
    root.addHandler(stream)
    _OUR_HANDLERS.append(stream)

    if to_file and log_file:
        directory = os.path.dirname(log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(SecretScrubber())
        root.addHandler(file_handler)
        _OUR_HANDLERS.append(file_handler)

    # Tame noisy third party loggers.
    for noisy in ("aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    _IMPLICIT = _implicit


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, ensuring logging is configured."""
    if not _CONFIGURED:
        setup_logging(to_file=False, log_file=None, _implicit=True)
    return logging.getLogger(f"rpmstream.{name}")
