"""Shared error hierarchy.

The user interface only ever renders the short ``user_message``; the technical
details stay in the logs.
"""

from __future__ import annotations

__all__ = [
    "RPMStreamError",
    "MediaError",
    "UploadCancelled",
    "TelegramTransferError",
    "RPMShareError",
    "RPMShareTransientError",
    "RPMSharePermanentError",
]


class RPMStreamError(Exception):
    """Base class for every expected failure inside RPMStream."""

    #: Short, friendly text shown to the user.
    user_message = "Something went wrong while streaming your video."
    #: Whether the automatic retry system should try again.
    retryable = False

    def __init__(self, message: str | None = None, *, user_message: str | None = None) -> None:
        super().__init__(message or self.__class__.__name__)
        if user_message:
            self.user_message = user_message


class MediaError(RPMStreamError):
    """The incoming Telegram media cannot be streamed."""

    user_message = "That media could not be read from Telegram."


class UploadCancelled(RPMStreamError):
    """The user cancelled the upload (never retried)."""

    retryable = False
    user_message = "Upload cancelled."


class TelegramTransferError(RPMStreamError):
    """Reading the file from Telegram failed — usually temporary."""

    retryable = True
    user_message = "Telegram interrupted the transfer."


class RPMShareError(RPMStreamError):
    """Base class for RPMShare API failures."""

    def __init__(self, message: str | None = None, *, user_message: str | None = None, status: int | None = None) -> None:
        super().__init__(message, user_message=user_message)
        self.status = status


class RPMShareTransientError(RPMShareError):
    """Network trouble, timeouts, rate limits, 5xx — safe to retry."""

    retryable = True
    user_message = "RPMShare is temporarily unavailable."


class RPMSharePermanentError(RPMShareError):
    """Bad API key, rejected file, out of storage — retrying will not help."""

    retryable = False
    user_message = "RPMShare rejected this upload."
