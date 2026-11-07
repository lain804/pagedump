"""Opt-in integration check: PAGEDUMP_BROWSER_TESTS=1 uv run python -m unittest discover -s tests -v."""

import asyncio
import base64
import os
import tempfile
import unittest
from pathlib import Path

from pagedump.monitor import monitor


@unittest.skipUnless(os.environ.get("PAGEDUMP_BROWSER_TESTS") == "1", "requires installed Chromium")
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_extensionless_assets_with_continuous_background_traffic(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII="
        )
        connections = set()

        async def handle(reader, writer):
            connections.add(writer)
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                path = head.split(b" ", 2)[1]
                if path == b"/":
                    mime = "text/html"
                    body = b"""<!doctype html><link rel="stylesheet" href="/style">
                    <script>
                    fetch('/events');
                    setInterval(() => fetch('/poll'), 50);
                    setTimeout(() => { let i = new Image(); i.src = '/delayed'; }, 100);
                    </script><body>assets</body>"""
                elif path == b"/scripts":
                    mime = "text/html"
                    body = b"<!doctype html><img src='/blocked-image'><script src='/app.js'></script>"
                elif path == b"/app.js":
                    mime, body = "text/javascript", b"window.loaded = true;"
                elif path == b"/mixed":
                    mime = "text/html"
                    body = ("<!doctype html>" + "".join(f"<img src='/stalled{i}'>" for i in range(10))
                            + f"<img src='http://localhost:{port}/delayed'>").encode()
                elif path.startswith(b"/stalled"):
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                                 b"Content-Length: 100000\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    await reader.read()
                    return
                elif path == b"/blocked-image":
                    await reader.read()
                    return
                elif path == b"/style":
                    mime, body = "text/css", b"body { background-image: url('/background'); }"
                elif path in {b"/delayed", b"/background"}:
                    await asyncio.sleep(0.1)
                    mime, body = "image/png", png
                elif path == b"/events":
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
                    await writer.drain()
                    await reader.read()
                    return
                else:
                    mime, body = "application/json", b"{}"
                writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\n"
                              f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
                await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass  # Chromium can open speculative connections and close them unused.
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                connections.discard(writer)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                started = asyncio.get_running_loop().time()
                result = await monitor(f"http://127.0.0.1:{port}/", headed=False, selected={"images"},
                                       timeout=5000, output_dir=Path(tmp), idle_timeout=300,
                                       solve_cloudflare=False)
                elapsed = asyncio.get_running_loop().time() - started
                self.assertEqual({url.rsplit("/", 1)[-1] for url in result["images"]},
                                 {"delayed", "background"})
                self.assertEqual(set(result), {"images"})
                self.assertEqual(len(list((Path(tmp) / "images").iterdir())), 2)
                self.assertLess(elapsed, 4, "background polling should not consume the 5s deadline")
                for asset in (Path(tmp) / "images").iterdir():
                    self.assertEqual(asset.read_bytes(), png)
                started = asyncio.get_running_loop().time()
                scripts = await monitor(f"http://127.0.0.1:{port}/scripts", headed=False,
                                        selected={"scripts"}, timeout=5000, output_dir=Path(tmp),
                                        idle_timeout=300, solve_cloudflare=False)
                self.assertLess(asyncio.get_running_loop().time() - started, 4,
                                "unselected hanging images must not delay selected scripts")
                self.assertEqual(scripts, {"scripts": [f"http://127.0.0.1:{port}/app.js"]})
                with self.assertLogs("pagedump", level="WARNING"):
                    mixed = await monitor(f"http://127.0.0.1:{port}/mixed", headed=False,
                                          selected={"images"}, timeout=1200, output_dir=Path(tmp),
                                          concurrency=3, solve_cloudflare=False)
                self.assertEqual(mixed, {"images": [f"http://localhost:{port}/delayed"]},
                                 "stalled response bodies must not starve a completed asset")
        finally:
            for writer in list(connections):
                writer.close()
