#!/usr/bin/env python3
"""Record and validate the public dashboard demo from the real browser fixture."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for module_root in (SCRIPTS, TESTS):
    if str(module_root) not in sys.path:
        sys.path.insert(0, str(module_root))

from _catalog_dashboard import running_server  # noqa: E402
from browser_validation import BrowserDemoProvider  # noqa: E402


PLAYWRIGHT_VERSION = "1.61.0"
FIXED_NOW = "2026-01-15T12:00:00+00:00"
FIXED_GIT_DATE = "2026-01-15T12:00:00Z"
OUTPUT_WIDTH = 1120
OUTPUT_HEIGHT = 630
OVERVIEW_WIDTH = 1440
OVERVIEW_HEIGHT = 900
MAX_DURATION_SECONDS = 30.0
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_OVERVIEW_BYTES = 3 * 1024 * 1024
PATH_FIELDS = {
    "source_path",
    "archive_path",
    "path",
    "canonical_path",
    "preferred_source_path",
    "catalog_root",
    "local_path",
    "manifest_path",
}
FREE_SPACE_FIELDS = {"free_bytes", "disk_free_bytes"}
PRIVATE_MARKERS = (
    "/home/",
    "/Users/",
    "/mnt/",
    "/tmp/",
    "C:\\Users\\",
    "C:/Users/",
    "file:///C:/Users/",
    "Authorization:",
    "token=",
)


class PublicDemoProvider:
    """Sanitize volatile/private fields while retaining real store projections."""

    def __init__(self, delegate: BrowserDemoProvider) -> None:
        self.delegate = delegate
        self.fetch_operation_id = delegate.fetch_operation_id
        self.failed_operation_id = delegate.failed_operation_id

    def summary(self):
        return _public_projection(self.delegate.summary())

    def repositories(self):
        return _public_projection(self.delegate.repositories())

    def repository(self, repository_id):
        return _public_projection(self.delegate.repository(repository_id))

    def tags(self, repository_id):
        return _public_projection(self.delegate.tags(repository_id))

    def operations(self):
        return _public_projection(self.delegate.operations())

    def events(self, after_sequence):
        return _public_projection(self.delegate.events(after_sequence))


def _public_projection(value: Any, *, field: str | None = None) -> Any:
    if field in PATH_FIELDS and value:
        return "[local path withheld for public demo]"
    if field in FREE_SPACE_FIELDS:
        return 128 * 1024 * 1024 * 1024
    if field == "total_bytes":
        return 256 * 1024 * 1024 * 1024
    if field and (field.endswith("_at") or field in {"timestamp", "measured_at"}):
        return FIXED_NOW if value is not None else None
    if isinstance(value, dict):
        return {
            key: _public_projection(item, field=key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_projection(item) for item in value]
    return value


@contextlib.contextmanager
def _deterministic_git_environment(template_directory: Path) -> Iterator[None]:
    template_directory.mkdir(parents=True, exist_ok=True)
    overrides = {
        "GIT_AUTHOR_DATE": FIXED_GIT_DATE,
        "GIT_AUTHOR_EMAIL": "browser-validation@example.invalid",
        "GIT_AUTHOR_NAME": "Browser Validation",
        "GIT_COMMITTER_DATE": FIXED_GIT_DATE,
        "GIT_COMMITTER_EMAIL": "browser-validation@example.invalid",
        "GIT_COMMITTER_NAME": "Browser Validation",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_DEFAULT_HASH": "sha1",
        "GIT_TEMPLATE_DIR": str(template_directory),
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
    }
    isolated_keys = {
        key
        for key in os.environ
        if key.startswith("GIT_CONFIG_")
        or key
        in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        }
    }
    managed_keys = set(overrides) | isolated_keys
    previous = {key: os.environ.get(key) for key in managed_keys}
    for key in managed_keys:
        os.environ.pop(key, None)
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key in managed_keys:
            os.environ.pop(key, None)
        for key, value in previous.items():
            if value is None:
                continue
            else:
                os.environ[key] = value


def _require_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise SystemExit(f"Required showcase tool is unavailable: {name}")
    return executable


def _assert_share_safe(text: str) -> None:
    matches = [marker for marker in PRIVATE_MARKERS if marker.casefold() in text.casefold()]
    if matches:
        raise RuntimeError(f"Public demo contains private/sensitive markers: {matches}")


def _freeze_browser_time(page: Any) -> None:
    page.add_init_script(
        f"""
        (() => {{
          const RealDate = Date;
          const fixed = RealDate.parse({FIXED_NOW!r});
          class FixedDate extends RealDate {{
            constructor(...args) {{ super(...(args.length ? args : [fixed])); }}
            static now() {{ return fixed; }}
          }}
          Object.setPrototypeOf(FixedDate, RealDate);
          window.Date = FixedDate;
        }})();
        """
    )


def _freeze_dashboard_polling(page: Any) -> None:
    """Keep public-capture DOM nodes stable after the dashboard's initial refresh."""
    page.add_init_script(
        """
        (() => {
          const realSetInterval = window.setInterval.bind(window);
          window.setInterval = (callback, delay, ...args) =>
            delay === 2000 ? 0 : realSetInterval(callback, delay, ...args);
        })();
        """
    )


def _capture_overview(
    browser: Any,
    base_url: str,
    destination: Path,
    *,
    console_errors: list[str],
    page_errors: list[str],
    failed_requests: list[str],
    external_requests: list[str],
) -> None:
    context = browser.new_context(
        viewport={"width": OVERVIEW_WIDTH, "height": OVERVIEW_HEIGHT},
        locale="en-US",
        timezone_id="UTC",
        color_scheme="light",
        reduced_motion="reduce",
    )
    page = context.new_page()
    _freeze_browser_time(page)
    _freeze_dashboard_polling(page)
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "requestfailed",
        lambda request: failed_requests.append(f"{request.url}: {request.failure}"),
    )
    page.on(
        "request",
        lambda request: external_requests.append(request.url)
        if not request.url.startswith(base_url)
        else None,
    )
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#sync-state.online").wait_for(timeout=5000)
    page.get_by_role("heading", name="Catalog health").wait_for()
    _assert_share_safe(page.locator("body").inner_text())
    destination.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(destination),
        animations="disabled",
        full_page=False,
    )
    context.close()


def _record_webm(destination: Path, screenshot_destination: Path | None = None) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Install showcase/requirements.txt and its Chromium browser before recording."
        ) from exc

    installed_version = importlib.metadata.version("playwright")
    if installed_version != PLAYWRIGHT_VERSION:
        raise SystemExit(
            f"Playwright {PLAYWRIGHT_VERSION} is required; found {installed_version}."
        )

    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    external_requests: list[str] = []
    observed_scenes: list[str] = []

    with contextlib.ExitStack() as stack:
        workspace = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        with _deterministic_git_environment(workspace / "empty-git-template"):
            fixture = BrowserDemoProvider(
                workspace / "catalog", workspace / "user-sources"
            )
        fixture.now = FIXED_NOW
        provider = PublicDemoProvider(fixture)
        server = stack.enter_context(
            running_server(provider, instance_id="public-dashboard-demo")
        )
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        video_directory = workspace / "video"

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            if screenshot_destination is not None:
                _capture_overview(
                    browser,
                    base_url,
                    screenshot_destination,
                    console_errors=console_errors,
                    page_errors=page_errors,
                    failed_requests=failed_requests,
                    external_requests=external_requests,
                )
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                record_video_dir=str(video_directory),
                record_video_size={"width": 1280, "height": 720},
                locale="en-US",
                timezone_id="UTC",
                color_scheme="light",
                reduced_motion="reduce",
            )
            page = context.new_page()
            _freeze_browser_time(page)
            _freeze_dashboard_polling(page)
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "requestfailed",
                lambda request: failed_requests.append(
                    f"{request.url}: {request.failure}"
                ),
            )
            page.on(
                "request",
                lambda request: external_requests.append(request.url)
                if not request.url.startswith(base_url)
                else None,
            )

            page.goto(base_url, wait_until="domcontentloaded")
            page.locator("#sync-state.online").wait_for(timeout=5000)
            page.get_by_role("heading", name="Catalog health").wait_for()
            observed_scenes.append(page.locator("body").inner_text())
            page.wait_for_timeout(2200)

            search = page.locator("#repository-search")
            search.click()
            search.press_sequentially("Alpha.Core", delay=110)
            page.get_by_role("heading", name="Acme / Alpha").wait_for()
            observed_scenes.append(page.locator("body").inner_text())
            page.wait_for_timeout(1800)

            package_card = page.locator(".detail-card").filter(
                has_text="Package provenance"
            )
            package_card.scroll_into_view_if_needed()
            if "Exact commit" not in package_card.inner_text():
                raise RuntimeError("Exact package provenance was not visible in the demo.")
            if "[local path withheld for public demo]" not in page.locator("body").inner_text():
                raise RuntimeError("The public path-withholding marker was not visible.")
            observed_scenes.append(page.locator("body").inner_text())
            page.wait_for_timeout(2600)

            search.fill("")
            page.locator("#repository-filter").select_option("warning")
            page.locator("button[data-repository-id='beta']").click()
            page.get_by_role("heading", name="Beta SDK").wait_for()
            observed_scenes.append(page.locator("body").inner_text())
            page.wait_for_timeout(1800)

            failed_operation = page.locator(
                f"details[data-operation-id='{provider.failed_operation_id}']"
            )
            failed_operation.scroll_into_view_if_needed()
            failed_operation.locator("summary").click()
            if "integrity_mismatch" not in failed_operation.inner_text():
                raise RuntimeError("Fail-closed integrity evidence was not visible.")
            observed_scenes.append(page.locator("body").inner_text())
            page.wait_for_timeout(3200)

            for scene in observed_scenes:
                _assert_share_safe(scene)
            combined = "\n".join(observed_scenes)
            for required in ("Alpha.Core", "Exact commit", "integrity_mismatch"):
                if required not in combined:
                    raise RuntimeError(f"Required demo evidence was not observed: {required}")

            video = page.video
            context.close()
            if video is None:
                raise RuntimeError("Playwright did not create a video artifact.")
            video_path = Path(video.path())
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(video_path, destination)
            browser.close()

    if console_errors:
        raise RuntimeError(f"Console errors during demo: {console_errors}")
    if page_errors:
        raise RuntimeError(f"Page errors during demo: {page_errors}")
    if failed_requests:
        raise RuntimeError(f"Failed requests during demo: {failed_requests}")
    if external_requests:
        raise RuntimeError(f"External browser requests during demo: {external_requests}")


def _convert_to_gif(webm: Path, gif: Path) -> None:
    ffmpeg = _require_tool("ffmpeg")
    with tempfile.TemporaryDirectory() as temporary:
        palette = Path(temporary) / "palette.png"
        scale = f"fps=10,scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:flags=lanczos"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(webm),
                "-vf",
                f"{scale},palettegen=max_colors=96:stats_mode=diff",
                str(palette),
            ],
            check=True,
        )
        gif.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(webm),
                "-i",
                str(palette),
                "-lavfi",
                f"{scale}[x];[x][1:v]paletteuse="
                "dither=bayer:bayer_scale=3:diff_mode=rectangle",
                "-loop",
                "0",
                str(gif),
            ],
            check=True,
        )


def _validate_gif(path: Path) -> tuple[float, int]:
    ffprobe = _require_tool("ffprobe")
    header = path.read_bytes()[:10]
    if len(header) != 10 or header[:6] not in {b"GIF87a", b"GIF89a"}:
        raise RuntimeError("Showcase output is not a GIF file.")
    width, height = struct.unpack("<HH", header[6:10])
    if (width, height) != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
        raise RuntimeError(
            f"Unexpected GIF dimensions: {width}x{height}; "
            f"expected {OUTPUT_WIDTH}x{OUTPUT_HEIGHT}."
        )
    duration_result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(duration_result.stdout.strip())
    size = path.stat().st_size
    if duration > MAX_DURATION_SECONDS:
        raise RuntimeError(
            f"GIF duration {duration:.2f}s exceeds {MAX_DURATION_SECONDS:.0f}s."
        )
    if size > MAX_OUTPUT_BYTES:
        raise RuntimeError(
            f"GIF size {size} bytes exceeds the {MAX_OUTPUT_BYTES}-byte budget."
        )
    return duration, size


def _validate_overview(path: Path) -> int:
    header = path.read_bytes()[:24]
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[8:12] != struct.pack(">I", 13)
        or header[12:16] != b"IHDR"
    ):
        raise RuntimeError("Dashboard overview output is not a PNG file.")
    width, height = struct.unpack(">II", header[16:24])
    if (width, height) != (OVERVIEW_WIDTH, OVERVIEW_HEIGHT):
        raise RuntimeError(
            f"Unexpected overview dimensions: {width}x{height}; "
            f"expected {OVERVIEW_WIDTH}x{OVERVIEW_HEIGHT}."
        )
    size = path.stat().st_size
    if size > MAX_OVERVIEW_BYTES:
        raise RuntimeError(
            f"Overview size {size} bytes exceeds the {MAX_OVERVIEW_BYTES}-byte budget."
        )
    return size


def record_dashboard(
    output: Path, *, screenshot_output: Path | None = None
) -> tuple[float, int]:
    with tempfile.TemporaryDirectory() as temporary:
        webm = Path(temporary) / "dashboard-demo.webm"
        _record_webm(webm, screenshot_output)
        _convert_to_gif(webm, output)
    if screenshot_output is not None:
        _validate_overview(screenshot_output)
    return _validate_gif(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "images" / "dashboard-demo.gif",
    )
    parser.add_argument(
        "--screenshot-output",
        type=Path,
        help="Also capture the deterministic 1440x900 dashboard overview PNG.",
    )
    args = parser.parse_args()
    output = args.output.expanduser().resolve(strict=False)
    screenshot_output = (
        args.screenshot_output.expanduser().resolve(strict=False)
        if args.screenshot_output is not None
        else None
    )
    if screenshot_output == output:
        raise SystemExit("GIF and screenshot outputs must be different paths.")
    duration, size = record_dashboard(output, screenshot_output=screenshot_output)
    print(
        f"Dashboard demo validated: {output} "
        f"({OUTPUT_WIDTH}x{OUTPUT_HEIGHT}, {duration:.2f}s, {size} bytes)."
    )
    if screenshot_output is not None:
        print(
            f"Dashboard overview validated: {screenshot_output} "
            f"({OVERVIEW_WIDTH}x{OVERVIEW_HEIGHT}, "
            f"{screenshot_output.stat().st_size} bytes)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
