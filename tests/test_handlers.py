"""Handler layer: media intake, the watcher loop and every callback action."""

import asyncio
import types

import pytest_asyncio
from pyrogram.file_id import FileId, FileType

from app.bot.context import BotContext, set_context
from app.bot.handlers.callbacks import handle_callback
from app.bot.handlers.media import handle_media
from app.bot.keyboards.inline import Callback
from app.queue.upload_queue import JobState, UploadQueue
from app.rpmshare.client import FileLinks
from app.ui.animations import Stage
from app.utils.errors import UploadCancelled


def encoded_file_id(doc_id: int = 4242) -> str:
    return FileId(file_type=FileType.VIDEO, dc_id=2, media_id=doc_id, access_hash=7, file_reference=b"r").encode()


class FakeMessage:
    def __init__(self, chat_id: int = 11, message_id: int = 100, user_id: int = 7) -> None:
        self.id = message_id
        self.chat = types.SimpleNamespace(id=chat_id)
        self.from_user = types.SimpleNamespace(id=user_id, first_name="Otaku", username="otaku")
        self.media = None
        self.video = None
        self.document = None
        self.animation = None
        self.photo = None
        self.command = None
        self.text = ""
        self.markup = None
        self.replies: list[FakeMessage] = []
        self.edits: list[str] = []
        self.deleted = False

    async def reply_text(self, text, reply_markup=None, **kwargs):
        message = FakeMessage(self.chat.id, self.id + len(self.replies) + 1, self.from_user.id)
        message.text = text
        message.markup = reply_markup
        self.replies.append(message)
        return message

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edits.append(text)
        return self

    async def delete(self):
        self.deleted = True

    # helpers -----------------------------------------------------------
    def as_video(self, name: str = "Movie.mp4", size: int = 200_000) -> "FakeMessage":
        self.media = object()
        self.video = types.SimpleNamespace(
            file_id=encoded_file_id(),
            file_unique_id="unique42",
            file_name=name,
            file_size=size,
            mime_type="video/mp4",
            duration=120,
            width=1280,
            height=720,
        )
        return self

    def labels(self) -> list[str]:
        if self.markup is None:
            return []
        return [button.text for row in self.markup.inline_keyboard for button in row]


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.stickers: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text))
        return FakeMessage(chat_id)

    async def send_sticker(self, chat_id, sticker):
        self.stickers.append((chat_id, sticker))


class FakeQuery:
    def __init__(self, message: FakeMessage, data: str, user_id: int = 7) -> None:
        self.data = data
        self.message = message
        self.from_user = types.SimpleNamespace(id=user_id, first_name="Otaku")
        self.answers: list[str | None] = []

    async def answer(self, text=None, cache_time: int = 0):
        self.answers.append(text)


class FakeService:
    """Stands in for UploadService: streams a little and finishes."""

    def __init__(self, *, fail: bool = False, cancel: bool = False, max_size_mb: int = 0) -> None:
        self.fail = fail
        self.cancel = cancel
        self.max_size_mb = max_size_mb
        self.started = asyncio.Event()

    def exceeds_size_limit(self, source) -> bool:
        return bool(self.max_size_mb) and source.size > self.max_size_mb * 1024 * 1024

    async def run(self, job) -> FileLinks:
        self.started.set()
        progress = job.progress
        progress.stage = Stage.TRANSFERRING
        step = max(1, job.size // 4)
        for _ in range(4):
            await asyncio.sleep(0.01)
            if self.cancel:
                progress.stage = Stage.CANCELLED
                raise UploadCancelled("stopped")
            progress.advance(step)
        if self.fail:
            progress.error = "RPMShare is temporarily unavailable."
            progress.stage = Stage.FAILED
            raise RuntimeError("boom")
        links = FileLinks(file_code="code123", watch_url="https://rpmshare.com/code123")
        job.result = links
        progress.file_code = links.file_code
        progress.stage = Stage.COMPLETE
        return links


@pytest_asyncio.fixture
async def ctx(settings, monkeypatch):
    import app.bot.handlers.media as media_module

    monkeypatch.setattr(media_module, "_POLL", 0.02)

    service = FakeService()
    queue = UploadQueue(service.run, max_workers=1)
    queue.start()
    context = BotContext(settings=settings, queue=queue, service=service, rpm=None, streamer=None, client=FakeClient())
    set_context(context)
    yield context
    await queue.stop()


async def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was never met")


# ----------------------------------------------------------------------
async def test_video_is_queued_and_ends_on_the_success_screen(ctx):
    client = FakeClient()
    message = FakeMessage().as_video(size=200_000)

    await handle_media(client, message)

    assert len(message.replies) == 1
    status = message.replies[0]
    assert "Added to Queue" in status.text
    assert "Movie.mp4" in status.text
    assert "📊 View Queue" in status.labels()

    job = ctx.queue.jobs()[0]
    await wait_for(lambda: job.state.is_finished)
    await wait_for(lambda: "UPLOAD COMPLETE" in status.text)

    assert job.state is JobState.DONE
    assert "https://rpmshare.com/code123" not in status.text, "the URL stays behind a button"
    urls = [b.url for row in status.markup.inline_keyboard for b in row if b.url]
    assert urls == ["https://rpmshare.com/code123"]
    assert "🎬 Watch Video" in status.labels()
    assert job.progress.stage is Stage.COMPLETE
    await wait_for(lambda: ctx.watchers == {}), "the watcher task must clean itself up"


async def test_progress_message_is_edited_not_spammed(ctx):
    client = FakeClient()
    message = FakeMessage().as_video(size=400_000)
    await handle_media(client, message)
    status = message.replies[0]
    job = ctx.queue.jobs()[0]

    await wait_for(lambda: job.state.is_finished)
    await wait_for(lambda: "UPLOAD COMPLETE" in status.text)

    assert len(message.replies) == 1, "exactly one message per upload"
    assert len(status.edits) >= 2, "the status message must be edited live"
    assert any("Transferring video" in text for text in status.edits)


async def test_unsupported_media_gets_a_friendly_screen(ctx):
    message = FakeMessage()
    await handle_media(FakeClient(), message)
    assert "Unsupported media" in message.replies[0].text


async def test_private_mode_blocks_unknown_users(ctx, env):
    from app.config.settings import Settings

    ctx.settings = Settings.from_env(env={**env, "ALLOWED_USERS": "999"})
    message = FakeMessage().as_video()
    await handle_media(FakeClient(), message)
    assert "private" in message.replies[0].text
    assert ctx.queue.jobs() == []


async def test_oversized_file_is_rejected(ctx):
    ctx.service.max_size_mb = 1
    message = FakeMessage().as_video(size=5 * 1024 * 1024)
    await handle_media(FakeClient(), message)
    assert "too large" in message.replies[0].text.lower()
    assert ctx.queue.jobs() == []


async def test_failed_upload_offers_a_retry(ctx):
    ctx.service.fail = True
    message = FakeMessage().as_video()
    await handle_media(FakeClient(), message)
    status = message.replies[0]
    job = ctx.queue.jobs()[0]

    await wait_for(lambda: "Upload Failed" in status.text)
    assert job.state is JobState.FAILED
    assert "🔄 Retry" in status.labels()


# ----------------------------------------------------------------------
async def test_home_and_queue_callbacks(ctx):
    message = FakeMessage()
    query = FakeQuery(message, Callback.HOME)
    await handle_callback(FakeClient(), query)
    assert "Welcome" in message.text
    assert "📊 Queue" in message.labels()

    query = FakeQuery(message, f"{Callback.QUEUE}:0")
    await handle_callback(FakeClient(), query)
    assert "Queue" in message.text
    assert query.answers and "job(s)" in query.answers[-1]


async def test_about_and_creator_callbacks_keep_urls_in_buttons(ctx):
    message = FakeMessage()
    await handle_callback(FakeClient(), FakeQuery(message, Callback.ABOUT))
    assert "Salman Biswas" in message.text

    await handle_callback(FakeClient(), FakeQuery(message, Callback.CREATOR))
    urls = [b.url for row in message.markup.inline_keyboard for b in row if b.url]
    assert "https://github.com/salman-dev-app" in urls
    assert "https://profile.vrozek.xyz/" in urls
    assert "https://github.com/salman-dev-app" not in message.text, "raw URLs must not leak into text"


async def test_cancel_flow(ctx):
    ctx.service.cancel = False
    slow = asyncio.Event()

    async def slow_runner(job):
        await slow.wait()

    ctx.queue._runner = slow_runner  # noqa: SLF001 - keep the job running for the test
    message = FakeMessage().as_video()
    status = await message.reply_text("placeholder")
    from app.queue.upload_queue import UploadJob
    from app.telegram.streamer import MediaSource

    job = UploadJob(
        user_id=7,
        chat_id=11,
        source=MediaSource(file_id="x", file_unique_id="u", file_name="Movie.mp4", size=1000),
    )
    ctx.queue.add(job)
    await wait_for(lambda: job.state is JobState.RUNNING)

    # ❌ Cancel -> confirmation screen
    await handle_callback(FakeClient(), FakeQuery(status, f"{Callback.CANCEL}:{job.id}"))
    assert "Cancel Upload?" in status.text
    assert "✅ Yes, Cancel" in status.labels()

    # ↩️ Go back keeps it running
    await handle_callback(FakeClient(), FakeQuery(status, f"{Callback.CANCEL_NO}:{job.id}"))
    assert job.state is JobState.RUNNING

    # ✅ Yes -> cancelled
    await handle_callback(FakeClient(), FakeQuery(status, f"{Callback.CANCEL_YES}:{job.id}"))
    await wait_for(lambda: job.state is JobState.CANCELLED)
    slow.set()

    await handle_callback(FakeClient(), FakeQuery(status, f"{Callback.STATUS}:{job.id}"))
    assert "Upload Cancelled" in status.text


async def test_status_callback_of_an_unknown_job(ctx):
    message = FakeMessage()
    query = FakeQuery(message, f"{Callback.STATUS}:nope1234")
    await handle_callback(FakeClient(), query)
    assert query.answers and "no longer tracked" in query.answers[-1]


async def test_buttons_of_another_user_are_refused(ctx):
    message = FakeMessage().as_video()
    await handle_media(FakeClient(), message)
    job = ctx.queue.jobs()[0]
    await wait_for(lambda: job.state.is_finished)

    status = message.replies[0]
    query = FakeQuery(status, f"{Callback.STATUS}:{job.id}", user_id=999)
    await handle_callback(FakeClient(), query)
    assert query.answers and "not your upload" in query.answers[-1]


async def test_link_and_details_callbacks(ctx):
    message = FakeMessage().as_video()
    await handle_media(FakeClient(), message)
    status = message.replies[0]
    job = ctx.queue.jobs()[0]
    await wait_for(lambda: job.state.is_finished)

    await handle_callback(FakeClient(), FakeQuery(status, f"{Callback.LINK}:{job.id}"))
    assert "https://rpmshare.com/code123" in status.text, "the reveal screen is the only place with a raw URL"

    await handle_callback(FakeClient(), FakeQuery(status, f"{Callback.DETAILS}:{job.id}"))
    assert "Upload Details" in status.text
    assert "Movie.mp4" in status.text


async def test_close_callback_deletes_the_message(ctx):
    message = FakeMessage().as_video()
    await handle_media(FakeClient(), message)
    status = message.replies[0]
    job = ctx.queue.jobs()[0]
    await wait_for(lambda: job.state.is_finished)

    await handle_callback(FakeClient(), FakeQuery(status, f"{Callback.CLOSE}:{job.id}"))
    assert status.deleted is True


async def test_retry_callback_requeues_the_job(ctx):
    message = FakeMessage().as_video()
    await handle_media(FakeClient(), message)
    status = message.replies[0]
    job = ctx.queue.jobs()[0]
    await wait_for(lambda: job.state.is_finished)
    assert job.state is JobState.DONE

    await handle_callback(FakeClient(), FakeQuery(status, f"{Callback.RETRY}:{job.id}"))
    assert "Added to Queue" in status.text
    assert job.state in {JobState.QUEUED, JobState.RUNNING, JobState.DONE}
    await wait_for(lambda: job.state is JobState.DONE)
    assert job.attempts == 0, "a retry starts a clean job"


async def test_watchdog_survives_a_deleted_status_message(ctx):
    """If the user deletes the status message, the result is sent as a new one."""
    client = FakeClient()
    message = FakeMessage().as_video()
    await handle_media(client, message)
    status = message.replies[0]
    job = ctx.queue.jobs()[0]

    # Simulate the message becoming uneditable.
    async def boom(text, reply_markup=None):
        raise RuntimeError("message deleted")

    status.edit_text = boom
    await wait_for(lambda: job.state.is_finished)
    await wait_for(lambda: client.sent != [])
    assert "UPLOAD COMPLETE" in client.sent[0][1]


# ----------------------------------------------------------------------
# /start, /help, /about, /queue, /status, /cancel, /creator
# ----------------------------------------------------------------------
async def test_start_command_shows_the_home_screen(ctx):
    from app.bot.handlers.start import handle_command

    message = FakeMessage()
    message.command = ["start", "RPMStreamBot"]
    await handle_command(FakeClient(), message)

    reply = message.replies[0]
    assert "Welcome, <b>Otaku</b>" in reply.text
    assert "📊 Queue" in reply.labels()
    assert "👨‍💻 Creator" in reply.labels()


async def test_every_command_renders_a_screen(ctx):
    from app.bot.handlers.start import handle_command

    expected = {
        "help": "How it works",
        "about": "Created &amp; Developed by",
        "queue": "Queue",
        "status": "Nothing is uploading",
        "creator": "Official links",
    }
    for command, needle in expected.items():
        message = FakeMessage()
        message.command = [command]
        await handle_command(FakeClient(), message)
        assert message.replies, f"/{command} sent nothing"
        assert needle in message.replies[0].text, f"/{command} rendered the wrong screen"


async def test_status_command_lists_running_uploads(ctx):
    from app.bot.handlers.start import handle_command

    blocker = asyncio.Event()

    async def slow(job):
        await blocker.wait()

    ctx.queue._runner = slow  # noqa: SLF001
    from app.queue.upload_queue import UploadJob
    from app.telegram.streamer import MediaSource

    ctx.queue.add(UploadJob(user_id=7, chat_id=11, source=MediaSource(file_id="x", file_unique_id="u", file_name="Running.mp4", size=1000)))
    await wait_for(lambda: ctx.queue.active_count == 1)

    message = FakeMessage()
    message.command = ["status"]
    await handle_command(FakeClient(), message)
    assert "Running.mp4" in message.replies[0].text
    blocker.set()


async def test_cancel_command_stops_the_users_jobs(ctx):
    from app.bot.handlers.start import handle_command

    blocker = asyncio.Event()

    async def slow(job):
        await blocker.wait()

    ctx.queue._runner = slow  # noqa: SLF001
    from app.queue.upload_queue import UploadJob
    from app.telegram.streamer import MediaSource

    ctx.queue.add(UploadJob(user_id=7, chat_id=11, source=MediaSource(file_id="x", file_unique_id="u", file_name="Mine.mp4", size=10)))
    ctx.queue.add(UploadJob(user_id=8, chat_id=11, source=MediaSource(file_id="y", file_unique_id="v", file_name="Theirs.mp4", size=10)))
    await wait_for(lambda: ctx.queue.active_count == 1)

    message = FakeMessage()
    message.command = ["cancel"]
    await handle_command(FakeClient(), message)

    # Only the caller's uploads are touched — user 8's job keeps running.
    assert "Cancelled 1 upload(s)" in message.replies[0].text
    mine = ctx.queue.get(ctx.queue.jobs(7)[0].id)
    theirs = ctx.queue.get(ctx.queue.jobs(8)[0].id)
    assert mine.cancel_event.is_set()
    assert not theirs.cancel_event.is_set()
    blocker.set()


async def test_commands_respect_private_mode(ctx, env):
    from app.bot.handlers.start import handle_command
    from app.config.settings import Settings

    ctx.settings = Settings.from_env(env={**env, "ALLOWED_USERS": "999"})
    message = FakeMessage()
    message.command = ["start"]
    await handle_command(FakeClient(), message)
    assert "private" in message.replies[0].text
