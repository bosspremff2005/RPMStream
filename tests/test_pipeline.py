"""Full pipeline: real streamer + real RPMShare client + real queue/service.

The only fakes are the two network endpoints (Telegram's MTProto session and the
RPMShare HTTP server), so every line of the streaming path is executed.
"""

import asyncio

import pytest
from pyrogram.raw.functions.upload import GetFile
from pyrogram.raw.types import InputDocumentFileLocation
from pyrogram.raw.types.upload import File as UploadFile

from app.config.settings import Settings
from app.queue.upload_queue import JobState, UploadJob, UploadQueue
from app.rpmshare.client import RPMShareClient
from app.services.upload_service import UploadService
from app.telegram.streamer import MediaSource, TelegramChunkStreamer
from app.ui.animations import Stage


class FakeMediaSession:
    """Serves ``upload.getFile`` from in-memory blobs keyed by document id."""

    def __init__(self, blobs: dict[int, bytes]) -> None:
        self.blobs = blobs
        self.concurrent = 0
        self.peak_concurrent = 0

    async def invoke(self, request, sleep_threshold: int = 0):  # noqa: ANN001
        assert isinstance(request, GetFile)
        assert isinstance(request.location, InputDocumentFileLocation)
        data = self.blobs[request.location.id]
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            await asyncio.sleep(0)  # let the upload side interleave with us
            return UploadFile(type=None, mtime=0, bytes=data[request.offset : request.offset + request.limit])
        finally:
            self.concurrent -= 1


class FakeTelegramClient:
    def __init__(self, session: FakeMediaSession) -> None:
        self.session = session

    async def get_session(self, dc_id: int, is_media: bool = False, is_cdn: bool = False, temporary: bool = False):
        return self.session


def make_source(doc_id: int, name: str, data: bytes) -> MediaSource:
    from pyrogram.file_id import FileId, FileType

    file_id = FileId(
        file_type=FileType.VIDEO, dc_id=2, media_id=doc_id, access_hash=999, file_reference=b"ref"
    ).encode()
    return MediaSource(
        file_id=file_id,
        file_unique_id=f"unique-{doc_id}",
        file_name=name,
        size=len(data),
        mime_type="video/mp4",
    )


@pytest.fixture
def pipeline_settings(env, fake_rpmshare):
    env["RPMSHARE_API_BASE"] = fake_rpmshare.base_url
    env["RPMSHARE_FILE_URL_TEMPLATE"] = f"{fake_rpmshare.base_url}/{{file_code}}"
    env["CHUNK_SIZE"] = "262144"
    env["MAX_CONCURRENT_UPLOADS"] = "1"
    env["MAX_RETRIES"] = "2"
    return Settings.from_env(env=env)


async def test_three_videos_stream_end_to_end(pipeline_settings, fake_rpmshare):
    blobs = {
        1001: bytes(range(256)) * 4_096,  # 1 MiB
        1002: b"\xab" * (700_000),  # not a chunk multiple
        1003: b"\xcd" * 1024,  # smaller than one chunk
    }
    names = {1001: "One.mp4", 1002: "Two.mkv", 1003: "Three.mp4"}

    session = FakeMediaSession(blobs)
    tg = FakeTelegramClient(session)
    streamer = TelegramChunkStreamer(tg, chunk_size=pipeline_settings.chunk_size, max_parallel=2)

    async with RPMShareClient(
        pipeline_settings.rpmshare_api_key, base_url=fake_rpmshare.base_url, timeout=30.0
    ) as rpm:
        service = UploadService(pipeline_settings, rpm, streamer)
        queue = UploadQueue(service.run, max_workers=pipeline_settings.max_concurrent_uploads)
        queue.start()

        jobs = [
            UploadJob(user_id=7, chat_id=7, source=make_source(doc_id, names[doc_id], blobs[doc_id]))
            for doc_id in (1001, 1002, 1003)
        ]
        for job in jobs:
            assert queue.add(job) is not None

        await queue.join()
        await queue.stop()

    for job, doc_id in zip(jobs, (1001, 1002, 1003)):
        assert job.state is JobState.DONE, job.error
        assert job.result is not None
        assert job.progress.stage is Stage.COMPLETE
        assert job.progress.transferred == len(blobs[doc_id])
        assert job.result.file_code == "test123abc"
        assert job.result.watch_url.endswith("test123abc.html")
        assert job.result.hls_url.endswith("master.m3u8")

    # RPMShare received every file byte for byte, in arrival order.
    assert [len(body) for body in fake_rpmshare.uploaded] == [len(blobs[i]) for i in (1001, 1002, 1003)]
    for body, doc_id in zip(fake_rpmshare.uploaded, (1001, 1002, 1003)):
        assert body == blobs[doc_id]
    assert [fields["__filename__"] for fields in fake_rpmshare.upload_fields] == ["One.mp4", "Two.mkv", "Three.mp4"]

    # Every upload declared a real Content-Length, never chunked encoding.
    assert all(te is None for te in fake_rpmshare.transfer_encodings)
    for body, declared in zip(fake_rpmshare.uploaded, fake_rpmshare.upload_content_lengths):
        assert int(declared) - len(body) < 2048


async def test_a_transient_failure_is_retried_and_still_completes(pipeline_settings, fake_rpmshare):
    fake_rpmshare.fail_upload_times = 1
    fake_rpmshare.upload_status = 502

    data = b"z" * 512_000
    blobs = {2001: data}
    streamer = TelegramChunkStreamer(FakeTelegramClient(FakeMediaSession(blobs)), chunk_size=262_144)

    async with RPMShareClient("k", base_url=fake_rpmshare.base_url, timeout=30.0) as rpm:
        service = UploadService(pipeline_settings, rpm, streamer)
        queue = UploadQueue(service.run, max_workers=1)
        queue.start()
        job = UploadJob(user_id=1, chat_id=1, source=make_source(2001, "Retry.mp4", data))
        queue.add(job)
        await queue.join()
        await queue.stop()

    assert job.state is JobState.DONE, job.error
    assert job.attempts == 2
    assert fake_rpmshare.uploaded[-1] == data


async def test_cancelling_mid_transfer_stops_the_stream(pipeline_settings, fake_rpmshare):
    data = b"q" * (8 * 1024 * 1024)
    blobs = {3001: data}
    streamer = TelegramChunkStreamer(FakeTelegramClient(FakeMediaSession(blobs)), chunk_size=65_536)

    async with RPMShareClient("k", base_url=fake_rpmshare.base_url, timeout=30.0) as rpm:
        service = UploadService(pipeline_settings, rpm, streamer)
        queue = UploadQueue(service.run, max_workers=1)
        queue.start()
        job = UploadJob(user_id=1, chat_id=1, source=make_source(3001, "Big.mp4", data))
        queue.add(job)

        # Wait until the transfer is really under way, then cancel it.
        for _ in range(500):
            if job.progress.transferred > 200_000:
                break
            await asyncio.sleep(0.01)
        assert job.progress.transferred > 0, "the upload should have started"

        assert await queue.cancel(job.id) is True
        for _ in range(500):
            if job.state is JobState.CANCELLED:
                break
            await asyncio.sleep(0.01)

        await queue.stop()

    assert job.state is JobState.CANCELLED
    assert job.progress.transferred < len(data), "the transfer must stop before the end"
    assert job.result is None


async def test_concurrent_workers_stay_within_the_limit(pipeline_settings, env, fake_rpmshare):
    env["MAX_CONCURRENT_UPLOADS"] = "2"
    settings = Settings.from_env(env=env)

    blobs = {4000 + i: bytes([i]) * 300_000 for i in range(4)}
    session = FakeMediaSession(blobs)
    streamer = TelegramChunkStreamer(FakeTelegramClient(session), chunk_size=65_536, max_parallel=2)

    async with RPMShareClient("k", base_url=fake_rpmshare.base_url, timeout=30.0) as rpm:
        service = UploadService(settings, rpm, streamer)
        queue = UploadQueue(service.run, max_workers=2)
        queue.start()
        jobs = [UploadJob(user_id=1, chat_id=1, source=make_source(doc_id, f"f{doc_id}.mp4", blobs[doc_id])) for doc_id in blobs]
        for job in jobs:
            queue.add(job)
        await queue.join()
        await queue.stop()

    assert all(job.state is JobState.DONE for job in jobs)
    assert len(fake_rpmshare.uploaded) == 4
    assert session.peak_concurrent <= 2


async def test_size_limit_rejects_before_streaming(pipeline_settings, env):
    env["MAX_FILE_SIZE_MB"] = "1"
    settings = Settings.from_env(env=env)
    streamer = TelegramChunkStreamer(FakeTelegramClient(FakeMediaSession({})), chunk_size=262_144)
    service = UploadService(settings, RPMShareClient("k"), streamer)
    assert service.exceeds_size_limit(make_source(1, "huge.mp4", b"x" * (5 * 1024 * 1024))) is True
    await service._rpm.close()  # noqa: SLF001 - test cleanup
