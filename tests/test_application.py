"""Application wiring: the objects are built and connected correctly."""

import asyncio

import pytest

from app import __author__, __version__
from app.bot.context import BotContext, get_context, set_context
from app.bot.handlers import handle_callback, handle_command, handle_media, register_handlers
from app.main import Application, build_application
from app.queue.upload_queue import UploadQueue
from app.rpmshare.client import RPMShareClient
from app.services.upload_service import UploadService
from app.telegram.streamer import TelegramChunkStreamer


@pytest.fixture
def app(env, fake_rpmshare, tmp_path):
    from app.config.settings import Settings

    env["RPMSHARE_API_BASE"] = fake_rpmshare.base_url
    env["WORK_DIR"] = str(tmp_path)
    env["MAX_CONCURRENT_UPLOADS"] = "3"
    settings = Settings.from_env(env=env)
    application = Application(settings)
    yield application
    application.queue._workers = []  # noqa: SLF001 - the queue was never started


def test_build_application_wires_every_layer(app):
    assert isinstance(app.rpm, RPMShareClient)
    assert isinstance(app.streamer, TelegramChunkStreamer)
    assert isinstance(app.service, UploadService)
    assert isinstance(app.queue, UploadQueue)
    assert isinstance(app.context, BotContext)

    assert app.queue.max_workers == 3, "workers must follow MAX_CONCURRENT_UPLOADS"
    assert app.streamer.chunk_size == app.settings.chunk_size
    assert app.queue._runner == app.service.run, "the queue must run the upload service"  # noqa: SLF001


async def test_handlers_are_registered_on_the_client(app):
    """Pyrogram registers handlers via a task on the client loop — verify they land."""
    register_handlers(app.client)
    await asyncio.sleep(0)
    groups = app.client.dispatcher.groups
    assert sum(len(handlers) for handlers in groups.values()) == 3

    callbacks = [handler.callback for handlers in groups.values() for handler in handlers]
    assert handle_media in callbacks
    assert handle_command in callbacks
    assert handle_callback in callbacks


def test_startup_log_line_hides_secrets(app):
    text = app.settings.safe_repr()
    assert "test-api-key" not in text
    assert app.settings.bot_token not in text
    assert "***" in text


async def test_rpmshare_account_is_verified_at_boot(app, fake_rpmshare):
    await app._verify_rpmshare()  # noqa: SLF001 - boot step
    assert app.context.account_cache is not None
    assert app.context.account_cache[1].login == "rpmstream_test"
    assert "account/info" in fake_rpmshare.api_calls
    await app.rpm.close()


async def test_rpmshare_verification_failure_does_not_block_boot(app, fake_rpmshare):
    fake_rpmshare.fail_api_times = 1
    await app._verify_rpmshare()  # noqa: SLF001
    assert app.context.account_cache is None
    await app.rpm.close()


def test_context_registry():
    context = BotContext(
        settings=app_settings_stub(),
        queue=None,
        service=None,
        rpm=None,
        streamer=None,
    )
    set_context(context)
    assert get_context() is context


def app_settings_stub():
    from app.config.settings import Settings

    return Settings(
        api_id=1,
        api_hash="h",
        bot_token="t",
        rpmshare_api_key="k",
    )


def test_build_application_from_settings(env):
    from app.config.settings import Settings

    settings = Settings.from_env(env={**env, "WORK_DIR": "/tmp/rpmstream-build-test"})
    application = build_application(settings)
    assert application.settings is settings
    assert __version__
    assert __author__ == "Salman Biswas"
