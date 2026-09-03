"""The streaming multipart body: exact length, zero look-ahead buffering."""

import os

import pytest

from app.rpmshare.client import RPMShareClient
from app.rpmshare.payload import StreamingMultipartPayload, build_multipart, new_boundary
from app.utils.errors import RPMShareTransientError


def test_boundary_is_random_and_long():
    first, second = new_boundary(), new_boundary()
    assert first != second
    assert len(first) > 40
    assert first.startswith("----RPMStreamBoundary")


def test_frame_sizes_are_exact():
    head, tail, total = build_multipart(
        [("key", "apikey"), ("file_title", "My Movie")],
        file_field="file",
        file_name="movie.mp4",
        content_type="video/mp4",
        file_size=1_000_000,
        boundary="BOUND",
    )
    assert head.startswith(b"--BOUND\r\n")
    assert b'name="key"' in head
    assert b"My Movie" in head
    assert b'name="file"; filename="movie.mp4"' in head
    assert b"Content-Type: video/mp4" in head
    assert tail == b"\r\n--BOUND--\r\n"
    assert total == len(head) + 1_000_000 + len(tail)


def test_payload_declares_the_full_body_size():
    head, tail, total = build_multipart(
        [("key", "k")],
        file_field="file",
        file_name="a.mp4",
        content_type="video/mp4",
        file_size=4096,
        boundary="B",
    )

    async def factory():
        yield b"x" * 4096
        if False:  # pragma: no cover
            yield b""

    payload = StreamingMultipartPayload(head, factory, 4096, tail, content_type="multipart/form-data; boundary=B")
    assert payload.size == total
    assert payload.file_size == 4096


def test_payload_refuses_to_be_decoded():
    payload = StreamingMultipartPayload(b"", lambda: None, 0, b"", content_type="multipart/form-data; boundary=B")
    with pytest.raises(TypeError):
        payload.decode()


async def test_chunks_are_written_strictly_in_order_without_look_ahead(fake_rpmshare):
    """Proves the body is produced lazily: chunk N is only read after N-1 hit the wire."""
    data = os.urandom(2 * 1024 * 1024)
    chunk_size = 128 * 1024
    observations: list[tuple[int, int]] = []
    holder: dict[str, StreamingMultipartPayload] = {}

    async def factory():
        for index, start in enumerate(range(0, len(data), chunk_size)):
            payload = holder["payload"]
            observations.append((index, payload.bytes_written))
            yield data[start : start + chunk_size]

    client = RPMShareClient("test-api-key", base_url=fake_rpmshare.base_url, timeout=30.0)
    try:
        # Build the same payload the client would build, to observe its counter.
        import app.rpmshare.client as rpm_module

        original = rpm_module.StreamingMultipartPayload

        def spy(head, chunk_factory, file_size, tail, *, content_type):
            payload = original(head, chunk_factory, file_size, tail, content_type=content_type)
            holder["payload"] = payload
            return payload

        rpm_module.StreamingMultipartPayload = spy
        try:
            await client.upload_stream(
                chunk_factory=factory,
                file_name="probe.mp4",
                file_size=len(data),
            )
        finally:
            rpm_module.StreamingMultipartPayload = original
    finally:
        await client.close()

    assert fake_rpmshare.uploaded[0] == data
    assert len(observations) == len(data) // chunk_size
    head_len = holder["payload"].head_size
    for index, written in observations:
        expected = head_len + index * chunk_size
        assert written == expected, f"chunk {index} was read before chunk {index - 1} was flushed"


async def test_a_broken_stream_surfaces_as_an_error(fake_rpmshare):
    async def factory():
        yield b"x" * 1024
        raise RuntimeError("Telegram dropped the connection")

    client = RPMShareClient("test-api-key", base_url=fake_rpmshare.base_url, timeout=10.0)
    try:
        with pytest.raises(Exception):  # noqa: B017, PT011 - aiohttp wraps the payload error
            await client.upload_stream(chunk_factory=factory, file_name="broken.mp4", file_size=4096)
    finally:
        await client.close()


async def test_length_mismatch_is_detected(fake_rpmshare):
    """If fewer bytes arrive than declared, the payload refuses to lie about it."""
    head, tail, _ = build_multipart(
        [("key", "k")],
        file_field="file",
        file_name="short.mp4",
        content_type="video/mp4",
        file_size=4096,
        boundary="short-boundary",
    )

    async def factory():
        yield b"x" * 1024  # promised 4096

    payload = StreamingMultipartPayload(
        head, factory, 4096, tail, content_type="multipart/form-data; boundary=short-boundary"
    )

    class Sink:
        async def write(self, data: bytes) -> None:
            return None

    with pytest.raises(ValueError, match="mismatch"):
        await payload.write(Sink())


async def test_retries_are_not_confused_by_transient_errors(fake_rpmshare):
    fake_rpmshare.fail_upload_times = 2
    fake_rpmshare.upload_status = 500

    client = RPMShareClient("test-api-key", base_url=fake_rpmshare.base_url, timeout=10.0)
    data = os.urandom(4096)
    try:
        for _ in range(2):
            with pytest.raises(RPMShareTransientError):
                await client.upload_stream(
                    chunk_factory=lambda: _single(data),
                    file_name="retry.mp4",
                    file_size=len(data),
                )
        result = await client.upload_stream(
            chunk_factory=lambda: _single(data),
            file_name="retry.mp4",
            file_size=len(data),
        )
        assert result.filecode == "test123abc"
        assert fake_rpmshare.uploaded[-1] == data
    finally:
        await client.close()


async def _single(data: bytes):
    yield data
