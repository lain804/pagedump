# pagedump

Install globally:

```sh
uv tool install --with-executables-from scrapling git+https://github.com/lain804/pagedump.git
```

or

```sh
pip install git+https://github.com/lain804/pagedump.git
```

Install Chromium once after installing the package:

```sh
scrapling install
```

Requires Scrapling 0.5.1 or newer. The lockfile records the tested version;
refresh it with `uv lock --upgrade-package scrapling` and `uv sync` to update.

Use it with a page URL:

```sh
pagedump https://example.com
pagedump https://example.com --assets images fonts -o ./assets
pagedump https://example.com --assets media --timeout 15000
pagedump https://example.com --cookies cookies.txt
pagedump https://example.com --no-solve-cloudflare
```

Asset categories are `scripts`, `images`, `css`, `fonts`, `audio`, `video`,
`documents`, and `media`, or `all` (the default). `media` includes audio,
video, and media with an unknown subtype. Response content types identify
extensionless assets, including assets requested through fetch/XHR.
HEAD probes are ignored so they cannot replace the body of a later GET.

`X-Content-Type-Options: nosniff` does not exclude an asset. Complete response
bodies exposed by Chromium are collected, including downloaded scripts that
Chromium refuses to execute because of a MIME mismatch. Requests blocked before
a complete response is available cannot be saved; pagedump does not refetch them.

Files are grouped by category. Names retain the URL basename and extension
with a stable URL hash appended, so different directories or query strings
cannot overwrite each other. Only successfully saved URLs count toward the
reported total. Writes use temporary files and atomic replacement; filesystem
errors cause a nonzero exit status.

After page preparation, collection finishes once scripts and styles have finished
and selected assets have been idle for `--idle-timeout` milliseconds (default:
500). Once an asset is saved, duplicate responses in the same category do not
extend the idle window, even if their bodies stall. Scripts, styles, and
documents still count as activity because they can discover further assets. Explicit
event streams and multipart image streams are skipped. When the idle window ends,
pagedump waits for headers from outstanding fetch/XHR requests to identify
selected assets. A request that never sends headers can keep collection open
until `--timeout`. Later unselected polls do not extend that wait.
With `--no-solve-cloudflare`, collection watches assets after DOM readiness.
Increase the idle window for pages that discover assets later.
`--concurrency` limits simultaneous body reads and disk writes (default:
8); browser request concurrency is unchanged.

`--timeout` (alias `--load-timeout`) sets a collection deadline in milliseconds
(default: 60000), starting after browser startup. Body reads share this deadline
and also have individual timeouts. Startup, browser cleanup, and finishing an
in-progress disk write can take additional time. A timeout keeps completed files
and emits a warning; timing out before saving any selected assets exits with an
error. Navigations are attempted once rather than silently multiplying the wait
through retries.

Cloudflare solving is enabled by default; it waits for page loading and the
solver before evaluating asset idleness, adding waiting even on ordinary pages.
The current solver spends about five seconds checking ordinary pages; a shorter
collection timeout can therefore expire even after their assets have loaded.
Use `--no-solve-cloudflare` to disable it, or `--solve-cloudflare` to enable it explicitly.
The 60-second default follows Scrapling's recommended minimum solver budget.
Challenge handling is an attempt, not a verified result: Scrapling can return
with an unresolved challenge, including after an embedded Turnstile click times
out. Saving assets does not prove that a challenge was solved. The browser
regression tests use ordinary local pages and do not validate live CAPTCHAs.
`--headed` only shows the browser. `-v` / `--verbose` enables debug and progress
logs, including saved URLs. Without it, only warnings, errors, and the saved-assets
summary are shown. Cookie files use Netscape format.

This captures assets the browser actually requests during the collection window.
It does not scroll, click, crawl linked files, or assemble streaming media.
Redirects, HTTP errors, and incomplete range responses are not saved as complete
assets. Partial range responses produce a warning.

Run the regression suite with:

```sh
uv run python -m unittest discover -s tests -v
PAGEDUMP_BROWSER_TESTS=1 uv run python -m unittest discover -s tests -v
```

The second command also exercises an installed Chromium against local pages with
delayed assets, continuous background traffic, an unrelated hanging image,
HEAD probes, and `nosniff` responses with both Cloudflare settings.
