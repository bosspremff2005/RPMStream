"""RPMShare client against a local server that speaks the documented API."""

import os

import pytest
import pytest_asyncio

from app.rpmshare.client import FileLinks, RPMShareClient, UploadedFile, classify_failure
from app.rpmshare.payload import as_factory
from app.utils.errors import RPMSharePermanentError, RPMShareTransientError


@pytest_asyncio.fixture
async def client(fake_rpmshare):
    rpm = RPMShareClient("test-api-key", base_url=fake_rpmshare.base_url, timeout=15.0)
    try:
        yield rpm
    finally:
        await rpm.close()


def chunks_of(data: bytes, size: int) -> list[bytes]:
    return [data[index : index + size] for index in range(0, len(data), size)]


async def test_account_info_is_parsed(fake_rpmshare, client):
    info = await client.account_info()
    assert info.login == "rpmstream_test"
    assert info.storage_left == 10_737_418_240
    assert info.files_total == 7
    assert info.premium == 1
    assert fake_rpmshare.api_calls == ["account/info"]


async def test_upload_server_is_resolved_and_cached(fake_rpmshare, client):
    url = await client.get_upload_server()
    assert url == f"{fake_rpmshare.base_url}/upload/01"
    calls_after_first = len(fake_rpmshare.api_calls)

    assert await client.get_upload_server() == url
    assert len(fake_rpmshare.api_calls) == calls_after_first, "the upload server must be cached"

    await client.get_upload_server(force=True)
    assert len(fake_rpmshare.api_calls) == calls_after_first + 1


async def test_upload_stream_sends_the_exact_file(fake_rpmshare, client):
    payload_bytes = os.urandom(3 * 1024 * 1024 + 12_345)
    chunk_size = 256 * 1024

    uploaded = await client.upload_stream(
        chunk_factory=as_factory(chunks_of(payload_bytes, chunk_size)),
        file_name="Movie 1080p.mp4",
        file_size=len(payload_bytes),
        content_type="video/mp4",
        title="Movie 1080p",
        tags="promo, test",
        folder_id=25,
        category_id=5,
        public=True,
        adult=False,
    )

    assert isinstance(uploaded, UploadedFile)
    assert uploaded.filecode == "test123abc"
    assert uploaded.status == "OK"
    assert fake_rpmshare.uploaded[0] == payload_bytes, "the server must receive byte identical data"
    assert fake_rpmshare.upload_fields[0]["key"] == "test-api-key"
    assert fake_rpmshare.upload_fields[0]["file_title"] == "Movie 1080p"
    assert fake_rpmshare.upload_fields[0]["tags"] == "promo, test"
    assert fake_rpmshare.upload_fields[0]["fld_id"] == "25"
    assert fake_rpmshare.upload_fields[0]["cat_id"] == "5"
    assert fake_rpmshare.upload_fields[0]["file_public"] == "1"
    assert fake_rpmshare.upload_fields[0]["file_adult"] == "0"
    assert fake_rpmshare.upload_fields[0]["__filename__"] == "Movie 1080p.mp4"


async def test_upload_declares_a_real_content_length(fake_rpmshare, client):
    payload_bytes = os.urandom(1024 * 1024)
    await client.upload_stream(
        chunk_factory=as_factory(chunks_of(payload_bytes, 128 * 1024)),
        file_name="clip.mp4",
        file_size=len(payload_bytes),
    )

    # PHP upload endpoints usually reject chunked bodies; we must send the length.
    assert fake_rpmshare.transfer_encodings[0] is None
    declared = int(fake_rpmshare.upload_content_lengths[0])
    assert declared > len(payload_bytes)
    assert declared - len(payload_bytes) < 2048, "the frame overhead must stay tiny"


async def test_transient_api_errors_are_classified(fake_rpmshare, client):
    fake_rpmshare.fail_api_times = 1
    with pytest.raises(RPMShareTransientError):
        await client.get_upload_server()


async def test_transient_upload_errors_are_classified(fake_rpmshare, client):
    fake_rpmshare.fail_upload_times = 1
    fake_rpmshare.upload_status = 502
    with pytest.raises(RPMShareTransientError):
        await client.upload_stream(
            chunk_factory=as_factory([b"x" * 1024]),
            file_name="clip.mp4",
            file_size=1024,
        )


async def test_resolve_links_only_returns_what_rpmshare_gave(fake_rpmshare, client):
    links = await client.resolve_links("test123abc", watch_url_template="https://rpmshare.com/{file_code}")

    assert links.file_code == "test123abc"
    assert links.watch_url == f"{fake_rpmshare.base_url}/test123abc.html", "RPMShare's own link wins"
    assert links.hls_url.endswith("/master.m3u8")
    assert set(links.qualities) == {"n", "h"}
    assert links.thumbnail.endswith("test123abc.jpg")
    assert links.title == "Test Video"

    labels = [label for label, _ in links.as_buttons()]
    assert "🎬 Watch Video" in labels
    assert "📺 Open Player (HLS)" in labels
    assert "🖼️ Thumbnail" not in labels, "the thumbnail is metadata, not a destination"
    assert links.thumbnail


async def test_resolve_links_survives_a_premium_only_endpoint(fake_rpmshare, client):
    fake_rpmshare.with_direct_link = False
    fake_rpmshare.with_encodings_link = False

    links = await client.resolve_links("test123abc", watch_url_template="https://rpmshare.com/{file_code}")
    assert links.watch_url == "https://rpmshare.com/test123abc"
    assert links.qualities == {}
    assert links.hls_url == ""
    assert links.as_buttons() == [("🎬 Watch Video", "https://rpmshare.com/test123abc")]


async def test_file_info_and_folders(fake_rpmshare, client):
    info = await client.file_info("test123abc")
    assert info["file_code"] == "test123abc"
    assert (await client.encodings("test123abc"))[0]["status"] == "ENCODING"


def test_error_classification():
    assert classify_failure(503, "Server busy").retryable is True
    assert classify_failure(429, "Too many requests").retryable is True
    assert classify_failure(200, "Please try again later").retryable is True
    assert classify_failure(403, "Invalid API key").retryable is False
    assert classify_failure(400, "Storage exceeded").retryable is False
    unknown = classify_failure(None, "weird")
    assert isinstance(unknown, RPMShareTransientError)


async def test_missing_api_key_is_rejected_immediately():
    with pytest.raises(RPMSharePermanentError):
        RPMShareClient("")


async def test_unreachable_host_is_transient():
    rpm = RPMShareClient("key", base_url="http://127.0.0.1:1", timeout=2.0)
    try:
        with pytest.raises(RPMShareTransientError):
            await rpm.account_info()
    finally:
        await rpm.close()


async def test_file_links_button_ordering():
    links = FileLinks(
        file_code="abc",
        watch_url="https://rpmshare.com/abc",
        hls_url="https://rpmshare.com/abc.m3u8",
        qualities={"n": "https://rpmshare.com/abc-n.mp4"},
        thumbnail="https://img/abc.jpg",
    )
    labels = [label for label, _ in links.as_buttons()]
    assert labels == ["🎬 Watch Video", "📺 Open Player (HLS)", "📥 Normal"]
