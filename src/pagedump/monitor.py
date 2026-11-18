from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from copy import copy
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

from patchright.async_api import Page, Request, Response, TimeoutError as BrowserTimeoutError
from scrapling.fetchers import AsyncStealthySession

logger = logging.getLogger("pagedump")

ASSET_TYPES = {"scripts", "images", "css", "fonts", "audio", "video", "documents", "media"}

EXTENSION_TYPES = {
    ".js": "scripts", ".mjs": "scripts", ".cjs": "scripts", ".wasm": "scripts",
    ".css": "css", ".jpg": "images", ".jpeg": "images", ".png": "images",
    ".gif": "images", ".webp": "images", ".svg": "images", ".avif": "images",
    ".ico": "images", ".woff": "fonts", ".woff2": "fonts", ".ttf": "fonts",
    ".otf": "fonts", ".eot": "fonts", ".mp3": "audio", ".wav": "audio",
    ".ogg": "audio", ".m4a": "audio", ".aac": "audio", ".flac": "audio",
    ".mp4": "video", ".webm": "video", ".mov": "video", ".m4v": "video",
    ".avi": "video", ".mkv": "video", ".html": "documents", ".htm": "documents",
    ".xhtml": "documents",
    ".pdf": "documents", ".bmp": "images", ".tif": "images", ".tiff": "images",
    ".opus": "audio", ".weba": "audio", ".m3u8": "media", ".mpd": "media",
}
RESOURCE_TYPES = {
    "script": "scripts", "image": "images", "stylesheet": "css", "font": "fonts",
    "document": "documents",
}


MIME_TYPES = {
    "text/css": "css", "text/javascript": "scripts",
    "application/javascript": "scripts", "application/x-javascript": "scripts",
    "application/ecmascript": "scripts", "text/ecmascript": "scripts",
    "application/wasm": "scripts", "text/html": "documents",
    "application/xhtml+xml": "documents", "application/pdf": "documents",
    "application/font-woff": "fonts", "application/x-font-woff": "fonts",
    "application/vnd.ms-fontobject": "fonts", "application/x-font-ttf": "fonts",
    "application/x-font-opentype": "fonts", "application/ogg": "media",
    "application/vnd.apple.mpegurl": "media", "application/x-mpegurl": "media",
    "application/dash+xml": "media",
}


def categorize(resource_type: str, url: str, content_type: str = "") -> str | None:
    """Prefer response metadata, including for extensionless and XHR assets."""
    mime = content_type.partition(";")[0].strip().lower()
    for prefix, category in (("image/", "images"), ("font/", "fonts"),
                             ("audio/", "audio"), ("video/", "video")):
        if mime.startswith(prefix):
            return category
    if mime in MIME_TYPES:
        return MIME_TYPES[mime]
    suffix = unquote(urlsplit(url).path).rsplit("/", 1)[-1].lower()
    extension_category = EXTENSION_TYPES.get("." + suffix.rsplit(".", 1)[-1]) if "." in suffix else None
    if extension_category:
        return extension_category
    if resource_type == "media":
        return "media"  # Browser does not distinguish audio and video requests.
    return RESOURCE_TYPES.get(resource_type)


def asset_path(output_dir: Path, category: str, url: str) -> Path:
    filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1]).strip()
    filename = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in filename)
    filename = filename.rstrip(". ") or "index"
    path = Path(filename)
    extension = path.suffix if len(path.suffix.encode("utf-8")) <= 20 else ""
    stem = filename[:-len(extension)] if extension else filename
    stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore")
    # Always hash: names stay stable regardless of response order and previous runs.
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return output_dir / category / f"{stem}-{digest}{extension}"


def write_asset(destination: Path, body: bytes) -> None:
    """Commit a complete file without blocking the browser's event loop."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".pagedump-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _AssetCollector:
    def __init__(self, output_dir: Path, selected: set[str], timeout: float,
                 concurrency: int, idle_timeout: float) -> None:
        self.output_dir = output_dir
        self.selected = ASSET_TYPES if "all" in selected else selected.copy()
        if "media" in self.selected:
            self.selected = self.selected | {"audio", "video"}
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.captured: dict[str, list[str]] = defaultdict(list)
        self._queue: asyncio.Queue[tuple[Response, str]] = asyncio.Queue()
        self._seen: set[tuple[str, str]] = set()
        self._saved: set[tuple[str, str]] = set()
        self._responses: dict[Request, tuple[Response, str]] = {}
        self._fallbacks: dict[tuple[str, str], tuple[Response, str]] = {}
        self._active: set[Request] = set()
        self._pending = 0
        self._changed = asyncio.Event()
        self._dom_ready = asyncio.Event()
        self._last_activity = asyncio.get_running_loop().time()
        self._pages: list[Page] = []
        self._closed = False
        self.errors: list[Exception] = []
        # Queue references, rather than spawning an unbounded task per response.
        self._workers = [asyncio.create_task(self._save_worker()) for _ in range(concurrency)]

    def _touch(self) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        self._changed.set()

    def on_request(self, request: Request) -> None:
        category = categorize(request.resource_type, request.url)
        # Scripts and styles can discover selected assets even when not being saved.
        if category in self.selected or request.resource_type in {"script", "stylesheet", "document"}:
            self._active.add(request)
            self._touch()

    def on_request_done(self, request: Request) -> None:
        response = self._responses.pop(request, None)
        if response is not None:
            self._enqueue(*response)
        if request in self._active:
            self._active.remove(request)
            self._touch()

    def on_request_failed(self, request: Request) -> None:
        response = self._responses.pop(request, None)
        if response is not None or categorize(request.resource_type, request.url) in self.selected:
            logger.warning("Request failed for %s", request.url)
        self.on_request_done(request)

    def on_response(self, response: Response) -> None:
        if self._closed or not 200 <= response.status < 300 or response.status in {204, 205}:
            return
        request = response.request
        category = categorize(request.resource_type, request.url, response.headers.get("content-type", ""))
        if category not in self.selected:
            return
        if response.status == 206:
            match = re.fullmatch(r"bytes 0-([0-9]{1,20})/([0-9]{1,20})",
                                 response.headers.get("content-range", ""), re.IGNORECASE)
            if not match or int(match[1]) + 1 != int(match[2]):
                logger.warning("Skipping partial response for %s; not a complete asset", request.url)
                return
        # Headers arrive before the body. Keep incomplete requests out of the
        # worker queue so stalled streams cannot starve completed assets.
        self._responses[request] = (response, category)
        self._active.add(request)
        self._touch()

    def _enqueue(self, response: Response, category: str) -> None:
        key = (category, response.request.url)
        if key in self._seen:
            if key not in self._saved:
                self._fallbacks[key] = (response, category)
            return
        self._seen.add(key)
        self._pending += 1
        self._queue.put_nowait((response, category))
        self._touch()

    async def setup(self, page: Page) -> None:
        self._pages.append(page)
        page.on("request", self.on_request)
        page.on("requestfinished", self.on_request_done)
        page.on("requestfailed", self.on_request_failed)
        page.on("response", self.on_response)
        page.on("domcontentloaded", self._dom_ready.set)

    async def wait_for_document(self) -> None:
        await self._dom_ready.wait()
        await self.wait_for_idle(None)

    async def _save_worker(self) -> None:
        while True:
            response, category = await self._queue.get()
            try:
                try:
                    async with asyncio.timeout(self.timeout):
                        body = await response.body()
                        if response.status == 206:
                            expected = int(response.headers["content-range"].rsplit("/", 1)[1])
                            size = len(body)
                            if response.headers.get("content-encoding", "identity").lower() != "identity":
                                # Chromium exposes decoded bytes; ranges count encoded bytes.
                                size = (await response.request.sizes())["responseBodySize"]
                            if size != expected:
                                raise ValueError(f"range declares {expected} bytes but received {size}")
                except Exception as exc:
                    key = (category, response.request.url)
                    self._seen.discard(key)
                    logger.warning("Could not read %s: %s", response.request.url, exc)
                    fallback = self._fallbacks.pop(key, None)
                    if fallback is not None:
                        self._enqueue(*fallback)
                    continue
                destination = asset_path(self.output_dir, category, response.request.url)
                write = asyncio.create_task(asyncio.to_thread(write_asset, destination, body))
                try:
                    # A cancelled to_thread call otherwise keeps writing after shutdown.
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    try:
                        await write
                    except OSError as exc:
                        self._write_failed(destination, exc)
                    else:
                        self._record_saved(response, category)
                    raise
                except OSError as exc:
                    self._write_failed(destination, exc)
                    continue
                self._record_saved(response, category)
            finally:
                # Idle workers must not retain their last (potentially large) body
                # or the completed to_thread task and its arguments.
                body = write = response = None
                self._pending -= 1
                self._queue.task_done()
                self._touch()

    def _write_failed(self, destination: Path, exc: OSError) -> None:
        if not self.errors:
            # The original traceback and exception chain reference the response body.
            self.errors.append(copy(exc))
        logger.error("Could not write %s: %s", destination, exc)

    def _record_saved(self, response: Response, category: str) -> None:
        key = (category, response.request.url)
        self._saved.add(key)
        self._fallbacks.pop(key, None)
        self.captured[category].append(response.request.url)

    async def wait_for_idle(self, page: Page | None) -> None:
        # Re-evaluate after every event; a single gather() snapshot misses late responses.
        self._touch()
        while True:
            self._changed.clear()
            remaining = self.idle_timeout - (asyncio.get_running_loop().time() - self._last_activity)
            if not self._active and not self._pending and remaining <= 0:
                self._stop_listening()
                return
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=max(remaining, 0.001)
                                       if not self._active and not self._pending else None)
            except TimeoutError:
                pass

    def _stop_listening(self) -> None:
        self._closed = True
        for page in self._pages:
            page.remove_listener("request", self.on_request)
            page.remove_listener("requestfinished", self.on_request_done)
            page.remove_listener("requestfailed", self.on_request_failed)
            page.remove_listener("response", self.on_response)
            page.remove_listener("domcontentloaded", self._dom_ready.set)
        self._pages.clear()

    async def close(self) -> None:
        self._stop_listening()
        for worker in self._workers:
            worker.cancel()
        results = await asyncio.gather(*self._workers, return_exceptions=True)
        self.errors.extend(result for result in results if isinstance(result, Exception))
        # Release queued responses before closing their browser context.
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._pending = 0
        self._responses.clear()
        self._fallbacks.clear()
        self._active.clear()


async def _collect(session: AsyncStealthySession, collector: _AssetCollector,
                   url: str, timeout: int, solve_cloudflare: bool) -> None:
    fetch = asyncio.create_task(session.fetch(
        url, page_setup=collector.setup,
        page_action=collector.wait_for_idle if solve_cloudflare else None,
        network_idle=False, timeout=timeout,
    ))
    idle = None
    try:
        if solve_cloudflare:
            # Let the solver navigate through challenges before deciding the page is ready.
            await fetch
        else:
            # Scrapling waits for `load` even with network_idle=False. Watch selected
            # assets independently so unrelated slow images/frames cannot delay us.
            idle = asyncio.create_task(collector.wait_for_document())
            done, _ = await asyncio.wait({fetch, idle}, return_when=asyncio.FIRST_COMPLETED)
            if fetch in done:
                await fetch  # Propagate navigation errors promptly.
            await idle
    finally:
        tasks = [fetch] + ([idle] if idle is not None else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def monitor(
    url: str, *, headed: bool, selected: set[str], timeout: int,
    output_dir: Path, cookies: list[dict[str, object]] | None = None,
    concurrency: int = 8, idle_timeout: int = 500, solve_cloudflare: bool = True,
) -> dict[str, list[str]]:
    if timeout <= 0 or concurrency <= 0 or idle_timeout < 0:
        raise ValueError("timeout and concurrency must be positive; idle_timeout must be nonnegative")
    if not selected or selected - ASSET_TYPES - {"all"}:
        raise ValueError("select at least one valid asset category")
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    collector = _AssetCollector(output_dir, selected, timeout / 1000, concurrency, idle_timeout / 1000)
    try:
        async with AsyncStealthySession(
            headless=not headed, timeout=timeout, google_search=True,
            solve_cloudflare=solve_cloudflare, cookies=cookies, retries=1,
            additional_args={"ignore_https_errors": False, "chromium_sandbox": True},
        ) as session:
            timed_out = False
            try:
                async with asyncio.timeout(timeout / 1000):
                    await _collect(session, collector, url, timeout, solve_cloudflare)
            except (TimeoutError, BrowserTimeoutError):
                timed_out = True
            finally:
                await collector.close()
            if collector.errors:
                raise collector.errors[0]
            if timed_out:
                if not collector.captured:
                    raise TimeoutError("Collection timed out before any selected assets were saved")
                logger.warning("Collection timed out after %d ms; keeping saved assets", timeout)
    finally:
        await collector.close()
    return dict(collector.captured)
