"""End to end pipeline logic: retries, cancellation, reference refresh."""

import asyncio
import time

import pytest

import app.services.upload_service as upload_service_module
from app.queue.upload_queue import UploadJob
from app.rpmshare.client import FileLinks, UploadedFile
from app.services.upload_service import UploadService
from app.telegram.streamer import FileReferenceExpired, MediaSource
from app.ui.animations import Stage
from app.utils.errors import RPMSharePermanentError, RPMShareTransientError, RPMStreamError, UploadCancelled


class FakeRPM:
    def __init__(self, *, fail_times: int = 0, permanent: bool = False) -> None:
        self.fail_times = fail_times
        self.permanent = permanent
        self.uploads = 0
        self.server_calls = 0
        self.received: list[int] = []
        self.names: list[str] = []
        self.titles: list[str] = []
        self.options: list[dict] = []

    async def get_upload_server(self, *, force: bool = False) -> str:
        self.server_calls += 1
        return "http://upload.example/01"

    async def upload_stream(self, *, chunk_factory, file_name, file_size, content_type="video/mp4", title="", **kwargs):
        self.uploads += 1
        self.names.append(file_name)
        self.titles.append(title)
        self.options.append(kwargs)
        if self.permanent:
            raise RPMSharePermanentError("storage exceeded")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RPMShareTransientError("temporary failure")

        total = 0
        async for chunk in chunk_factory():
            total += len(chunk)
        self.received.append(total)
        return UploadedFile(filecode="code123", filename=file_name)

    async def resolve_links(self, file_code: str, *, watch_url_template: str = "") -> FileLinks:
        return FileLinks(file_code=file_code, watch_url=f"https://rpmshare.com/{file_code}")


class FakeStreamer:
    """Mimics :class:`TelegramChunkStreamer` without touching MTProto."""

    def __init__(self, *, chunk_size: int = 65_536, cancel_after: int | None = None, expire_once: bool = False) -> None:
        self.chunk_size = chunk_size
        self.cancel_after = cancel_after
        self.expire_once = expire_once
        self.chunks_read = 0

    def stream(self, source: MediaSource, *, cancel=None, on_chunk=None, offset: int = 0):
        chunk_size = self.chunk_size

        async def generator():
            produced = 0
            size = source.size
            while produced < size:
                if cancel is not None and cancel.is_set():
                    raise UploadCancelled("stopped by the user")
                if self.expire_once and produced == 0:
                    self.expire_once = False
                    raise FileReferenceExpired("FILE_REFERENCE_EXPIRED")
                length = min(chunk_size, size - produced)
                produced += length
                self.chunks_read += 1
                if on_chunk is not None:
                    on_chunk(length)
                yield b"z" * length
                if self.cancel_after is not None and self.chunks_read >= self.cancel_after:
                    if cancel is not None:
                        cancel.set()
                    await asyncio.sleep(0)

        return generator()


def make_source(name: str = "Movie.mp4", size: int = 512 * 1024) -> MediaSource:
    return MediaSource(file_id="id", file_unique_id="u", file_name=name, size=size, mime_type="video/mp4")


def make_job(source: MediaSource | None = None, *, size: int = 512 * 1024) -> UploadJob:
    return UploadJob(user_id=1, chat_id=1, source=source or make_source(size=size))


async def test_happy_path_streams_every_byte(settings):
    rpm, streamer = FakeRPM(), FakeStreamer()
    service = UploadService(settings, rpm, streamer)
    job = make_job()

    links = await service.run(job)

    assert links.file_code == "code123"
    assert links.watch_url == "https://rpmshare.com/code123"
    assert rpm.received == [512 * 1024], "every byte must reach RPMShare"
    assert rpm.server_calls == 1
    assert job.progress.transferred == job.size
    assert job.progress.stage is Stage.COMPLETE
    assert job.progress.file_code == "code123"
    assert job.result is links


async def test_transient_failure_is_retried(settings):
    rpm = FakeRPM(fail_times=1)
    service = UploadService(settings, rpm, FakeStreamer())
    job = make_job()

    links = await service.run(job)

    assert rpm.uploads == 2, "one failure plus one successful retry"
    assert job.attempts == 2
    assert job.progress.attempt == 2
    assert links.file_code == "code123"
    assert rpm.received == [512 * 1024]


async def test_retries_stop_at_max_retries(settings):
    rpm = FakeRPM(fail_times=99)
    service = UploadService(settings, rpm, FakeStreamer())
    job = make_job()

    with pytest.raises(RPMShareTransientError):
        await service.run(job)

    assert rpm.uploads == settings.max_retries + 1
    assert job.progress.stage is Stage.FAILED
    assert job.progress.error


async def test_permanent_failure_is_not_retried(settings):
    rpm = FakeRPM(permanent=True)
    service = UploadService(settings, rpm, FakeStreamer())
    job = make_job()

    with pytest.raises(RPMSharePermanentError):
        await service.run(job)

    assert rpm.uploads == 1
    assert job.progress.stage is Stage.FAILED


async def test_cancellation_stops_the_transfer(settings):
    streamer = FakeStreamer(chunk_size=65_536, cancel_after=2)
    service = UploadService(settings, FakeRPM(), streamer)
    job = make_job()

    with pytest.raises(UploadCancelled):
        await service.run(job)

    assert job.progress.stage is Stage.CANCELLED
    assert job.progress.transferred < job.size, "the stream must stop early"


async def test_expired_file_reference_is_refreshed_and_retried(settings):
    rpm = FakeRPM()
    streamer = FakeStreamer(expire_once=True)
    refreshed = make_source(name="Refreshed.mkv", size=256 * 1024)
    calls: list[str] = []

    async def refresher(source: MediaSource) -> MediaSource:
        calls.append(source.file_name)
        return refreshed

    service = UploadService(settings, rpm, streamer, source_refresher=refresher)
    job = make_job()

    await service.run(job)

    assert calls == ["Movie.mp4"]
    assert rpm.names == ["Movie.mp4", "Refreshed.mkv"]
    assert job.progress.file_name == "Refreshed.mkv"
    assert rpm.received == [256 * 1024]


async def test_unexpected_errors_are_wrapped(settings):
    class Broken(FakeRPM):
        async def upload_stream(self, **kwargs):  # noqa: D102
            raise KeyError("boom")

    service = UploadService(settings, Broken(), FakeStreamer())
    job = make_job()

    with pytest.raises(RPMStreamError) as excinfo:
        await service.run(job)

    # The user sees a friendly line; the KeyError detail stays in the log.
    assert excinfo.value.user_message == "Unexpected error while streaming."
    assert "KeyError" not in excinfo.value.user_message
    assert job.progress.stage is Stage.FAILED
    assert job.progress.error == "Unexpected error while streaming."


async def test_stage_hook_is_called_in_order(settings):
    seen: list[Stage] = []
    service = UploadService(settings, FakeRPM(), FakeStreamer(), on_stage=lambda job, stage: seen.append(stage))
    await service.run(make_job())

    assert seen == [
        Stage.STARTING,
        Stage.PREPARING,
        Stage.CONNECTING,
        Stage.TRANSFERRING,
        Stage.PROCESSING,
        Stage.FINALIZING,
        Stage.COMPLETE,
    ]


async def test_stage_hook_errors_do_not_break_uploads(settings):
    def bad_hook(job, stage):
        raise RuntimeError("UI exploded")

    service = UploadService(settings, FakeRPM(), FakeStreamer(), on_stage=bad_hook)
    links = await service.run(make_job())
    assert links.file_code == "code123"


async def test_upload_options_are_forwarded(settings, env):
    env.update(
        {
            "RPMSHARE_FOLDER_ID": "25",
            "RPMSHARE_CATEGORY_ID": "5",
            "RPMSHARE_TAGS": "promo, hd",
            "RPMSHARE_TITLE_TEMPLATE": "RPMStream | {file_name}",
            "RPMSHARE_FILE_PUBLIC": "false",
        }
    )
    from app.config.settings import Settings

    rpm = FakeRPM()
    service = UploadService(Settings.from_env(env=env), rpm, FakeStreamer())
    await service.run(make_job())

    options = rpm.options[0]
    assert options["folder_id"] == 25
    assert options["category_id"] == 5
    assert options["tags"] == "promo, hd"
    assert options["public"] is False
    assert rpm.titles == ["RPMStream | Movie.mp4"]


async def test_size_limit_helpers(settings, env):
    from app.config.settings import Settings

    limited = Settings.from_env(env={**env, "MAX_FILE_SIZE_MB": "1"})
    service = UploadService(limited, FakeRPM(), FakeStreamer())
    assert service.exceeds_size_limit(make_source(size=2 * 1024 * 1024)) is True
    assert service.exceeds_size_limit(make_source(size=1024)) is False

    unlimited = UploadService(settings, FakeRPM(), FakeStreamer())
    assert unlimited.exceeds_size_limit(make_source(size=99 * 1024 * 1024 * 1024)) is False


async def test_backoff_grows_and_is_bounded(settings):
    service = UploadService(settings, FakeRPM(), FakeStreamer())
    first = service._backoff_delay(1)  # noqa: SLF001 - white box
    fourth = service._backoff_delay(4)  # noqa: SLF001
    assert first >= settings.retry_base_delay
    assert fourth <= settings.retry_max_delay * 1.2
    assert fourth >= first


async def test_ticker_derives_speed_from_progress(settings, monkeypatch):
    monkeypatch.setattr(upload_service_module, "_TICKER_INTERVAL", 0.05)
    service = UploadService(settings, FakeRPM(), FakeStreamer())
    job = make_job(size=10_000_000)
    job.started_at = time.time()

    ticker = asyncio.create_task(service._ticker(job))  # noqa: SLF001
    try:
        for _ in range(6):
            await asyncio.sleep(0.05)
            job.progress.transferred += 500_000
        assert job.progress.speed > 0
        assert job.progress.eta is not None
    finally:
        ticker.cancel()
        await asyncio.sleep(0)


async def test_ticker_survives_a_retry_reset(settings, monkeypatch):
    monkeypatch.setattr(upload_service_module, "_TICKER_INTERVAL", 0.05)
    service = UploadService(settings, FakeRPM(), FakeStreamer())
    job = make_job(size=1_000_000)
    job.started_at = time.time()

    ticker = asyncio.create_task(service._ticker(job))  # noqa: SLF001
    try:
        await asyncio.sleep(0.08)
        job.progress.transferred = 400_000
        await asyncio.sleep(0.08)
        job.progress.transferred = 0  # retry restart
        await asyncio.sleep(0.08)
        job.progress.transferred = 100_000
        await asyncio.sleep(0.08)
        assert job.progress.transferred == 100_000
    finally:
        ticker.cancel()
        await asyncio.sleep(0)
