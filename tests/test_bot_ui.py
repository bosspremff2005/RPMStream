"""Screens, keyboards and the "no raw URLs in text" rule."""

from app.bot.branding import CREATOR, CREATOR_LINKS, PROJECT_COPYRIGHT
from app.bot.keyboards import inline as kb
from app.bot.messages import texts
from app.bot.screens import job_screen, sticker_for_stage
from app.queue.upload_queue import JobState, QueueSnapshot, UploadJob
from app.rpmshare.client import FileLinks
from app.telegram.streamer import MediaSource
from app.ui.animations import Stage


def make_job(state: JobState = JobState.RUNNING, **kwargs) -> UploadJob:
    source = MediaSource(
        file_id="id",
        file_unique_id="u",
        file_name=kwargs.pop("name", "Movie.mp4"),
        size=kwargs.pop("size", 10_737_418_240),
        mime_type="video/mp4",
        duration=3600,
        width=1920,
        height=1080,
    )
    job = UploadJob(user_id=1, chat_id=1, source=source, **kwargs)
    job.state = state
    return job


LINKS = FileLinks(
    file_code="code123",
    watch_url="https://rpmshare.com/code123",
    hls_url="https://rpmshare.com/code123.m3u8",
    qualities={"n": "https://cdn.rpmshare.com/code123-n.mp4"},
    thumbnail="https://img.rpmshare.com/code123.jpg",
    title="Test",
)


# ----------------------------------------------------------------------
def test_running_screen_shows_live_progress(settings):
    job = make_job()
    job.progress.stage = Stage.TRANSFERRING
    job.progress.transferred = 5_368_709_120
    job.progress.speed = 10_485_760

    text, markup = job_screen(job, settings)
    assert "50%" in text
    assert "Transferring video" in text
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "🔄 Refresh Status" in labels
    assert "❌ Cancel Upload" in labels


def test_success_screen_hides_urls_behind_buttons(settings):
    job = make_job(state=JobState.DONE)
    job.result = LINKS
    job.finished_at = job.created_at + 120

    text, markup = job_screen(job, settings)
    assert "UPLOAD COMPLETE" in text
    assert "10.0 GB" in text
    assert "02:00" in text

    # The rule from the brief: destinations live in buttons, never in the text.
    assert "https://rpmshare.com/code123" not in text
    urls = [button.url for row in markup.inline_keyboard for button in row if button.url]
    assert "https://rpmshare.com/code123" in urls
    assert LINKS.hls_url in urls
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "🎬 Watch Video" in labels
    assert "📋 View Link" in labels
    assert "🗑️ Close" in labels


def test_success_screen_without_premium_links_only_shows_what_exists(settings):
    job = make_job(state=JobState.DONE)
    job.result = FileLinks(file_code="abc", watch_url="https://rpmshare.com/abc")

    text, markup = job_screen(job, settings)
    urls = [button.url for row in markup.inline_keyboard for button in row if button.url]
    assert urls == ["https://rpmshare.com/abc"]


def test_failure_screen_offers_a_retry(settings):
    job = make_job(state=JobState.FAILED)
    job.attempts = 3
    job.progress.error = "RPMShare is temporarily unavailable."

    text, markup = job_screen(job, settings)
    assert "Upload Failed" in text
    assert "temporarily unavailable" in text
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "🔄 Retry" in labels


def test_cancelled_screen(settings):
    job = make_job(state=JobState.CANCELLED)
    text, markup = job_screen(job, settings)
    assert "Upload Cancelled" in text
    assert any(button.text == "🏠 Home" for row in markup.inline_keyboard for button in row)


def test_sticker_selection_is_opt_in(settings, env):
    job = make_job(state=JobState.DONE)
    assert sticker_for_stage(job, settings) == ""

    from app.config.settings import Settings

    loud = Settings.from_env(env={**env, "SEND_STAGE_STICKERS": "true", "SUCCESS_STICKER_ID": "CAACAgIA"})
    assert sticker_for_stage(job, loud) == "CAACAgIA"
    failed = make_job(state=JobState.FAILED)
    assert sticker_for_stage(failed, loud) == ""


# ----------------------------------------------------------------------
def test_cancel_confirmation_screen():
    text = texts.cancel_confirm(file_name="Movie.mp4", size=9_019_431_936)
    assert "Cancel Upload?" in text
    assert "8.4 GB" in text
    markup = kb.cancel_confirm_keyboard("abcd1234")
    data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "cyes:abcd1234" in data
    assert "status:abcd1234" in data


def test_queue_screen_paginates(settings):
    jobs = [make_job(name=f"video{i}.mp4") for i in range(8)]
    snapshot = QueueSnapshot(active=jobs[:1], waiting=jobs[1:], processed=3, failed=1, cancelled=2, workers=1)

    text, pages = texts.queue_screen(snapshot, page=0, per_page=6, bot_title=settings.bot_title)
    assert pages == 2
    assert "video0.mp4" in text
    assert "video7.mp4" not in text

    second, _ = texts.queue_screen(snapshot, page=1, per_page=6)
    assert "video7.mp4" in second
    assert "Page 2/2" in second


def test_queue_screen_when_empty(settings):
    text, pages = texts.queue_screen(QueueSnapshot(), page=0)
    assert pages == 1
    assert "queue is empty" in text


def test_details_screen_includes_account_data():
    from app.rpmshare.client import AccountInfo

    job = make_job()
    job.progress.file_code = "code123"
    job.attempts = 2
    account = AccountInfo(login="rpmstream_test", storage_left=10_737_418_240, files_total=7)

    text = texts.details(job, account=account)
    assert "1920×1080" in text
    assert "1:00:00" in text
    assert "code123" in text
    assert "rpmstream_test" in text
    assert "10.0 GB" in text


def test_link_reveal_shows_copyable_urls():
    text = texts.link_reveal(links=LINKS)
    assert "https://rpmshare.com/code123" in text
    assert LINKS.hls_url in text


def test_about_screen_credits_the_creator(settings):
    text = texts.about(bot_title=settings.bot_title, tagline=settings.bot_tagline, creator=settings.creator_name)
    assert "RPMStream" in text
    assert "Created &amp; Developed by" in text
    assert "Salman Biswas" in text
    assert "Fast • Interactive • Lightweight" in text


def test_welcome_and_help_are_button_oriented(settings):
    welcome = texts.welcome(user_name="Otaku", bot_title=settings.bot_title)
    assert "Welcome, <b>Otaku</b>" in welcome
    assert "Choose an option below" in welcome
    assert "as a document" in texts.help_text()


def test_unsupported_and_guard_screens():
    assert "Unsupported media" in texts.unsupported()
    assert "private" in texts.not_allowed()
    assert "Queue Full" in texts.queue_full(limit=250)


# ----------------------------------------------------------------------
def test_creator_links_are_only_ever_buttons():
    markup = kb.creator_links_keyboard()
    urls = [button.url for row in markup.inline_keyboard for button in row if button.url]
    assert set(urls) == {link["url"] for link in CREATOR_LINKS}
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "⬅️ Back" in labels
    # Portfolio gets a full width row, the rest pair up two per row.
    assert len(markup.inline_keyboard[0]) == 1
    assert len(markup.inline_keyboard[1]) == 2


def test_no_raw_creator_url_leaks_into_any_message(settings):
    rendered = [
        texts.welcome(user_name="X", bot_title=settings.bot_title),
        texts.help_text(),
        texts.about(bot_title=settings.bot_title, creator=settings.creator_name),
        texts.creator_screen(creator=settings.creator_name),
        texts.unsupported(),
        texts.not_allowed(),
        texts.queue_full(limit=10),
        texts.cancel_confirm(file_name="a.mp4", size=1),
        texts.cancelled(file_name="a.mp4"),
        texts.success(file_name="a.mp4", size=1, elapsed=1, links=LINKS, job_id="abcd1234"),
        texts.failure(file_name="a.mp4", reason="x", attempts=1),
        texts.details(make_job()),
        texts.queue_screen(QueueSnapshot())[0],
    ]
    blob = "\n".join(rendered)
    for link in CREATOR_LINKS:
        assert link["url"] not in blob, f"{link['url']} must stay behind a button"
    assert CREATOR in blob
    assert PROJECT_COPYRIGHT in blob


def test_status_keyboard_omits_cancel_when_finished():
    labels = [button.text for row in kb.status_keyboard("x", running=False).inline_keyboard for button in row]
    assert "❌ Cancel Upload" not in labels
    assert "🔄 Refresh Status" in labels


def test_queue_navigation_buttons():
    single = kb.queue_navigation(0, 1)
    assert [button.text for row in single.inline_keyboard for button in row] == ["🔄 Refresh", "🏠 Home"]

    multi = kb.queue_navigation(1, 3)
    labels = [button.text for row in multi.inline_keyboard for button in row]
    assert labels == ["◀️ Previous", "🔄 Refresh", "Next ▶️", "🏠 Home"]
    data = [button.callback_data for row in multi.inline_keyboard for button in row]
    assert "queue:0" in data and "queue:2" in data


def test_callback_data_stays_within_telegrams_limit():
    for markup in (
        kb.home_keyboard(queue_count=3),
        kb.status_keyboard("abcd1234"),
        kb.success_keyboard(LINKS, "abcd1234"),
        kb.error_keyboard("abcd1234"),
        kb.about_keyboard(),
        kb.creator_links_keyboard(),
        kb.link_reveal_keyboard("abcd1234"),
        kb.details_keyboard("abcd1234"),
    ):
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data is not None:
                    assert len(button.callback_data.encode()) <= 64


def test_error_keyboard_carries_the_job_id():
    data = [button.callback_data for row in kb.error_keyboard("deadbeef").inline_keyboard for button in row]
    assert "retry:deadbeef" in data
    assert "close:deadbeef" in data
