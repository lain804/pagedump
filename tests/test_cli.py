import contextlib
import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pagedump.cli import load_netscape_cookies, main


class CliTests(unittest.TestCase):
    def test_empty_cookie_expiry_is_a_session_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            row = "example.test\tFALSE\t/\tTRUE\t{}\tprefs\tvalue\n"
            path.write_text(row.format(""), encoding="utf-8")
            [cookie] = load_netscape_cookies(path)
            self.assertEqual(cookie["name"], "prefs")
            self.assertNotIn("expires", cookie)
            path.write_text(row.format("invalid"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expiry must be an integer"):
                load_netscape_cookies(path)

    def test_diagnostic_logs_require_verbose_independently_of_headed(self):
        root = logging.getLogger()
        loggers = [logging.getLogger(name) for name in ("pagedump", "scrapling")]

        async def run(*args, **kwargs):
            for logger in loggers:
                logger.debug("%s debug", logger.name)
                logger.info("%s progress", logger.name)
                logger.warning("%s warning", logger.name)
            return {}

        for headed in ([], ["--headed"]):
            for verbose in ([], ["-v"], ["--verbose"]):
                with self.subTest(headed=headed, verbose=verbose), contextlib.ExitStack() as stack:
                    output = io.StringIO()
                    stack.enter_context(contextlib.redirect_stderr(output))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    stack.enter_context(patch.object(root, "handlers", []))
                    stack.callback(root.setLevel, root.level)
                    for logger in loggers:
                        stack.callback(logger.setLevel, logger.level)
                    stack.enter_context(patch.object(loggers[1], "handlers", [logging.StreamHandler(output)]))
                    monitor = stack.enter_context(patch("pagedump.cli.monitor", AsyncMock(side_effect=run)))
                    main(["https://example.com", *headed, *verbose])
                    self.assertEqual(monitor.await_args.kwargs["headed"], bool(headed))
                    for logger in loggers:
                        self.assertEqual(f"{logger.name} debug" in output.getvalue(), bool(verbose))
                        self.assertEqual(f"{logger.name} progress" in output.getvalue(), bool(verbose))
                        self.assertIn(f"{logger.name} warning", output.getvalue())

    def test_default_timeout_allows_recommended_cloudflare_budget(self):
        run = AsyncMock(return_value={})
        with patch("pagedump.cli.monitor", run), contextlib.redirect_stdout(io.StringIO()):
            main(["https://example.com"])
        self.assertEqual(run.await_args.kwargs["timeout"], 60_000)

    def test_cloudflare_is_enabled_by_default_and_can_be_disabled(self):
        for flags, enabled in (([], True), (["--solve-cloudflare"], True),
                               (["--no-solve-cloudflare"], False)):
            with self.subTest(flags=flags):
                run = AsyncMock(return_value={})
                with patch("pagedump.cli.monitor", run), contextlib.redirect_stdout(io.StringIO()):
                    main(["https://example.com", *flags])
                self.assertEqual(run.await_args.kwargs["solve_cloudflare"], enabled)

    def test_options_reach_monitor_and_summary_counts_saved_urls(self):
        run = AsyncMock(return_value={"images": ["https://example.com/asset"]})
        output = io.StringIO()
        with patch("pagedump.cli.monitor", run), contextlib.redirect_stdout(output):
            main(["https://example.com", "--assets", "images", "-o", "assets",
                  "--timeout", "2000", "--idle-timeout", "100", "--concurrency", "3",
                  "--solve-cloudflare"])
        self.assertEqual(run.await_args.kwargs["timeout"], 2000)
        self.assertEqual(run.await_args.kwargs["idle_timeout"], 100)
        self.assertEqual(run.await_args.kwargs["concurrency"], 3)
        self.assertTrue(run.await_args.kwargs["solve_cloudflare"])
        self.assertEqual(run.await_args.kwargs["output_dir"], Path("assets"))
        self.assertEqual(output.getvalue().strip(), "Saved 1 asset to assets")

    def test_invalid_limits_fail_before_launching_browser(self):
        for option, value in (("--timeout", "0"), ("--timeout", "-1"),
                              ("--idle-timeout", "-1"), ("--concurrency", "0")):
            with self.subTest(option=option, value=value):
                with patch("pagedump.cli.monitor") as run, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exc:
                        main(["https://example.com", option, value])
                self.assertEqual(exc.exception.code, 2)
                run.assert_not_called()

    def test_browser_failure_has_nonzero_status_and_no_success_message(self):
        output = io.StringIO()
        with patch("pagedump.cli.monitor", AsyncMock(side_effect=OSError("disk full"))):
            with self.assertLogs("pagedump", level="ERROR"), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as exc:
                    main(["https://example.com"])
        self.assertEqual(exc.exception.code, 1)
        self.assertFalse(output.getvalue())
