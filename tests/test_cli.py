import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pagedump.cli import main


class CliTests(unittest.TestCase):
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
