"""MTProto chunk reading with a fake Pyrogram session."""

import asyncio

import pytest
from pyrogram.errors import FileIdInvalid, FileReferenceExpired as TgFileReferenceExpired, FloodWait
from pyrogram.file_id import FileId, FileType
from pyrogram.raw.functions.upload import GetFile
from pyrogram.raw.types import InputDocumentFileLocation
from pyrogram.raw.types.upload import File as UploadFile

from app.telegram.streamer import (
    FileReferenceExpired,
    MediaSource,
    TelegramChunkStreamer,
    detect_media,
)
from app.utils.errors import MediaError, TelegramTransferError, UploadCancelled


def encoded_file_id() -> str:
    return FileId(
        file_type=FileType.VIDEO,
        dc_id=2,
        media_id=123456789,
        access_hash=987654321,
        file_reference=b"reference",
    ).encode()


class FakeSession:
    """Answers ``upload.getFile`` from an in-memory blob."""

    def __init__(self, data: bytes, *, fail_once: Exception | None = None, max_calls: int = 10_000) -> None:
        self.data = data
        self.calls: list[tuple[int, int]] = []
        self.fail_once = fail_once
        self.max_calls = max_calls
        self.delay = 0.0

    async def invoke(self, request, sleep_threshold: int = 0):  # noqa: ANN001
        assert isinstance(request, GetFile)
        assert isinstance(request.location, InputDocumentFileLocation)
        assert request.location.id == 123456789
        assert request.location.file_reference == b"reference"
        self.calls.append((request.offset, request.limit))

        if len(self.calls) > self.max_calls:  # pragma: no cover - safety net
            raise AssertionError("the streamer looped forever")
        if self.fail_once is not None:
            error, self.fail_once = self.fail_once, None
            raise error
        if self.delay:
            await asyncio.sleep(self.delay)

        return UploadFile(type=None, mtime=0, bytes=self.data[request.offset : request.offset + request.limit])


class FakeClient:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.sessions: list[tuple[int, bool]] = []

    async def get_session(self, dc_id: int, is_media: bool = False, is_cdn: bool = False, temporary: bool = False):
        self.sessions.append((dc_id, is_media))
        return self.session


def make_source(size: int) -> MediaSource:
    return MediaSource(file_id=encoded_file_id(), file_unique_id="unique", file_name="Movie.mp4", size=size)


async def test_streams_every_byte_with_sequential_offsets():
    data = bytes(range(256)) * 12_288  # 3 MiB
    session = FakeSession(data)
    streamer = TelegramChunkStreamer(FakeClient(session), chunk_size=1024 * 1024)

    received = bytearray()
    async for chunk in streamer.stream(make_source(len(data))):
        received += chunk

    assert bytes(received) == data
    assert session.calls == [(0, 1024 * 1024), (1024 * 1024, 1024 * 1024), (2 * 1024 * 1024, 1024 * 1024)]


async def test_chunk_size_is_respected_and_aligned():
    data = b"x" * 300_000
    session = FakeSession(data)
    streamer = TelegramChunkStreamer(FakeClient(session), chunk_size=65_536)
    assert streamer.chunk_size == 65_536

    sizes = [len(chunk) async for chunk in streamer.stream(make_source(len(data)))]
    assert sizes == [65_536, 65_536, 65_536, 65_536, 37_856]  # short read ends the stream


async def test_chunk_size_is_clamped_into_telegrams_window():
    streamer = TelegramChunkStreamer(FakeClient(FakeSession(b"")), chunk_size=999_999_999)
    assert streamer.chunk_size == 1024 * 1024
    odd = TelegramChunkStreamer(FakeClient(FakeSession(b"")), chunk_size=100_001)
    assert odd.chunk_size % 4096 == 0


async def test_on_chunk_callback_reports_progress():
    data = b"y" * 262_144
    session = FakeSession(data)
    streamer = TelegramChunkStreamer(FakeClient(session), chunk_size=65_536)
    seen: list[int] = []

    async for _ in streamer.stream(make_source(len(data)), on_chunk=seen.append):
        pass

    assert seen == [65_536] * 4
    assert sum(seen) == len(data)


async def test_async_on_chunk_callback_is_awaited():
    data = b"z" * 131_072
    session = FakeSession(data)
    streamer = TelegramChunkStreamer(FakeClient(session), chunk_size=65_536)
    seen: list[int] = []

    async def callback(size: int) -> None:
        await asyncio.sleep(0)
        seen.append(size)

    async for _ in streamer.stream(make_source(len(data)), on_chunk=callback):
        pass
    assert seen == [65_536, 65_536]


async def test_cancellation_stops_the_stream_between_chunks():
    data = b"a" * 1_048_576
    session = FakeSession(data)
    streamer = TelegramChunkStreamer(FakeClient(session), chunk_size=262_144)
    cancel = asyncio.Event()
    reads = 0

    with pytest.raises(UploadCancelled):
        async for _ in streamer.stream(make_source(len(data)), cancel=cancel):
            reads += 1
            cancel.set()

    assert reads == 1
    assert len(session.calls) == 1


async def test_file_reference_expiry_is_mapped():
    session = FakeSession(b"abc", fail_once=TgFileReferenceExpired("FILE_REFERENCE_EXPIRED"))
    streamer = TelegramChunkStreamer(FakeClient(session), chunk_size=65_536)
    with pytest.raises(FileReferenceExpired):
        async for _ in streamer.stream(make_source(3)):
            pass


async def test_flood_wait_is_translated():
    session = FakeSession(b"abc", fail_once=FloodWait(3))
    streamer = TelegramChunkStreamer(FakeClient(session), chunk_size=65_536)
    with pytest.raises(TelegramTransferError):
        async for _ in streamer.stream(make_source(3)):
            pass


async def test_invalid_file_id_is_a_media_error():
    session = FakeSession(b"abc", fail_once=FileIdInvalid("FILE_ID_INVALID"))
    streamer = TelegramChunkStreamer(FakeClient(session), chunk_size=65_536)
    with pytest.raises(MediaError):
        async for _ in streamer.stream(make_source(3)):
            pass


async def test_garbage_file_id_is_rejected_before_any_request():
    session = FakeSession(b"")
    streamer = TelegramChunkStreamer(FakeClient(session), chunk_size=65_536)
    broken = MediaSource(file_id="not-a-file-id", file_unique_id="u", file_name="x.mp4", size=10)
    with pytest.raises(MediaError):
        async for _ in streamer.stream(broken):
            pass
    assert session.calls == []


async def test_offset_resumes_midway():
    data = b"0123456789" * 30_000
    session = FakeSession(data)
    streamer = TelegramChunkStreamer(FakeClient(session), chunk_size=65_536)
    received = bytearray()
    async for chunk in streamer.stream(make_source(len(data)), offset=65_536):
        received += chunk
    assert bytes(received) == data[65_536:]
    assert session.calls[0] == (65_536, 65_536)


# ----------------------------------------------------------------------
class _Media:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _Message:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)
        self.media = object()
        self.chat = _Media(id=555)
        self.id = 77


def test_detect_media_accepts_videos():
    message = _Message(video=_Media(file_id="vid", file_unique_id="vu", file_name="A.mp4", file_size=100, mime_type="video/mp4", duration=10, width=1920, height=1080))
    source = detect_media(message)
    assert source is not None
    assert source.kind == "video" and source.size == 100 and source.is_video
    assert source.chat_id == 555 and source.message_id == 77


def test_detect_media_accepts_videos_sent_as_documents():
    message = _Message(document=_Media(file_id="doc", file_unique_id="du", file_name="Show.S01E01.mkv", file_size=999, mime_type="video/x-matroska"))
    source = detect_media(message)
    assert source is not None and source.kind == "video" and source.size == 999


def test_detect_media_accepts_animation():
    message = _Message(animation=_Media(file_id="anim", file_unique_id="au", file_name=None, file_size=50, mime_type="video/mp4"))
    source = detect_media(message)
    assert source is not None and source.kind == "animation"
    assert source.file_name.startswith("animation_")


def test_detect_media_rejects_plain_text():
    message = _Message()
    message.media = None
    assert detect_media(message) is None
    assert detect_media(None) is None


def test_non_video_documents_are_optional():
    document = _Media(file_id="d", file_unique_id="du", file_name="notes.pdf", file_size=10, mime_type="application/pdf")
    assert detect_media(_Message(document=document), allow_any_document=True) is not None
    assert detect_media(_Message(document=document), allow_any_document=False) is None


def test_media_source_describe():
    source = MediaSource(file_id="f", file_unique_id="u", file_name="Movie.mp4", size=2048, mime_type="video/mp4")
    assert "Movie.mp4" in source.describe()
