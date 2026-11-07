import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pagedump.monitor import _AssetCollector, asset_path, categorize, monitor, write_asset


class CategorizeTests(unittest.TestCase):
    def test_extensionless_assets_use_mime(self):
        for mime, category in (("image/avif", "images"), ("font/woff2", "fonts"),
                               ("audio/ogg", "audio"), ("video/mp4", "video"),
                               ("application/pdf", "documents"),
                               ("text/css; charset=utf-8", "css"),
                               ("APPLICATION/JAVASCRIPT", "scripts")):
            with self.subTest(mime=mime):
                self.assertEqual(categorize("fetch", "https://example.com/asset", mime), category)

    def test_mime_overrides_misleading_extension(self):
        self.assertEqual(categorize("image", "https://example.com/file.php", "image/png"), "images")
        self.assertEqual(categorize("fetch", "https://example.com/file.js", "video/mp4"), "video")

    def test_fallbacks_and_encoded_extensions(self):
        for resource, path, category in (("media", "stream", "media"),
                                          ("image", "asset", "images"),
                                          ("fetch", "photo%2Epng?v=1", "images"),
                                          ("fetch", "report.PDF", "documents"),
                                          ("xhr", "events", None)):
            with self.subTest(path=path):
                self.assertEqual(categorize(resource, "https://example.com/" + path), category)


class Request:
    def __init__(self, url, resource_type="image"):
        self.url = url
        self.resource_type = resource_type


class Response:
    def __init__(self, url="https://example.com/image.png", *, status=200,
                 mime="image/png", body=b"image", delay=0, read=None, headers=None):
        self.request = Request(url)
        self.status = status
        self.headers = {"content-type": mime, **(headers or {})}
        self._body = body
        self.delay = delay
        self.read = read
        self.reads = 0

    async def body(self):
        self.reads += 1
        if self.read:
            return await self.read()
        await asyncio.sleep(self.delay)
        return self._body


class Page:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def remove_listener(self, event, handler):
        self.handlers.pop(event, None)


class AssetPathTests(unittest.TestCase):
    def test_names_are_stable_unique_and_safe(self):
        root = Path("output")
        urls = ["https://example.com/a/image.png", "https://example.com/b/image.png",
                "https://example.com/a/image.png?v=2", "https://example.com/%2e%2e%2f%00CON.png",
                "https://example.com/" + "猫" * 300 + ".png", "https://example.com/.."]
        paths = [asset_path(root, "images", url) for url in urls]
        self.assertEqual(len(set(paths)), len(urls))
        self.assertEqual(paths, [asset_path(root, "images", url) for url in urls])
        for path in paths:
            self.assertEqual(path.parent, root / "images")
            self.assertLessEqual(len(path.name.encode()), 255)
            self.assertNotIn("\x00", path.name)
        self.assertEqual(paths[0].suffix, ".png")

    def test_failed_atomic_replace_preserves_original_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "asset.png"
            destination.write_bytes(b"original")
            with patch("pagedump.monitor.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    write_asset(destination, b"replacement")
            self.assertEqual(destination.read_bytes(), b"original")
            self.assertEqual(list(Path(tmp).iterdir()), [destination])


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.collector = _AssetCollector(self.root, {"all"}, 0.2, 3, 0.01)
        self.addAsyncCleanup(self.collector.close)
        self.page = Page()
        await self.collector.setup(self.page)

    def complete(self, response):
        self.collector.on_response(response)
        self.collector.on_request_done(response.request)

    async def test_concurrency_is_bounded_and_duplicates_are_read_once(self):
        active = maximum = 0

        async def read():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.002)
                return b"asset"
            finally:
                active -= 1

        responses = [Response(f"https://example.com/{i}/image.png", read=read) for i in range(40)]
        for response in responses:
            self.complete(response)
            self.complete(response)
        await self.collector.wait_for_idle(self.page)
        self.assertEqual(maximum, 3)
        self.assertEqual(sum(r.reads for r in responses), 40)
        self.assertEqual(len(self.collector.captured["images"]), 40)
        self.assertEqual(len(list((self.root / "images").iterdir())), 40)
        self.assertFalse(self.page.handlers)

    async def test_late_response_is_drained_before_finishing(self):
        first = Response(delay=0.02)
        late = Response("https://example.com/late.png", delay=0.02)
        self.complete(first)

        async def enqueue_late():
            await asyncio.sleep(0.01)
            self.complete(late)

        enqueue = asyncio.create_task(enqueue_late())
        await self.collector.wait_for_idle(self.page)
        await enqueue
        self.assertEqual(set(self.collector.captured["images"]), {first.request.url, late.request.url})

    async def test_unrelated_polling_does_not_delay_idle(self):
        async def poll():
            for _ in range(100):
                request = Request("https://example.com/events", "xhr")
                self.collector.on_request(request)
                self.collector.on_request_done(request)
                await asyncio.sleep(0.005)

        polling = asyncio.create_task(poll())
        try:
            await asyncio.wait_for(self.collector.wait_for_idle(self.page), 0.15)
            self.assertFalse(polling.done())
        finally:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)

    async def test_selected_request_without_headers_keeps_collection_open(self):
        request = Request("https://example.com/slow.png")
        self.collector.on_request(request)
        waiting = asyncio.create_task(self.collector.wait_for_idle(self.page))
        await asyncio.sleep(0.02)
        self.assertFalse(waiting.done())
        self.complete(Response(request.url))
        self.collector.on_request_done(request)
        await asyncio.wait_for(waiting, 0.5)
        self.assertEqual(self.collector.captured["images"], [request.url])

    async def test_status_filter_and_partial_content(self):
        responses = [Response(f"https://example.com/{status}.png", status=status)
                     for status in (204, 205, 302, 304, 404, 500)]
        partial = Response("https://example.com/partial.mp4", status=206, mime="video/mp4",
                           headers={"content-range": "bytes 0-2/6"})
        complete = Response("https://example.com/complete.mp4", status=206, mime="video/mp4",
                            headers={"content-range": "bytes 0-5/6"}, body=b"abcdef")
        with self.assertLogs("pagedump", level="WARNING"):
            for response in [*responses, partial, complete]:
                self.complete(response)
        await self.collector.wait_for_idle(self.page)
        self.assertEqual(sum(r.reads for r in responses), 0)
        self.assertEqual(partial.reads, 0)
        self.assertEqual(self.collector.captured, {"video": [complete.request.url]})

    async def test_media_selection_includes_extensionless_audio_and_video(self):
        self.collector.selected = {"media", "audio", "video"}
        self.complete(Response("https://example.com/song", mime="audio/ogg"))
        self.complete(Response("https://example.com/movie", mime="video/webm"))
        self.complete(Response())
        await self.collector.wait_for_idle(self.page)
        self.assertEqual(set(self.collector.captured), {"audio", "video"})

    async def test_body_timeout_is_reported_without_recording_a_saved_asset(self):
        self.collector.timeout = 0.01
        self.complete(Response(delay=10))
        with self.assertLogs("pagedump", level="WARNING"):
            await asyncio.wait_for(self.collector.wait_for_idle(self.page), 0.5)
        self.assertFalse(self.collector.captured)
        self.assertFalse(list(self.root.iterdir()))

    async def test_shutdown_waits_for_in_progress_disk_write(self):
        started, release = threading.Event(), threading.Event()

        def write(destination, body):
            started.set()
            if not release.wait(2):
                raise RuntimeError("test did not release writer")
            write_asset(destination, body)

        with patch("pagedump.monitor.write_asset", side_effect=write):
            self.complete(Response())
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            closing = asyncio.create_task(self.collector.close())
            try:
                await asyncio.sleep(0.01)
                self.assertFalse(closing.done())
            finally:
                release.set()
                await asyncio.wait_for(closing, 1)
        self.assertEqual(len(self.collector.captured["images"]), 1)
        self.assertTrue(all(worker.done() for worker in self.collector._workers))

    async def test_stalled_responses_do_not_occupy_save_workers(self):
        stalled = [Response(f"https://example.com/stalled{i}.png", delay=10) for i in range(10)]
        for response in stalled:
            self.collector.on_response(response)
        ready = Response("https://example.com/ready.png")
        self.complete(ready)
        await asyncio.wait_for(self.collector._queue.join(), 0.5)
        self.assertEqual(self.collector.captured["images"], [ready.request.url])
        self.assertEqual(sum(response.reads for response in stalled), 0)
        with self.assertLogs("pagedump", level="WARNING"):
            for response in stalled:
                self.collector.on_request_failed(response.request)
        await self.collector.wait_for_idle(self.page)

    async def test_failed_body_read_uses_completed_duplicate_as_fallback(self):
        async def fail():
            await asyncio.sleep(0.01)
            raise RuntimeError("body unavailable")

        failed = Response(read=fail)
        fallback = Response(body=b"fallback")
        with self.assertLogs("pagedump", level="WARNING"):
            self.complete(failed)
            self.complete(fallback)
            await asyncio.wait_for(self.collector.wait_for_idle(self.page), 0.5)
        self.assertEqual(fallback.reads, 1)
        self.assertEqual(self.collector.captured["images"], [fallback.request.url])
        self.assertEqual(next((self.root / "images").iterdir()).read_bytes(), b"fallback")

    async def test_completed_duplicate_can_replace_a_stalled_first_request(self):
        stalled = Response(delay=10)
        ready = Response(body=b"ready")
        self.collector.on_response(stalled)
        self.complete(ready)
        await asyncio.wait_for(self.collector._queue.join(), 0.5)
        self.assertEqual(stalled.reads, 0)
        self.assertEqual(self.collector.captured["images"], [ready.request.url])


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def session(self, fetch):
        class Session:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        Session.fetch = fetch
        return patch("pagedump.monitor.AsyncStealthySession", Session)

    async def test_deadline_keeps_saved_assets_and_cancels_workers(self):
        async def fetch(session, url, **kwargs):
            self.assertEqual(session.kwargs["retries"], 1)
            self.assertFalse(kwargs["network_idle"])
            page = Page()
            await kwargs["page_setup"](page)
            response = Response()
            page.handlers["response"](response)
            page.handlers["requestfinished"](response.request)
            page.handlers["domcontentloaded"]()
            await asyncio.sleep(10)

        with self.session(fetch), self.assertLogs("pagedump", level="WARNING"):
            result = await monitor("https://example.com", headed=False, selected={"images"},
                                   timeout=200, output_dir=self.root)
        self.assertEqual(len(result["images"]), 1)

    async def test_cloudflare_is_enabled_by_default_and_finishes_through_page_action(self):
        async def fetch(session, url, **kwargs):
            self.assertTrue(session.kwargs["solve_cloudflare"])
            page = Page()
            await kwargs["page_setup"](page)
            response = Response()
            page.handlers["response"](response)
            page.handlers["requestfinished"](response.request)
            await kwargs["page_action"](page)

        with self.session(fetch):
            result = await monitor("https://example.com", headed=False, selected={"images"},
                                   timeout=1000, idle_timeout=0, output_dir=self.root)
        self.assertEqual(result, {"images": ["https://example.com/image.png"]})

    async def test_deadline_without_assets_is_an_error(self):
        async def fetch(session, url, **kwargs):
            await asyncio.sleep(10)

        with self.session(fetch), self.assertRaises(TimeoutError):
            await monitor("https://example.com", headed=False, selected={"images"},
                          timeout=10, output_dir=self.root)

    async def test_disk_error_propagates_instead_of_claiming_success(self):
        async def fetch(session, url, **kwargs):
            page = Page()
            await kwargs["page_setup"](page)
            response = Response()
            page.handlers["response"](response)
            page.handlers["requestfinished"](response.request)
            page.handlers["domcontentloaded"]()
            await asyncio.sleep(10)

        with self.session(fetch), patch("pagedump.monitor.write_asset", side_effect=OSError("disk full")):
            with self.assertLogs("pagedump", level="ERROR"), self.assertRaisesRegex(OSError, "disk full"):
                await monitor("https://example.com", headed=False, selected={"images"},
                              timeout=1000, idle_timeout=0, output_dir=self.root, solve_cloudflare=False)
