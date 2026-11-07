from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from .monitor import ASSET_TYPES, monitor

logger = logging.getLogger("pagedump")


def load_netscape_cookies(path: Path) -> list[dict[str, object]]:
    cookies: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue

        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(f"{path}:{line_number}: expected 7 tab-separated cookie fields")

        domain, include_subdomains, cookie_path, secure, expires, name, value = fields
        is_http_only = domain.startswith("#HttpOnly_")
        if is_http_only:
            domain = domain.removeprefix("#HttpOnly_")

        if include_subdomains not in {"TRUE", "FALSE"}:
            raise ValueError(f"{path}:{line_number}: include-subdomains must be TRUE or FALSE")
        if secure not in {"TRUE", "FALSE"}:
            raise ValueError(f"{path}:{line_number}: secure must be TRUE or FALSE")
        if not domain or not cookie_path.startswith("/") or not name:
            raise ValueError(f"{path}:{line_number}: domain, absolute path, and name are required")
        try:
            expiry = int(expires)
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: expiry must be an integer") from exc

        cookie: dict[str, object] = {
            "name": name,
            "value": value,
            "domain": (
                "." + domain.lstrip(".")
                if include_subdomains == "TRUE"
                else domain.lstrip(".")
            ),
            "path": cookie_path,
            "secure": secure == "TRUE",
            "httpOnly": is_http_only,
        }
        if expiry > 0:
            cookie["expires"] = expiry
        cookies.append(cookie)
    return cookies


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dump page assets using a browser")
    parser.add_argument("url", help="URL to open")
    parser.add_argument(
        "--cookies", type=Path, metavar="FILE",
        help="Load cookies from a Netscape-format cookies file",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path.cwd() / "output", metavar="DIR",
        help="Directory for downloaded assets (default: ./output)",
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser UI")
    parser.add_argument(
        "--solve-cloudflare", action="store_true",
        help="Enable Cloudflare challenge solving (adds waiting even on ordinary pages)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--assets", nargs="+", choices=("all", *sorted(ASSET_TYPES)), default=["all"],
        metavar="TYPE", help="Asset categories to collect (default: all)",
    )
    parser.add_argument(
        "--timeout", "--load-timeout", dest="timeout", type=int, default=30_000, metavar="MS",
        help="Collection deadline after browser startup (default: 30000 ms)",
    )
    parser.add_argument(
        "--idle-timeout", type=int, default=500, metavar="MS",
        help="Finish after selected assets are idle for this long (default: 500 ms)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=8, metavar="N",
        help="Maximum concurrent response body reads and file writes (default: 8)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("timeout must be positive milliseconds")
    if args.idle_timeout < 0:
        parser.error("idle-timeout must be nonnegative milliseconds")
    if args.concurrency <= 0:
        parser.error("concurrency must be positive")

    try:
        cookies = load_netscape_cookies(args.cookies) if args.cookies else None
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
    )
    try:
        captured = asyncio.run(
            monitor(args.url, headed=args.headed, selected=set(args.assets),
                    timeout=args.timeout,
                    idle_timeout=args.idle_timeout,
                    concurrency=args.concurrency,
                    solve_cloudflare=args.solve_cloudflare,
                    cookies=cookies,
                    output_dir=args.output)
        )
    except KeyboardInterrupt:
        logger.info("Interrupted")
        raise SystemExit(130) from None
    except Exception as exc:
        logger.error("Browser session failed: %s", exc)
        raise SystemExit(1) from exc

    if args.verbose:
        for category, urls in sorted(captured.items()):
            logger.debug("%s (%d)", category, len(urls))
            for url in urls:
                logger.debug("  %s", url)
    count = sum(len(urls) for urls in captured.values())
    print(f"Saved {count} {'asset' if count == 1 else 'assets'} to {args.output}")
