"""RPMStream entry point.

    python -m app.main

Wires configuration → RPMShare client → Telegram streamer → upload queue →
Pyrogram handlers, then runs until the process is stopped.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from pyrogram import Client
from pyrogram.types import BotCommand

from app import __version__
from app.bot.context import BotContext, set_context
from app.bot.handlers import register_handlers
from app.config.settings import ConfigError, Settings
from app.queue.upload_queue import UploadQueue
from app.rpmshare.client import RPMShareClient
from app.services.upload_service import UploadService, refresh_source
from app.telegram.streamer import TelegramChunkStreamer
from app.utils.logger import get_logger, setup_logging

__all__ = ["build_application", "main", "run"]

log = get_logger("main")

_BANNER = r"""
┌──────────────────────────────────────────────────────┐
│  🌊  R P M S T R E A M                               │
│      Telegram → RPMShare streaming uploader          │
│      © Prem — RPMStream                              │
└──────────────────────────────────────────────────────┘
"""


class Application:
    """Owns every long lived object so shutdown is deterministic."""

    def __init__(self, settings: Settings, *, client: Client | None = None) -> None:
        self.settings = settings
        self.rpm = RPMShareClient(
            settings.rpmshare_api_key,
            base_url=settings.rpmshare_api_base,
            timeout=settings.rpmshare_api_timeout,
            upload_timeout=settings.upload_timeout,
        )
        self.client = client or Client(
            name=settings.session_name,
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            bot_token=settings.bot_token,
            workdir=settings.work_dir,
        )
        self.streamer = TelegramChunkStreamer(self.client, chunk_size=settings.chunk_size, max_parallel=max(2, settings.max_concurrent_uploads + 1))
        self.service = UploadService(
            settings,
            self.rpm,
            self.streamer,
            source_refresher=lambda source: refresh_source(self.client, source),
        )
        self.queue = UploadQueue(self.service.run, max_workers=settings.max_concurrent_uploads, max_items=settings.queue_max_items)
        self.context = BotContext(
            settings=settings,
            queue=self.queue,
            service=self.service,
            rpm=self.rpm,
            streamer=self.streamer,
            client=self.client,
        )

    async def start(self) -> None:
        # Handlers must be added while an event loop is running: Pyrogram's
        # dispatcher schedules the registration as a task on ``client.loop``.
        register_handlers(self.client)
        await asyncio.sleep(0)  # let the registration tasks land before updates flow

        await self.client.start()
        me = await self.client.get_me()
        self.queue.start()
        set_context(self.context)
        self.context.started = True
        log.info("Bot @%s is online (v%s)", me.username, __version__)
        log.info("Settings → %s", self.settings.safe_repr())
        await self._publish_commands()
        await self._verify_rpmshare()

    async def stop(self) -> None:
        log.info("Shutting down…")
        await self.queue.stop()
        for watcher in list(self.context.watchers.values()):
            watcher.cancel()
        try:
            await self.client.stop()
        except Exception as exc:  # noqa: BLE001 - shutdown is best effort
            log.debug("Telegram client stop: %s", exc)
        await self.rpm.close()
        log.info("Goodbye 👋")

    async def run(self) -> None:
        await self.start()
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _request_stop() -> None:
            if not stop_event.is_set():
                log.info("Stop signal received")
                stop_event.set()

        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    loop.add_signal_handler(sig, _request_stop)
                except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
                    pass

        try:
            await stop_event.wait()
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    async def _publish_commands(self) -> None:
        try:
            await self.client.set_bot_commands(
                [
                    BotCommand("start", "🏠 Home"),
                    BotCommand("help", "🚀 How it works"),
                    BotCommand("queue", "📊 Queue"),
                    BotCommand("status", "📶 Active uploads"),
                    BotCommand("cancel", "❌ Cancel my uploads"),
                    BotCommand("about", "ℹ️ About & creator"),
                    BotCommand("creator", "👨‍💻 Creator links"),
                ]
            )
        except Exception as exc:  # noqa: BLE001 - cosmetic
            log.debug("Could not publish the bot command menu: %s", exc)

    async def _verify_rpmshare(self) -> None:
        """Sanity check the API key at boot — without blocking startup."""
        try:
            info = await self.rpm.account_info()
            log.info("RPMShare account verified: %s", info.login or "unknown")
            self.context.account_cache = (0.0, info)
        except Exception as exc:  # noqa: BLE001 - the bot still runs, uploads will report it
            log.warning("RPMShare account check failed: %s", exc)


def build_application(settings: Settings | None = None, *, client: Client | None = None) -> Application:
    """Create the application (used by ``main`` and by tests)."""
    settings = settings or Settings.from_env()
    return Application(settings, client=client)


async def run(settings: Settings | None = None) -> None:
    application = build_application(settings)
    await application.run()


def main() -> int:
    print(_BANNER)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"❌ Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.log_file, settings.log_to_file)
    log.info("Starting RPMStream %s", __version__)

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        log.info("Interrupted by the user")
    except Exception as exc:  # noqa: BLE001 - top level crash report
        log.exception("Fatal error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
