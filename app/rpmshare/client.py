"""RPMShare API client.

Every endpoint used here is taken verbatim from the official RPMShare API
documentation (https://rpmshare.com/apidoc/):

===========================  ====================================================
Endpoint                     Purpose
===========================  ====================================================
``/api/account/info``        login, storage, premium state
``/api/upload/server``       where a multipart upload has to be POSTed
``POST {upload server}``     the real file upload (multipart/form-data)
``/api/upload/url``          remote (URL) upload, returns a task id
``/api/upload/task``         status of a remote upload task
``/api/file/info``           metadata for a file code
``/api/file/direct_link``    downloadable versions + HLS manifest
``/api/file/encodings``      encoding progress and the public page link
``/api/file/list``           paginated file list
``/api/file/delete``         delete a file
``/api/folder/list``         folders (used to pick ``RPMSHARE_FOLDER_ID``)
===========================  ====================================================

Nothing is invented: when RPMShare does not return a URL, no URL is shown.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from collections.abc import AsyncIterator, Callable, Mapping

import aiohttp

from app.rpmshare.payload import StreamingMultipartPayload, build_multipart, new_boundary
from app.utils.errors import RPMSharePermanentError, RPMShareTransientError
from app.utils.logger import get_logger

__all__ = ["RPMShareClient", "UploadedFile", "FileLinks", "AccountInfo"]

log = get_logger("rpmshare.client")

#: API statuses that are worth retrying (server side trouble / throttling).
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})
#: Message fragments that mean "this will never succeed, stop retrying".
PERMANENT_HINTS = (
    "invalid",
    "not found",
    "forbidden",
    "unauthorized",
    "not allowed",
    "no such",
    "exceeded",
    "not enough",
    "too large",
    "blocked",
    "banned",
    "expired",
)
TRANSIENT_HINTS = (
    "try again",
    "temporary",
    "timeout",
    "maintenance",
    "rate limit",
    "too many",
    "overloaded",
    "busy",
)

_UPLOAD_SERVER_TTL = 10 * 60


@dataclass
class UploadedFile:
    """One entry of the ``files`` array returned by the upload endpoint."""

    filecode: str
    filename: str = ""
    status: str = "OK"
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> UploadedFile:
        return cls(
            filecode=str(payload.get("filecode") or payload.get("file_code") or ""),
            filename=str(payload.get("filename") or ""),
            status=str(payload.get("status") or "OK"),
            raw=dict(payload),
        )


@dataclass
class FileLinks:
    """Only URLs that RPMShare actually gave us."""

    file_code: str
    watch_url: str = ""
    hls_url: str = ""
    qualities: dict[str, str] = field(default_factory=dict)
    thumbnail: str = ""
    title: str = ""
    length: str = ""

    def as_buttons(self) -> list[tuple[str, str]]:
        """``(label, url)`` pairs for the completion screen."""
        buttons: list[tuple[str, str]] = []
        if self.watch_url:
            buttons.append(("🎬 Watch Video", self.watch_url))
        if self.hls_url:
            buttons.append(("📺 Open Player (HLS)", self.hls_url))
        for label, url in self.quality_buttons():
            buttons.append((label, url))
        return buttons

    def quality_buttons(self) -> list[tuple[str, str]]:
        names = {"n": "📥 Normal", "h": "📥 HD", "l": "📥 Low", "o": "📥 Original"}
        return [(names.get(name, f"📥 {name.upper()}"), url) for name, url in self.qualities.items() if url]


@dataclass
class AccountInfo:
    login: str = ""
    email: str = ""
    storage_left: int = 0
    storage_used: int = 0
    files_total: int = 0
    premium: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def classify_failure(status: Any, message: str) -> Exception:
    """Split an API error into *retry* vs *give up*."""
    text = (message or "").lower()
    numeric = _to_int(status, 0)

    if numeric in TRANSIENT_STATUSES or any(hint in text for hint in TRANSIENT_HINTS):
        return RPMShareTransientError(f"RPMShare {status}: {message}", status=numeric or None)
    if numeric in {400, 401, 403, 404} or any(hint in text for hint in PERMANENT_HINTS):
        return RPMSharePermanentError(f"RPMShare {status}: {message}", status=numeric or None)
    # Unknown shape → treat as transient but bounded by MAX_RETRIES.
    return RPMShareTransientError(f"RPMShare {status}: {message}", status=numeric or None)


class RPMShareClient:
    """Thin, documented-API-only client around RPMShare."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://rpmshare.com",
        timeout: float = 60.0,
        upload_timeout: float = 0.0,
        session: aiohttp.ClientSession | None = None,
        connector: aiohttp.BaseConnector | None = None,
    ) -> None:
        if not api_key:
            raise RPMSharePermanentError("RPMSHARE_API_KEY is not configured", user_message="The bot is not configured yet.")
        self._api_key = api_key
        self._base_url = (base_url or "https://rpmshare.com").rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout, connect=30, sock_connect=30, sock_read=timeout)
        self._upload_timeout = aiohttp.ClientTimeout(total=upload_timeout or None, connect=30, sock_connect=30, sock_read=300)
        self._own_session = session is None
        self._session = session
        self._connector = connector
        self._upload_server: str = ""
        self._upload_server_at: float = 0.0

    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return self._base_url

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(connector=self._connector, timeout=self._timeout)
            self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._own_session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> RPMShareClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    async def _get(self, endpoint: str, **params: Any) -> Any:
        """GET a documented endpoint and unwrap the ``{msg, status, result}`` envelope."""
        query = {"key": self._api_key}
        for name, value in params.items():
            if value is None or value == "":
                continue
            query[name] = value

        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        session = await self._get_session()

        try:
            async with session.get(url, params=query, timeout=self._timeout) as response:
                raw = await response.text()
                if response.status >= 500:
                    raise RPMShareTransientError(f"HTTP {response.status} from {endpoint}", status=response.status)
                if response.status == 429:
                    raise RPMShareTransientError("RPMShare rate limited the API", status=429)
                if response.status >= 400:
                    raise classify_failure(response.status, raw[:300])
                try:
                    import json

                    data = json.loads(raw)
                except ValueError as exc:
                    raise RPMShareTransientError(f"RPMShare returned a non JSON response from {endpoint}") from exc
        except aiohttp.ClientError as exc:
            raise RPMShareTransientError(f"Network error talking to RPMShare: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise RPMShareTransientError(f"Timeout talking to RPMShare {endpoint}") from exc

        status = data.get("status") if isinstance(data, dict) else None
        message = data.get("msg") if isinstance(data, dict) else None
        if isinstance(data, dict) and status is not None and _to_int(status, 200) != 200:
            raise classify_failure(status, str(message or "unknown error"))
        if isinstance(data, dict) and isinstance(message, str) and message.upper() != "OK" and status is None:
            raise classify_failure(status, message)

        return data.get("result") if isinstance(data, dict) and "result" in data else data

    # ------------------------------------------------------------------
    async def account_info(self) -> AccountInfo:
        """``/api/account/info`` — used by the /status style screens."""
        result = await self._get("api/account/info")
        payload = result if isinstance(result, Mapping) else {}
        return AccountInfo(
            login=str(payload.get("login", "")),
            email=str(payload.get("email", "")),
            storage_left=_to_int(payload.get("storage_left")),
            storage_used=_to_int(payload.get("storage_used")),
            files_total=_to_int(payload.get("files_total")),
            premium=_to_int(payload.get("premium")),
            raw=dict(payload),
        )

    async def get_upload_server(self, *, force: bool = False) -> str:
        """``/api/upload/server`` — cached briefly, it rarely changes."""
        if not force and self._upload_server and (time.monotonic() - self._upload_server_at) < _UPLOAD_SERVER_TTL:
            return self._upload_server
        result = await self._get("api/upload/server")
        url = str(result or "").strip()
        if not url.startswith(("http://", "https://")):
            raise RPMShareTransientError(f"RPMShare returned an unusable upload server: {result!r}")
        self._upload_server = url
        self._upload_server_at = time.monotonic()
        log.info("RPMShare upload server resolved")
        return url

    # ------------------------------------------------------------------
    async def upload_stream(
        self,
        *,
        chunk_factory: Callable[[], AsyncIterator[bytes]],
        file_name: str,
        file_size: int,
        content_type: str = "video/mp4",
        title: str = "",
        description: str = "",
        tags: str = "",
        folder_id: int | None = None,
        category_id: int | None = None,
        public: bool | None = None,
        adult: bool | None = None,
        upload_url: str = "",
    ) -> UploadedFile:
        """Stream a file straight into RPMShare without writing it to disk."""
        target = upload_url or await self.get_upload_server()

        fields: list[tuple[str, str | int | float]] = [("key", self._api_key)]
        if title:
            fields.append(("file_title", title))
        if description:
            fields.append(("file_descr", description))
        if tags:
            fields.append(("tags", tags))
        if folder_id:
            fields.append(("fld_id", int(folder_id)))
        if category_id:
            fields.append(("cat_id", int(category_id)))
        if public is not None:
            fields.append(("file_public", 1 if public else 0))
        if adult is not None:
            fields.append(("file_adult", 1 if adult else 0))

        boundary = new_boundary()
        head, tail, total = build_multipart(
            fields,
            file_field="file",
            file_name=file_name,
            content_type=content_type,
            file_size=file_size,
            boundary=boundary,
        )
        payload = StreamingMultipartPayload(
            head, chunk_factory, file_size, tail, content_type=f"multipart/form-data; boundary={boundary}"
        )

        log.info("Uploading %s (%d bytes) to the RPMShare upload server", file_name, file_size)
        session = await self._get_session()
        try:
            async with session.post(target, data=payload, timeout=self._upload_timeout) as response:
                raw = await response.text()
                if response.status >= 500:
                    raise RPMShareTransientError(f"Upload server HTTP {response.status}", status=response.status)
                if response.status == 429:
                    raise RPMShareTransientError("Upload server rate limited the request", status=429)
                if response.status >= 400:
                    raise classify_failure(response.status, raw[:300])
                import json

                try:
                    data = json.loads(raw)
                except ValueError as exc:
                    raise RPMShareTransientError("Upload server returned a non JSON response") from exc
        except aiohttp.ClientError as exc:
            raise RPMShareTransientError(f"Network error during upload: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise RPMShareTransientError("Upload timed out while streaming to RPMShare") from exc

        if not isinstance(data, dict):
            raise RPMShareTransientError("Unexpected upload response shape")

        envelope_status = _to_int(data.get("status"), 200)
        if envelope_status != 200:
            raise classify_failure(envelope_status, str(data.get("msg") or "upload rejected"))

        files = data.get("files")
        if not files:
            raise RPMShareTransientError(f"Upload finished but RPMShare returned no file: {str(data)[:200]}")

        uploaded = UploadedFile.from_payload(files[0] if isinstance(files, list) else files)
        if not uploaded.filecode:
            raise RPMShareTransientError("RPMShare answered without a file code")
        if uploaded.status and uploaded.status.upper() != "OK":
            raise classify_failure(uploaded.raw.get("status"), uploaded.status)

        log.info("RPMShare accepted the file as %s", uploaded.filecode)
        return uploaded

    # ------------------------------------------------------------------
    async def file_info(self, file_code: str) -> dict[str, Any]:
        """``/api/file/info`` — first entry of the returned list."""
        result = await self._get("api/file/info", file_code=file_code)
        if isinstance(result, list):
            return dict(result[0]) if result else {}
        if isinstance(result, Mapping):
            return dict(result)
        return {}

    async def direct_link(self, file_code: str, *, quality: str | None = None, hls: bool = True) -> dict[str, Any]:
        """``/api/file/direct_link`` — may be unavailable on some accounts."""
        result = await self._get("api/file/direct_link", file_code=file_code, q=quality, hls=1 if hls else 0)
        return dict(result) if isinstance(result, Mapping) else {}

    async def encodings(self, file_code: str = "") -> list[dict[str, Any]]:
        """``/api/file/encodings`` — encoding queue for a file."""
        result = await self._get("api/file/encodings", file_code=file_code)
        if isinstance(result, list):
            return [dict(item) for item in result if isinstance(item, Mapping)]
        return []

    async def file_list(self, *, page: int = 1, per_page: int = 25, folder_id: int | None = None) -> dict[str, Any]:
        """``/api/file/list``."""
        result = await self._get("api/file/list", page=page, per_page=per_page, fld_id=folder_id)
        return dict(result) if isinstance(result, Mapping) else {}

    async def folder_list(self, folder_id: int = 0, *, with_files: bool = False) -> list[dict[str, Any]]:
        """``/api/folder/list`` — handy for choosing ``RPMSHARE_FOLDER_ID``."""
        result = await self._get("api/folder/list", fld_id=folder_id, files=1 if with_files else 0)
        if isinstance(result, Mapping):
            return [dict(item) for item in result.get("folders", []) if isinstance(item, Mapping)]
        return []

    async def delete_file(self, file_code: str) -> None:
        """``/api/file/delete``."""
        await self._get("api/file/delete", file_code=file_code)

    async def edit_file(self, file_code: str, **changes: Any) -> Any:
        """``/api/file/edit``."""
        return await self._get("api/file/edit", file_code=file_code, **changes)

    async def upload_by_url(self, url: str, **options: Any) -> str:
        """``/api/upload/url`` — server side remote upload (no local traffic)."""
        result = await self._get("api/upload/url", url=url, **options)
        if isinstance(result, Mapping):
            return str(result.get("task_id", ""))
        return str(result or "")

    async def upload_task(self, task_id: str) -> dict[str, Any]:
        """``/api/upload/task`` — status of a remote upload."""
        result = await self._get("api/upload/task", task_id=task_id)
        if isinstance(result, Mapping):
            return dict(result)
        if isinstance(result, list) and result:
            return dict(result[0])
        return {}

    # ------------------------------------------------------------------
    async def resolve_links(
        self,
        file_code: str,
        *,
        watch_url_template: str = "https://rpmshare.com/{file_code}",
    ) -> FileLinks:
        """Collect every URL RPMShare reports for a freshly uploaded file.

        ``direct_link`` needs a premium capable account, so its absence is not
        an error — the completion screen simply shows fewer buttons.
        """
        links = FileLinks(file_code=file_code)
        links.watch_url = watch_url_template.format(file_code=file_code)

        try:
            info = await self.file_info(file_code)
        except RPMSharePermanentError:
            info = {}
        except RPMShareTransientError as exc:
            log.debug("file/info unavailable for %s: %s", file_code, exc)
            info = {}

        links.title = str(info.get("file_title", ""))
        links.thumbnail = str(info.get("player_img", ""))
        links.length = str(info.get("file_length", ""))

        try:
            direct = await self.direct_link(file_code)
        except Exception as exc:  # noqa: BLE001 - optional enrichment
            log.debug("direct_link unavailable for %s: %s", file_code, exc)
            direct = {}

        for version in direct.get("versions", []) or []:
            if isinstance(version, Mapping) and version.get("url"):
                links.qualities[str(version.get("name", ""))] = str(version["url"])
        if direct.get("hls_direct"):
            links.hls_url = str(direct["hls_direct"])

        # The public page link RPMShare itself reports wins over the template.
        try:
            for entry in await self.encodings(file_code):
                if entry.get("link"):
                    links.watch_url = str(entry["link"])
                    break
        except Exception as exc:  # noqa: BLE001 - optional enrichment
            log.debug("encodings unavailable for %s: %s", file_code, exc)

        return links
