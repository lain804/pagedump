from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

from scrapling.fetchers import AsyncStealthySession

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
}
RESOURCE_TYPES = {
    "script": "scripts", "image": "images", "stylesheet": "css", "font": "fonts",
    "document": "documents",
}


def categorize(resource_type: str, url: str) -> str | None:
    suffix = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    extension_category = EXTENSION_TYPES.get("." + suffix.rsplit(".", 1)[-1]) if "." in suffix else None
    if extension_category:
        return extension_category
    if resource_type == "media":
        return "media"  # Browser does not distinguish audio and video requests.
    return RESOURCE_TYPES.get(resource_type)


async def monitor(
    url: str, *, headed: bool, selected: set[str], timeout: int,
    output_dir: Path, cookies: list[dict[str, object]] | None = None,
) -> dict[str, list[str]]:
    selected_types = ASSET_TYPES if "all" in selected else selected
    captured: dict[str, list[str]] = defaultdict(list)
    saved_urls: dict[Path, str] = {}
    save_lock = asyncio.Lock()
    save_tasks: set[asyncio.Task[None]] = set()

    async def save_response(response: object) -> None:
        request = response.request
        category = categorize(request.resource_type, request.url)
        if category not in selected_types:
            return

        parsed_url = urlsplit(request.url)
        filename = unquote(parsed_url.path.rsplit("/", 1)[-1]).strip()
        if not filename or filename in {".", ".."}:
            filename = "index"
        # URL paths can contain characters that are invalid in Windows filenames.
        filename = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in filename)
        destination_dir = output_dir / category
        destination = destination_dir / filename

        async with save_lock:
            previous_url = saved_urls.get(destination)
            if previous_url is not None and previous_url != request.url:
                path = Path(filename)
                suffix = hashlib.sha256(request.url.encode("utf-8")).hexdigest()[:10]
                destination = destination_dir / f"{path.stem}-{suffix}{path.suffix}"
            saved_urls[destination] = request.url

        try:
            body = await response.body()
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
        except Exception as exc:
            # Some responses have no readable body (for example, failed or aborted requests).
            logging.getLogger("pagedump").debug("Could not save %s: %s", request.url, exc)

    async def setup(page: object) -> None:
        def on_request(request: object) -> None:
            category = categorize(request.resource_type, request.url)
            if category in selected_types:
                captured[category].append(request.url)

        page.on("request", on_request)

        def on_response(response: object) -> None:
            task = asyncio.create_task(save_response(response))
            save_tasks.add(task)
            task.add_done_callback(save_tasks.discard)

        page.on("response", on_response)

    async def wait_for_page(page: object) -> None:
        if save_tasks:
            await asyncio.gather(*tuple(save_tasks))

    async with AsyncStealthySession(
        headless=not headed,
        timeout=timeout,
        google_search=True,
        google_referer=True,
        solve_cloudflare=True,
        cookies=cookies,
    ) as session:
        await session.fetch(
            url,
            page_setup=setup,
            page_action=wait_for_page,
            network_idle=True,
            timeout=timeout,
        )
    return captured
