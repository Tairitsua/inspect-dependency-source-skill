# Reproducing the dashboard demo

[`dashboard-demo.gif`](../docs/images/dashboard-demo.gif) and [`dashboard-overview.png`](../docs/images/dashboard-overview.png) are captured from the same real catalog, HTTP server, and browser fixture exercised by [`tests/browser_validation.py`](../tests/browser_validation.py). They are not composed mockups.

## Prerequisites

- Python 3.11 or newer.
- Git.
- FFmpeg and FFprobe on `PATH`.
- The pinned Playwright release and its Chromium build.

```bash
python3 -m pip install -r showcase/requirements.txt
python3 -m playwright install chromium
```

## Record and validate

Run from the repository root:

```bash
python3 showcase/record_dashboard.py \
  --output docs/images/dashboard-demo.gif \
  --screenshot-output docs/images/dashboard-overview.png
```

The recorder creates a temporary real Git repository and SQLite catalog, isolates Git from user/system configuration and templates, serves the production dashboard on a random `127.0.0.1` port, and records real Playwright interactions. The public projection fixes volatile timestamps and disk metrics and replaces every local path with a visible withheld marker.

The command fails unless the recording and overview:

- shows package lookup, exact-commit provenance, and fail-closed `integrity_mismatch` evidence;
- makes no external browser request and emits no console, page, or request error;
- contains no common private-path or credential marker in visible text;
- produce a 1120×630 GIF no longer than 30 seconds and no larger than 8 MiB;
- produce a 1440×900 PNG no larger than 3 MiB.

Chromium, platform fonts, and FFmpeg encoders can change bytes between releases, so validation is semantic rather than byte-for-byte. Keep [`showcase/requirements.txt`](requirements.txt) pinned when regenerating release assets.
