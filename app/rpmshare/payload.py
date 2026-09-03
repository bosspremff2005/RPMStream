"""Streaming ``multipart/form-data`` bodies with a *known* total length.

``aiohttp`` normally sends multipart bodies with chunked transfer encoding,
which many PHP upload endpoints reject. RPMStream knows the exact file size up
front (Telegram tells us), so the frame around the file is computed byte exact
and the file itself is streamed through untouched:

    ┌ head: boundary + fields + file part header ┐
    │            streamed Telegram chunks        │  ← never touches disk
    └ tail: CRLF + closing boundary              ┘

``Payload.size`` makes aiohttp send a real ``Content-Length`` header while the
body is still produced lazily, one chunk at a time.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

from aiohttp.payload import Payload

__all__ = ["StreamingMultipartPayload", "build_multipart", "new_boundary"]

ChunkFactory = Callable[[], AsyncIterator[bytes]]


#: RFC 2046 (and aiohttp) allow at most 70 boundary characters.
MAX_BOUNDARY_LENGTH = 70


def new_boundary() -> str:
    """A random boundary that cannot realistically occur inside a video file.

    53 characters: long enough to be unguessable, short enough to satisfy the
    70 character limit every multipart parser enforces.
    """
    boundary = f"----RPMStreamBoundary{uuid.uuid4().hex}"
    assert len(boundary) <= MAX_BOUNDARY_LENGTH
    return boundary


def _encode_field(name: str, value: str | int | float) -> bytes:
    text = str(value)
    return (
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n{text}\r\n'
    ).encode()


def build_multipart(
    fields: Iterable[tuple[str, str | int | float]],
    *,
    file_field: str,
    file_name: str,
    content_type: str,
    file_size: int,
    boundary: str,
) -> tuple[bytes, bytes, int]:
    """Return ``(head, tail, total_body_size)`` for a streaming multipart body.

    Only the frame is materialised — a few hundred bytes — never the file.
    """
    head = bytearray()
    for name, value in fields:
        head += f"--{boundary}\r\n".encode("ascii")
        head += _encode_field(name, value)

    head += f"--{boundary}\r\n".encode("ascii")
    head += (
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()

    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    total = len(head) + int(file_size) + len(tail)
    return bytes(head), tail, total


class StreamingMultipartPayload(Payload):
    """A multipart body whose file part is an async iterator of chunks."""

    def __init__(
        self,
        head: bytes,
        chunk_factory: ChunkFactory,
        file_size: int,
        tail: bytes,
        *,
        content_type: str,
    ) -> None:
        super().__init__(value=None, content_type=content_type)
        self._head = head
        self._tail = tail
        self._chunk_factory = chunk_factory
        self._file_size = int(file_size)
        self._size = len(head) + self._file_size + len(tail)
        self.bytes_written = 0
        self.chunks_written = 0

    # ------------------------------------------------------------------
    @property
    def file_size(self) -> int:
        return self._file_size

    @property
    def head_size(self) -> int:
        """Bytes of multipart framing written before the first file chunk."""
        return len(self._head)

    async def write(self, writer) -> None:  # noqa: ANN001 - aiohttp writer type
        await writer.write(self._head)
        self.bytes_written += len(self._head)

        async for chunk in self._chunk_factory():
            if not chunk:
                continue
            await writer.write(chunk)
            self.bytes_written += len(chunk)
            self.chunks_written += 1

        await writer.write(self._tail)
        self.bytes_written += len(self._tail)

        if self._file_size and self.bytes_written != self._size:
            raise ValueError(
                f"Multipart body length mismatch: declared {self._size}, sent {self.bytes_written}"
            )

    async def write_with_length(self, writer, content_length) -> None:  # noqa: ANN001
        """aiohttp 3.12+ entry point — the whole body fits the declared length."""
        await self.write(writer)

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        raise TypeError("A streaming multipart payload cannot be decoded to text")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<StreamingMultipartPayload size={self._size} file={self._file_size}>"


async def consume(factory: Callable[[], AsyncIterator[bytes]]) -> int:  # pragma: no cover - helper
    """Read a chunk factory to the end, returning the byte count (tests/tools)."""
    total = 0
    async for chunk in factory():
        total += len(chunk)
    return total


async def collect(factory: Callable[[], AsyncIterator[bytes]]) -> bytes:  # pragma: no cover - helper
    """Concatenate a chunk factory (only ever used by small tests)."""
    out = bytearray()
    async for chunk in factory():
        out += chunk
    return bytes(out)


def as_factory(chunks: Iterable[bytes]) -> ChunkFactory:
    """Wrap an in-memory iterable as a chunk factory (tests / tooling)."""

    async def factory() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    return factory


def awaitable_factory(factory: Callable[[], Awaitable[AsyncIterator[bytes]]]) -> ChunkFactory:
    """Adapt a coroutine returning an async iterator into a chunk factory."""

    async def wrapped() -> AsyncIterator[bytes]:
        async for chunk in await factory():
            yield chunk

    return wrapped
