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

Files are grouped by category. Names retain the URL basename and extension
with a stable URL hash appended, so different directories or query strings
cannot overwrite each other. Only successfully saved URLs count toward the
reported total. Writes use temporary files and atomic replacement; filesystem
errors cause a nonzero exit status.

After page preparation, collection finishes once scripts and styles have finished
and selected assets have been idle for `--idle-timeout` milliseconds (default:
500). With `--no-solve-cloudflare`, collection watches assets after DOM readiness,
so unrelated background polling and unselected slow resources do not hold it
open. Increase this window for pages that discover assets
later. `--concurrency` limits simultaneous body reads and disk writes (default:
8); browser request concurrency is unchanged.

`--timeout` (alias `--load-timeout`) sets a collection deadline in milliseconds
(default: 30000), starting after browser startup. Body reads share this deadline
and also have individual timeouts. Startup, browser cleanup, and finishing an
in-progress disk write can take additional time. A timeout keeps completed files
and emits a warning; timing out before saving any selected assets exits with an
error. Navigations are attempted once rather than silently multiplying the wait
through retries.

Cloudflare solving is enabled by default; it waits for page loading and the
solver before evaluating asset idleness, adding waiting even on ordinary pages.
Use `--no-solve-cloudflare` to disable it, or `--solve-cloudflare` to enable it explicitly.
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
delayed assets, continuous background traffic, and an unrelated hanging image.
