"""Shared fixtures: settings, a fake RPMShare server, fakes for Telegram."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web

from app.utils.logger import setup_logging

# Keep the test run quiet and out of the repository: no log files, warnings only.
setup_logging("WARNING", log_file=None, to_file=False)

BASE_ENV = {
    "API_ID": "123456",
    "API_HASH": "0123456789abcdef0123456789abcdef",
    "BOT_TOKEN": "123456:TEST-token",
    "RPMSHARE_API_KEY": "test-api-key",
    "MAX_CONCURRENT_UPLOADS": "1",
    "CHUNK_SIZE": "1048576",
    "MAX_RETRIES": "3",
    "PROGRESS_UPDATE_INTERVAL": "1",
    "LOG_LEVEL": "INFO",
    "LOG_TO_FILE": "false",
    "RETRY_BASE_DELAY": "0.01",
    "RETRY_MAX_DELAY": "0.02",
}


@pytest.fixture
def env() -> dict[str, str]:
    return dict(BASE_ENV)


@pytest.fixture
def settings(env):
    from app.config.settings import Settings

    return Settings.from_env(env=env)


class FakeRPMShare:
    """A local server that speaks the documented RPMShare API."""

    def __init__(self) -> None:
        self.app = web.Application(client_max_size=1024**3)
        self.app.router.add_get("/api/account/info", self.account_info)
        self.app.router.add_get("/api/upload/server", self.upload_server)
        self.app.router.add_get("/api/file/info", self.file_info)
        self.app.router.add_get("/api/file/direct_link", self.direct_link)
        self.app.router.add_get("/api/file/encodings", self.encodings)
        self.app.router.add_get("/api/upload/task", self.upload_task)
        self.app.router.add_post("/upload/01", self.upload)

        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.base_url = ""

        # behaviour knobs
        self.fail_upload_times = 0
        self.fail_api_times = 0
        self.upload_status = 500
        self.with_direct_link = True
        self.with_encodings_link = True

        # observations
        self.uploaded: list[bytes] = []
        self.upload_fields: list[dict[str, str]] = []
        self.upload_content_lengths: list[str | None] = []
        self.transfer_encodings: list[str | None] = []
        self.api_calls: list[str] = []

    # ------------------------------------------------------------------
    async def start(self) -> str:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = list(self.site._server.sockets)[0].getsockname()[1]  # noqa: SLF001 - test only
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    # ------------------------------------------------------------------
    @staticmethod
    def _ok(result: Any) -> web.Response:
        return web.json_response({"msg": "OK", "server_time": "2026-01-01 00:00:00", "status": 200, "result": result})

    async def account_info(self, request: web.Request) -> web.Response:
        self.api_calls.append("account/info")
        if self.fail_api_times > 0:
            self.fail_api_times -= 1
            return web.json_response({"msg": "Internal error", "status": 500}, status=500)
        return self._ok(
            {
                "login": "rpmstream_test",
                "email": "test@example.com",
                "storage_left": 10_737_418_240,
                "storage_used": 1_073_741_824,
                "files_total": "7",
                "premium": 1,
            }
        )

    async def upload_server(self, request: web.Request) -> web.Response:
        self.api_calls.append("upload/server")
        if self.fail_api_times > 0:
            self.fail_api_times -= 1
            return web.json_response({"msg": "Try again later", "status": 503}, status=503)
        return self._ok(f"{self.base_url}/upload/01")

    async def file_info(self, request: web.Request) -> web.Response:
        self.api_calls.append("file/info")
        code = request.query.get("file_code", "unknown")
        return self._ok(
            [
                {
                    "file_code": code,
                    "file_title": "Test Video",
                    "player_img": f"{self.base_url}/img/{code}.jpg",
                    "file_length": "600",
                    "canplay": 1,
                    "file_public": "1",
                }
            ]
        )

    async def direct_link(self, request: web.Request) -> web.Response:
        self.api_calls.append("file/direct_link")
        if not self.with_direct_link:
            return web.json_response({"msg": "Not allowed", "status": 403}, status=403)
        code = request.query.get("file_code", "unknown")
        return self._ok(
            {
                "versions": [
                    {"url": f"{self.base_url}/v/{code}/n.mp4", "name": "n", "size": "120755726"},
                    {"url": f"{self.base_url}/v/{code}/h.mp4", "name": "h", "size": "135481436"},
                ],
                "file_length": "600",
                "hls_direct": f"{self.base_url}/hls/{code}/master.m3u8",
            }
        )

    async def encodings(self, request: web.Request) -> web.Response:
        self.api_calls.append("file/encodings")
        if not self.with_encodings_link:
            return self._ok([])
        code = request.query.get("file_code", "unknown")
        return self._ok([{"link": f"{self.base_url}/{code}.html", "progress": 15, "status": "ENCODING", "file_code": code}])

    async def upload_task(self, request: web.Request) -> web.Response:
        self.api_calls.append("upload/task")
        return self._ok({"task_id": request.query.get("task_id", ""), "status": "PENDING", "progress": 0})

    async def upload(self, request: web.Request) -> web.Response:
        """Accept the multipart upload exactly like the documented endpoint."""
        self.upload_content_lengths.append(request.headers.get("Content-Length"))
        self.transfer_encodings.append(request.headers.get("Transfer-Encoding"))

        if self.fail_upload_times > 0:
            self.fail_upload_times -= 1
            return web.json_response({"msg": "Temporary error", "status": self.upload_status}, status=self.upload_status)

        reader = await request.multipart()
        fields: dict[str, str] = {}
        body = bytearray()
        filename = ""

        while True:
            part = await reader.next()
            if part is None:
                break
            disposition = part.headers.get("Content-Disposition", "")
            name = disposition.split('name="', 1)[1].split('"', 1)[0] if 'name="' in disposition else ""
            if 'filename="' in disposition:
                filename = disposition.split('filename="', 1)[1].split('"', 1)[0]
                while True:
                    chunk = await part.read_chunk(256 * 1024)
                    if not chunk:
                        break
                    body += chunk
            else:
                fields[name] = (await part.text())

        self.uploaded.append(bytes(body))
        self.upload_fields.append(fields | {"__filename__": filename})

        return web.json_response(
            {
                "msg": "OK",
                "status": 200,
                "files": [{"filecode": "test123abc", "filename": filename, "status": "OK"}],
            }
        )


@pytest_asyncio.fixture
async def fake_rpmshare():
    server = FakeRPMShare()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
