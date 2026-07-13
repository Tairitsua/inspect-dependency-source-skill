#!/usr/bin/env python3
"""Playwright smoke validation for the bilingual, stateful local dashboard."""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _catalog_dashboard import StoreDashboardProvider, running_server  # noqa: E402
from _catalog_store import CatalogStore, utc_now  # noqa: E402


class BrowserDemoProvider:
    """Real store projections plus deterministic high-volume event history."""

    def __init__(self, catalog_root: Path, source_root: Path) -> None:
        self.now = datetime.now(timezone.utc).isoformat()
        self.live_events: list[dict[str, object]] = []
        self.fail_repository_details = False
        self.store = CatalogStore(catalog_root)
        self.store.initialize()
        self._seed_catalog(catalog_root, source_root)
        self.adapter = StoreDashboardProvider(self.store)

    def append_event(self, event: dict[str, object]) -> None:
        """Publish a deterministic event after the initial catch-up has completed."""

        self.live_events.append(event)

    def _seed_catalog(self, catalog_root: Path, source_root: Path) -> None:
        alpha_source = source_root / "alpha"
        alpha_source.mkdir(parents=True)
        (alpha_source / "README.md").write_text("Alpha source", encoding="utf-8")
        subprocess.run(
            ["git", "init", "--initial-branch=release/2.4"],
            cwd=alpha_source,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Browser Validation"],
            cwd=alpha_source,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "browser-validation@example.invalid"],
            cwd=alpha_source,
            check=True,
        )
        subprocess.run(["git", "add", "README.md"], cwd=alpha_source, check=True)
        subprocess.run(
            ["git", "commit", "--message", "Seed browser validation source"],
            cwd=alpha_source,
            check=True,
            capture_output=True,
        )
        alpha_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=alpha_source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.alpha_commit = alpha_commit
        self.store.add_repository(
            repository_id="alpha",
            provider="github",
            canonical_name="acme/alpha",
            display_name="Acme / Alpha",
            remote_url="https://github.com/acme/alpha.git",
            origin_kind="github",
            aliases=("alpha-core",),
        )
        self.store.upsert_local_source_artifact(
            "alpha",
            {
                "id": "local-alpha",
                "path": str(alpha_source),
                "canonical_path": str(alpha_source),
                "detected_version": "2.4.1",
                "branch": "release/2.4",
                "commit_sha": alpha_commit,
                "dirty": False,
                "exists": True,
                "markers": ["README.md"],
            },
            {
                "id": "artifact-alpha-local",
                "kind": "local",
                "ref": "v2.4.1",
                "detected_version": "2.4.1",
                "source_path": str(alpha_source),
                "status": "ready",
                "resolution_kind": "exact_commit",
                "expected_commit": alpha_commit,
                "actual_commit": alpha_commit,
                "verification_state": "verified",
                "external": True,
            },
        )
        self.store.bind_package(
            {
                "id": "binding-alpha",
                "ecosystem": "nuget",
                "package_id": "Alpha.Core",
                "version": "2.4.1",
                "repository_id": "alpha",
                "artifact_id": "artifact-alpha-local",
                "requested_ref": "2.4.1",
                "resolved_ref": alpha_commit,
                "resolution_kind": "exact_commit",
                "expected_commit": alpha_commit,
            }
        )
        self.store.replace_tags(
            "alpha",
            [
                {"name": "v2.4.1", "commit_sha": alpha_commit},
                {"name": "v2.4.0", "commit_sha": None},
            ],
        )
        manifest = catalog_root / "repos" / "alpha" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"summary": "Reference implementation", "languages": ["Python"]}),
            encoding="utf-8",
        )

        beta_source = catalog_root / "repos" / "beta" / "artifact-beta" / "source"
        beta_source.mkdir(parents=True)
        (beta_source / "README.md").write_text("Beta source", encoding="utf-8")
        self.store.add_repository(
            repository_id="beta",
            provider="git",
            canonical_name="example/beta",
            display_name="Beta SDK",
            remote_url="https://example.invalid/beta.git",
            origin_kind="git",
            aliases=("beta",),
        )
        self.store.upsert_artifact(
            "beta",
            {
                "id": "artifact-beta",
                "kind": "git_clone",
                "ref": "v9.0.0",
                "detected_version": "9.0.0",
                "source_path": str(beta_source),
                "status": "ready",
                "resolution_kind": "exact_tag",
                "actual_commit": "b" * 40,
                "verification_state": "verified",
                "external": False,
            },
        )

        self.store.reconcile_cached_metrics()
        self.store.update_artifact_health(
            "artifact-alpha-local",
            status="ready",
            verification_state="verified",
            source_bytes=12,
            archive_bytes=0,
            file_count=1,
            actual_commit=alpha_commit,
        )
        self.store.update_artifact_health(
            "artifact-beta",
            status="invalid",
            verification_state="failed",
            source_bytes=None,
            archive_bytes=None,
            file_count=None,
            actual_commit="b" * 40,
        )
        self.fetch_operation_id = self.store.create_operation(
            "Fetch exact tag", repository_id="alpha", target="Acme / Alpha"
        )
        self.store.update_operation(
            self.fetch_operation_id,
            status="running",
            phase="verification",
            message="Verifying archive",
        )
        self.failed_operation_id = self.store.create_operation(
            "Verify source", repository_id="beta", target="Beta SDK"
        )
        self.store.update_operation(
            self.failed_operation_id,
            status="failed",
            phase="failed",
            message="Operation failed.",
            error_code="integrity_mismatch",
            error_message="Cached source digest does not match the verified snapshot.",
        )

    def summary(self):
        return self.adapter.summary()

    def repositories(self):
        return self.adapter.repositories()

    def repository(self, repository_id):
        if self.fail_repository_details:
            raise RuntimeError("Repository detail projection is temporarily unavailable.")
        return self.adapter.repository(repository_id)

    def tags(self, repository_id):
        if self.fail_repository_details:
            raise RuntimeError("Tag projection is temporarily unavailable.")
        return self.adapter.tags(repository_id)

    def operations(self):
        return self.adapter.operations()

    def events(self, after_sequence):
        events = []
        for sequence in range(1, 1101):
            events.append(
                {
                    "sequence": sequence,
                    "operation_id": "historical-operation",
                    "message": f"Historical checkpoint {sequence}",
                    "timestamp": self.now,
                }
            )
        for sequence in range(1101, 1151):
            events.append(
                {
                    "sequence": sequence,
                    "operation_id": self.fetch_operation_id,
                    "message": f"Current fetch checkpoint {sequence}",
                    "timestamp": self.now,
                    "progress": 82,
                }
            )
        for sequence in range(1151, 1206):
            events.append(
                {
                    "sequence": sequence,
                    "operation_id": self.failed_operation_id,
                    "message": f"Current verification checkpoint {sequence}",
                    "timestamp": self.now,
                }
            )
        events.extend(self.live_events)
        return [event for event in events if event["sequence"] > after_sequence][:500]


def assert_no_horizontal_overflow(page) -> None:
    dimensions = page.evaluate(
        "() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth })"
    )
    assert dimensions["scroll"] <= dimensions["width"], dimensions


def run_validation(*, headed: bool = False) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is required for browser validation. Install it only for development, "
            "then run `playwright install chromium`."
        ) from exc

    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    external_requests: list[str] = []
    event_request_times: list[float] = []

    with contextlib.ExitStack() as stack:
        workspace_root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        catalog_root = workspace_root / "catalog"
        provider = BrowserDemoProvider(catalog_root, workspace_root / "user-sources")
        server = stack.enter_context(
            running_server(provider, instance_id="browser-validation")
        )
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not headed)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                color_scheme="dark",
                reduced_motion="reduce",
            )
            page = context.new_page()
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("requestfailed", lambda request: failed_requests.append(f"{request.url}: {request.failure}"))

            def observe_request(request):
                if not request.url.startswith(base_url):
                    external_requests.append(request.url)
                if "/api/v1/events?" in request.url:
                    event_request_times.append(time.monotonic())

            page.on("request", observe_request)
            page.goto(base_url, wait_until="domcontentloaded")
            page.locator("#sync-state.online").wait_for(timeout=5000)

            assert page.locator("html").get_attribute("data-theme") == "system"
            assert page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches")
            assert page.evaluate(
                "() => getComputedStyle(document.documentElement).getPropertyValue('--canvas').trim()"
            ) == "#101713"
            assert page.evaluate("() => matchMedia('(prefers-reduced-motion: reduce)').matches")
            assert page.evaluate(
                "() => getComputedStyle(document.documentElement).scrollBehavior"
            ) == "auto"
            assert page.get_by_role("heading", name="Inspect Dependency Source").count() == 1
            assert page.get_by_role("heading", name="Catalog health").count() == 1
            assert page.locator("#repository-list .repository-button").count() == 2
            assert page.get_by_role("heading", name="Acme / Alpha").count() == 1
            assert "Repositories" in page.locator("#metric-grid").inner_text()
            stale_metric = page.locator(".metric-card").filter(has_text="Stale tag indexes")
            assert stale_metric.count() == 1
            assert "1" in stale_metric.inner_text()
            page.wait_for_function(
                "() => Number(document.querySelector('#operation-list').dataset.eventSequence) === 1205",
                timeout=5000,
            )
            assert len(event_request_times) >= 3
            assert event_request_times[-1] - event_request_times[0] < 2.5, event_request_times
            assert int(page.locator("#operation-list").get_attribute("data-retained-event-count")) <= 360

            package_card = page.locator(".detail-card").filter(has_text="Package provenance")
            assert "Alpha.Core" in package_card.inner_text()
            assert "Exact commit" in package_card.inner_text()
            local_card = page.locator(".detail-card").filter(has_text="Local source snapshots")
            local_text = local_card.inner_text()
            assert "release/2.4" in local_text
            assert provider.alpha_commit[:16] in local_text
            assert "Clean" in local_text

            page.locator("#repository-search").fill("Alpha.Core")
            assert page.locator("#repository-list .repository-button").count() == 1
            assert "Acme / Alpha" in page.locator("#repository-list").inner_text()
            page.locator("#repository-search").fill("")
            page.locator("#repository-filter").select_option("local")
            assert page.locator("#repository-list .repository-button").count() == 1
            assert "Acme / Alpha" in page.locator("#repository-list").inner_text()
            page.locator("#repository-filter").select_option("verified")
            assert page.locator("#repository-list .repository-button").count() == 1
            assert "Beta SDK" not in page.locator("#repository-list").inner_text()
            page.locator("#repository-filter").select_option("all")

            expected_console_start = len(console_errors)
            provider.fail_repository_details = True
            page.locator("#error-toast:not([hidden])").wait_for(timeout=5000)
            assert "temporarily unavailable" in page.locator("#error-toast").inner_text()
            provider.fail_repository_details = False
            page.get_by_role("heading", name="Acme / Alpha").wait_for(timeout=5000)
            page.wait_for_function("() => document.querySelector('#error-toast').hidden", timeout=5000)
            expected_errors = console_errors[expected_console_start:]
            assert expected_errors and all("500 (Internal Server Error)" in item for item in expected_errors)
            del console_errors[expected_console_start:]

            provider.append_event(
                {
                    "sequence": 1206,
                    "operation_id": provider.fetch_operation_id,
                    "message": "Incremental checkpoint 1206",
                    "timestamp": provider.now,
                    "progress": 96,
                }
            )
            page.wait_for_function(
                "() => Number(document.querySelector('#operation-list').dataset.eventSequence) === 1206",
                timeout=5000,
            )

            page.locator("button[data-repository-id='beta']").click()
            page.get_by_role("heading", name="Beta SDK").wait_for()

            first_timeline = page.locator(
                f"details[data-operation-id='{provider.fetch_operation_id}']"
            )
            first_timeline.locator("summary").click()
            assert first_timeline.get_attribute("open") is not None
            assert "Current fetch checkpoint 1150" in first_timeline.inner_text()
            assert first_timeline.inner_text().count("Incremental checkpoint 1206") == 1
            assert first_timeline.locator(".timeline-event").count() <= 120
            failed_operation = page.locator(
                f"details[data-operation-id='{provider.failed_operation_id}']"
            )
            failed_operation.locator("summary").click()
            assert "integrity_mismatch" in failed_operation.inner_text()
            assert "digest does not match" in failed_operation.inner_text()
            page.wait_for_timeout(2400)
            first_timeline = page.locator(
                f"details[data-operation-id='{provider.fetch_operation_id}']"
            )
            assert first_timeline.get_attribute("open") is not None, "timeline collapsed after refresh"

            page.locator("#language-select").select_option("zh-CN")
            page.get_by_role("heading", name="目录健康状态").wait_for()
            page.locator("#theme-select").select_option("dark")
            page.locator("#repository-search").fill("Alpha")
            page.locator("#repository-filter").select_option("local")
            assert page.locator("#repository-list .repository-button").count() == 1

            page.reload(wait_until="domcontentloaded")
            page.locator("#sync-state.online").wait_for(timeout=5000)
            assert page.locator("#language-select").input_value() == "zh-CN"
            assert page.locator("#theme-select").input_value() == "dark"
            assert page.locator("#repository-search").input_value() == "Alpha"
            assert page.locator("#repository-filter").input_value() == "local"
            assert page.locator("html").get_attribute("data-theme") == "dark"
            page.get_by_role("heading", name="Beta SDK").wait_for()
            assert page.locator(
                f"details[data-operation-id='{provider.fetch_operation_id}']"
            ).get_attribute("open") is not None

            page.set_viewport_size({"width": 360, "height": 780})
            assert_no_horizontal_overflow(page)
            page.set_viewport_size({"width": 768, "height": 900})
            assert_no_horizontal_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 1000})
            assert_no_horizontal_overflow(page)

            assert page.get_by_role("main").count() == 1
            assert page.get_by_role("navigation", name="代码仓库清单").count() == 1
            assert page.get_by_label("语言").count() == 1
            assert page.get_by_label("主题").count() == 1
            assert page.get_by_label("筛选代码仓库").count() == 1
            aria_snapshot = page.locator("body").aria_snapshot()
            assert "目录健康状态" in aria_snapshot
            assert "Beta SDK" in aria_snapshot

            browser.close()

    assert not console_errors, f"console errors: {console_errors}"
    assert not page_errors, f"page errors: {page_errors}"
    assert not failed_requests, f"failed requests: {failed_requests}"
    assert not external_requests, f"unexpected external requests: {external_requests}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="Show Chromium during validation.")
    args = parser.parse_args()
    run_validation(headed=args.headed)
    print("Dashboard browser validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
