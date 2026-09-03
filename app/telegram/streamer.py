"""Chunk based reading of Telegram files over MTProto.

The whole point of RPMStream: a chunk is fetched from Telegram with
``upload.getFile`` and handed straight to RPMShare before the next one is read.
The file therefore never lands on disk and memory stays flat regardless of
whether the video is 20 MB or 30 GB.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any
from collections.abc import AsyncIterator, Callable, Iterable

from pyrogram import raw
from pyrogram.crypto import aes
from pyrogram.errors import RPCError
from pyrogram.file_id import FileId, FileType
from pyrogram.raw.functions.upload import GetCdnFile, GetCdnFileHashes, GetFile, ReuploadCdnFile
from pyrogram.raw.types import InputDocumentFileLocation, InputPhotoFileLocation

from app.utils.errors import MediaError, TelegramTransferError, UploadCancelled
from app.utils.fmt import safe_filename
from app.utils.logger import get_logger

__all__ = ["MediaSource", "TelegramChunkStreamer", "detect_media", "FileReferenceExpired", "VIDEO_EXTENSIONS"]

log = get_logger("telegram.streamer")

#: ``upload.getFile`` refuses limits that are not multiples of 4 KiB.
CHUNK_ALIGNMENT = 4096

VIDEO_EXTENSIONS = frozenset(
    {
        "mp4", "mkv", "mov", "avi", "webm", "m4v", "flv", "wmv", "mpg", "mpeg",
        "m2ts", "ts", "3gp", "3g2", "ogv", "vob", "rm", "rmvb", "asf", "divx", "f4v",
    }
)


class FileReferenceExpired(TelegramTransferError):
    """Telegram rotated the file reference — the caller must re-resolve the media."""

    retryable = True
    user_message = "Telegram expired the file reference."


@dataclass
class MediaSource:
    """A Telegram media object distilled to everything the pipeline needs."""

    file_id: str
    file_unique_id: str
    file_name: str
    size: int
    mime_type: str = "video/mp4"
    kind: str = "video"  # video | animation | document | photo
    duration: int = 0
    width: int = 0
    height: int = 0
    chat_id: int = 0
    message_id: int = 0

    @property
    def is_video(self) -> bool:
        return self.kind in {"video", "animation"} or (self.mime_type or "").startswith("video/")

    def describe(self) -> str:
        return f"{self.file_name} ({self.size} bytes, {self.mime_type})"


def _extension(name: str) -> str:
    return (name.rsplit(".", 1)[-1] if "." in name else "").lower()


def detect_media(message: Any, *, allow_any_document: bool = True) -> MediaSource | None:
    """Extract a :class:`MediaSource` from a Telegram message.

    Handles normal videos, forwarded media, animations and videos sent as
    documents. Returns ``None`` for anything that cannot be streamed.
    """
    if message is None or getattr(message, "media", None) is None:
        return None

    chat_id = getattr(getattr(message, "chat", None), "id", 0) or 0
    message_id = getattr(message, "id", 0) or 0

    def build(obj: Any, kind: str, default_name: str) -> MediaSource | None:
        file_id = getattr(obj, "file_id", None)
        if not file_id:
            return None
        name = getattr(obj, "file_name", None) or default_name
        return MediaSource(
            file_id=file_id,
            file_unique_id=getattr(obj, "file_unique_id", "") or "",
            file_name=safe_filename(name, default_name),
            size=int(getattr(obj, "file_size", 0) or 0),
            mime_type=getattr(obj, "mime_type", None) or "application/octet-stream",
            kind=kind,
            duration=int(getattr(obj, "duration", 0) or 0),
            width=int(getattr(obj, "width", 0) or 0),
            height=int(getattr(obj, "height", 0) or 0),
            chat_id=chat_id,
            message_id=message_id,
        )

    video = getattr(message, "video", None)
    if video is not None:
        return build(video, "video", f"video_{getattr(video, 'file_unique_id', 'x')}.mp4")

    animation = getattr(message, "animation", None)
    if animation is not None:
        return build(animation, "animation", f"animation_{getattr(animation, 'file_unique_id', 'x')}.mp4")

    document = getattr(message, "document", None)
    if document is not None:
        mime = (getattr(document, "mime_type", "") or "").lower()
        name = getattr(document, "file_name", "") or ""
        looks_like_video = mime.startswith("video/") or _extension(name) in VIDEO_EXTENSIONS
        if not looks_like_video and not allow_any_document:
            return None
        return build(document, "video" if looks_like_video else "document", f"file_{getattr(document, 'file_unique_id', 'x')}")

    # Photos, stickers, audio and the rest are not accepted: RPMShare is a video
    # host, so the handler shows the friendly "unsupported media" screen instead.
    return None


def _build_location(file_id: FileId) -> Any:
    """Turn a decoded Telegram file id into an MTProto input file location."""
    if file_id.file_type == FileType.PHOTO:
        return InputPhotoFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )
    return InputDocumentFileLocation(
        id=file_id.media_id,
        access_hash=file_id.access_hash,
        file_reference=file_id.file_reference,
        thumb_size=file_id.thumbnail_size,
    )


class TelegramChunkStreamer:
    """Yields a Telegram file chunk by chunk.

    Only one chunk is held in memory at a time, which gives natural
    backpressure: if RPMShare is slow, Telegram simply is not asked for more.
    """

    def __init__(self, client: Any, *, chunk_size: int = 1024 * 1024, max_parallel: int = 3, sleep_threshold: int = 30) -> None:
        self._client = client
        remainder = max(CHUNK_ALIGNMENT, int(chunk_size)) % CHUNK_ALIGNMENT
        if remainder:
            chunk_size -= remainder
        self._chunk_size = max(CHUNK_ALIGNMENT, min(1024 * 1024, int(chunk_size)))
        self._semaphore = asyncio.Semaphore(max(1, max_parallel))
        self._sleep_threshold = sleep_threshold

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    # ------------------------------------------------------------------
    async def stream(
        self,
        source: MediaSource,
        *,
        offset: int = 0,
        cancel: asyncio.Event | None = None,
        on_chunk: Callable[[int], Any] | None = None,
    ) -> AsyncIterator[bytes]:
        """Async generator of raw file bytes, starting at ``offset``."""
        try:
            file_id = FileId.decode(source.file_id)
        except Exception as exc:  # noqa: BLE001 - pyrogram raises ValueError/RuntimeError
            raise MediaError(f"Could not decode Telegram file id: {exc}") from exc

        location = _build_location(file_id)
        limit = self._chunk_size
        cursor = max(0, int(offset))
        remaining = source.size - cursor if source.size > 0 else None
        callback_async = on_chunk is not None and inspect.iscoroutinefunction(on_chunk)

        async with self._semaphore:
            session = await self._client.get_session(file_id.dc_id, is_media=True)
            log.debug("Streaming %s from DC%s with %s byte chunks", source.file_name, file_id.dc_id, limit)

            while True:
                self._check_cancel(cancel)

                try:
                    result = await session.invoke(
                        GetFile(location=location, offset=cursor, limit=limit, precise=True),
                        sleep_threshold=self._sleep_threshold,
                    )
                except RPCError as exc:
                    raise self._map_rpc_error(exc) from exc

                if isinstance(result, raw.types.upload.File):
                    chunk = result.bytes
                    if not chunk:
                        break
                    yield chunk
                    if on_chunk is not None:
                        if callback_async:
                            await on_chunk(len(chunk))
                        else:
                            on_chunk(len(chunk))
                    cursor += len(chunk)
                    if len(chunk) < limit:
                        break
                    if remaining is not None and cursor >= source.size:
                        break
                    continue

                if isinstance(result, raw.types.upload.FileCdnRedirect):
                    async for chunk in self._stream_cdn(session, result, cursor, limit, remaining, cancel, on_chunk, callback_async):
                        yield chunk
                    return

                raise TelegramTransferError(f"Unexpected upload.getFile response: {type(result).__name__}")

    # ------------------------------------------------------------------
    async def _stream_cdn(
        self,
        session: Any,
        redirect: Any,
        cursor: int,
        limit: int,
        remaining: int | None,
        cancel: asyncio.Event | None,
        on_chunk: Callable[[int], Any] | None,
        callback_async: bool,
    ) -> AsyncIterator[bytes]:
        """Read from a Telegram CDN data centre, decrypting each chunk."""
        log.info("Telegram redirected this file to CDN DC%s", redirect.dc_id)
        cdn_session = await self._client.get_session(redirect.dc_id, is_cdn=True, temporary=True)

        while True:
            self._check_cancel(cancel)

            result = await cdn_session.invoke(GetCdnFile(file_token=redirect.file_token, offset=cursor, limit=limit))

            if isinstance(result, raw.types.upload.CdnFileReuploadNeeded):
                await session.invoke(ReuploadCdnFile(file_token=redirect.file_token, request_token=result.request_token))
                continue

            chunk = result.bytes
            if not chunk:
                break

            iv = bytearray(redirect.encryption_iv[:-4] + (cursor // 16).to_bytes(4, "big"))
            decrypted = await asyncio.get_running_loop().run_in_executor(
                None, aes.ctr256_decrypt, chunk, redirect.encryption_key, iv
            )
            await self._verify_hashes(session, redirect, cursor, decrypted)

            yield decrypted
            if on_chunk is not None:
                if callback_async:
                    await on_chunk(len(decrypted))
                else:
                    on_chunk(len(decrypted))

            cursor += len(decrypted)
            if len(decrypted) < limit:
                break
            if remaining is not None and cursor >= remaining:
                break

    @staticmethod
    async def _verify_hashes(session: Any, redirect: Any, offset: int, chunk: bytes) -> None:
        """Compare the chunk against the CDN file hashes (mirrors Telegram's spec)."""
        from hashlib import sha256

        try:
            hashes = await session.invoke(GetCdnFileHashes(file_token=redirect.file_token, offset=offset))
        except RPCError as exc:  # pragma: no cover - depends on live CDN behaviour
            log.debug("CDN hash verification unavailable: %s", exc)
            return
        for index, entry in enumerate(hashes):
            start, stop = entry.limit * index, entry.limit * (index + 1)
            if start >= len(chunk):
                break
            if entry.hash != sha256(chunk[start:stop]).digest():
                raise TelegramTransferError("CDN file hash mismatch — refusing to upload corrupted data")

    # ------------------------------------------------------------------
    @staticmethod
    def _check_cancel(cancel: asyncio.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise UploadCancelled("Upload cancelled by the user")

    @staticmethod
    def _map_rpc_error(exc: RPCError) -> Exception:
        """Translate Telegram RPC errors into the RPMStream error hierarchy."""
        name = type(exc).__name__
        text = str(exc)

        if name == "FloodWait":
            delay = float(getattr(exc, "value", 5) or 5)
            log.warning("Telegram FloodWait for %.1fs while reading the file", delay)
            return TelegramTransferError(f"FloodWait {delay}s")
        if name in {"FileReferenceExpired", "FILE_REFERENCE_EXPIRED"} or "FILE_REFERENCE_EXPIRED" in text:
            return FileReferenceExpired("Telegram file reference expired")
        if name in {"FileIdInvalid", "FILE_ID_INVALID"}:
            return MediaError("Telegram rejected this file id", user_message="That file can no longer be read from Telegram.")
        if name in {"FileTooLarge", "FILE_TOO_LARGE"}:
            return MediaError("File is too large for Telegram to serve", user_message="That file is too large to stream.")
        if name in {"AuthKeyUnregistered", "AuthKeyDuplicated", "SessionRevoked"}:
            return MediaError(f"Telegram session problem: {name}", user_message="The bot lost its Telegram session.")
        return TelegramTransferError(f"{name}: {text}")


def iterable_media(message: Any) -> Iterable[Any]:
    """Small helper for handlers that need to look at albums/captions."""
    return [message]
