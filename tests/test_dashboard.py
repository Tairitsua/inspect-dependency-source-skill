"""Standard-library tests for the local dashboard and its lifecycle manager."""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _catalog_dashboard as dashboard_module  # noqa: E402
from _catalog_dashboard import (  # noqa: E402
    DashboardError,
    DashboardReconciler,
    MAX_DASHBOARD_LOG_FILE_BYTES,
    MAX_DASHBOARD_LOG_LINE_CHARACTERS,
    MAX_DASHBOARD_LOG_READ_BYTES,
    MAX_DASHBOARD_METADATA_BYTES,
    MAX_RECONCILIATION_DIAGNOSTIC_LINES,
    StoreDashboardProvider,
    _read_dashboard_log_tail,
    dashboard_status,
    running_server,
    start_dashboard,
    stop_dashboard,
)


class FakeProvider:
    """Deterministic dashboard projection provider used by HTTP tests."""

    def summary(self):
        return {
            "repository_count": 2,
            "artifact_count": 3,
            "verified_exact_count": 1,
            "manifest_ready_count": 1,
            "active_operation_count": 1,
            "failed_operation_count": 0,
            "stale_tag_count": 1,
            "integrity_warning_count": 1,
            "disk_used_bytes": 2048,
            "api_token": "must-not-leak",
        }

    def repositories(self):
        return [
            {
                "id": "repo-alpha",
                "name": "Alpha",
                "provider": "github",
                "remote_url": "https://alice:hunter2@example.test/acme/alpha.git",
                "verification_state": "verified",
                "resolution_provenance": "exact_tag",
                "package_search_terms": ["Alpha.Core 1.0.0"],
            },
            {
                "id": "repo-local",
                "name": "Local",
                "provider": "git",
                "has_local_source": True,
            },
        ]

    def repository(self, repository_id):
        if repository_id != "repo-alpha":
            return None
        return {
            "id": repository_id,
            "name": "Alpha",
            "artifacts": [{"id": "artifact-1", "ref": "v1.0.0"}],
            "package_bindings": [
                {
                    "package_id": "Alpha.Core",
                    "version": "1.0.0",
                    "resolution_kind": "exact_tag",
                }
            ],
        }

    def tags(self, repository_id):
        return ["v1.0.0", "v0.9.0"] if repository_id == "repo-alpha" else None

    def operations(self):
        return [
            {
                "id": "operation-1",
                "kind": "fetch",
                "status": "running",
                "events": [{"sequence": 1, "message": "Downloading"}],
            }
        ]

    def events(self, after_sequence):
        events = [
            {"sequence": 1, "operation_id": "operation-1", "message": "Downloading"},
            {"sequence": 2, "operation_id": "operation-1", "message": "Extracting"},
        ]
        return [event for event in events if event["sequence"] > after_sequence]


def wait_until(predicate, *, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class DashboardHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_context = running_server(FakeProvider(), instance_id="test-instance")
        cls.server = cls.server_context.__enter__()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server_context.__exit__(None, None, None)

    def request_json(self, path):
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
            return response, json.loads(response.read())

    def test_health_and_security_headers(self):
        response, payload = self.request_json("/api/v1/health")
        self.assertEqual(200, response.status)
        self.assertEqual("ok", payload["status"])
        self.assertEqual("inspect-dependency-source-dashboard", payload["service"])
        self.assertEqual("test-instance", payload["instance_id"])
        self.assertFalse(payload["reconciliation"]["enabled"])
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))

    def test_summary_is_redacted(self):
        _response, payload = self.request_json("/api/v1/summary")
        self.assertEqual(2, payload["summary"]["repository_count"])
        self.assertEqual("[redacted]", payload["summary"]["api_token"])

    def test_provider_errors_redact_inline_and_bearer_secrets(self):
        github_token = "ghp_" + "a" * 24
        environment_token = "dashboard-environment-token-canary"

        class FailingProvider(FakeProvider):
            def summary(self):
                raise RuntimeError(
                    "token=raw-value Authorization: Bearer abc.def.ghi "
                    f"github={github_token} environment={environment_token}"
                )

        with mock.patch.dict(os.environ, {"GH_TOKEN": environment_token}, clear=False):
            with running_server(FailingProvider()) as server:
                port = server.server_address[1]
                with self.assertRaises(HTTPError) as error:
                    urlopen(f"http://127.0.0.1:{port}/api/v1/summary", timeout=2)
                payload = json.loads(error.exception.read())
            safe_log = dashboard_module._safe_log_line(
                f"provider rejected {github_token} using {environment_token}\nforged"
            )
        serialized = json.dumps(payload)
        self.assertNotIn("raw-value", serialized)
        self.assertNotIn("abc.def.ghi", serialized)
        self.assertNotIn(github_token, serialized)
        self.assertNotIn(environment_token, serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertNotIn(github_token, safe_log)
        self.assertNotIn(environment_token, safe_log)
        self.assertNotIn("\n", safe_log)
        self.assertIn(r"\u000a", safe_log)

    def test_unexpected_provider_error_still_returns_a_redacted_envelope(self):
        class BrokenProvider(FakeProvider):
            def summary(self):
                raise KeyError("token=dashboard-secret")

        with running_server(BrokenProvider()) as server:
            port = server.server_address[1]
            with self.assertRaises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{port}/api/v1/summary", timeout=2)
            payload = json.loads(error.exception.read())

        self.assertEqual(payload["error"]["code"], "dashboard_provider_error")
        self.assertNotIn("dashboard-secret", json.dumps(payload))

    def test_repository_inventory_and_remote_credentials_are_redacted(self):
        _response, payload = self.request_json("/api/v1/repositories")
        self.assertEqual(2, payload["count"])
        remote = payload["repositories"][0]["remote_url"]
        self.assertEqual("https://example.test/acme/alpha.git", remote)
        self.assertNotIn("hunter2", json.dumps(payload))

    def test_repository_detail_and_tags(self):
        _response, detail = self.request_json("/api/v1/repositories/repo-alpha")
        self.assertEqual("Alpha", detail["repository"]["name"])
        _response, tags = self.request_json("/api/v1/repositories/repo-alpha/tags")
        self.assertEqual(["v1.0.0", "v0.9.0"], tags["tags"])

        with self.assertRaises(HTTPError) as missing:
            urlopen(f"http://127.0.0.1:{self.port}/api/v1/repositories/missing", timeout=2)
        self.assertEqual(404, missing.exception.code)

    def test_operations_and_incremental_events(self):
        _response, operations = self.request_json("/api/v1/operations")
        self.assertEqual("operation-1", operations["operations"][0]["id"])
        _response, events = self.request_json("/api/v1/events?after_sequence=1")
        self.assertEqual([2], [event["sequence"] for event in events["events"]])
        self.assertEqual(2, events["last_sequence"])

    def test_event_cursor_rejects_malformed_values(self):
        for query in (
            "after_sequence=-1",
            "after_sequence=nope",
            "after_sequence=999999999999999999999999999999",
            "other=1",
            "after_sequence=1&other=2",
        ):
            with self.subTest(query=query), self.assertRaises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{self.port}/api/v1/events?{query}", timeout=2)
            self.assertEqual(400, error.exception.code)

    def test_head_has_get_headers_without_a_body(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request("HEAD", "/api/v1/repositories")
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(200, response.status)
        self.assertGreater(int(response.getheader("Content-Length")), 0)
        self.assertEqual(b"", body)
        connection.close()

    def test_mutating_methods_are_not_allowed_and_do_not_enable_cors(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request("POST", "/api/v1/repositories", body=b"{}")
        response = connection.getresponse()
        self.assertEqual(405, response.status)
        self.assertEqual("GET, HEAD", response.getheader("Allow"))
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        response.read()
        connection.close()

    def test_arbitrary_http_methods_use_hardened_method_not_allowed_response(self):
        for method in ("PROPFIND", "BREW"):
            with self.subTest(method=method):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                connection.request(method, "/api/v1/summary")
                response = connection.getresponse()
                self.assertEqual(405, response.status)
                self.assertEqual("GET, HEAD", response.getheader("Allow"))
                self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
                self.assertEqual("no-store", response.getheader("Cache-Control"))
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
                self.assertEqual(b"", response.read())
                connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(
            "PROPFIND",
            "/api/v1/summary",
            headers={"Host": "attacker.example"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(421, response.status)
        self.assertEqual("untrusted_host", payload["error"]["code"])
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
        connection.close()

    def test_dns_rebinding_host_and_cross_origin_requests_are_rejected(self):
        cases = (
            ({"Host": "attacker.example"}, 421, "untrusted_host"),
            ({"Host": f"localhost:{self.port}"}, 421, "untrusted_host"),
            (
                {
                    "Host": f"127.0.0.1:{self.port}",
                    "Origin": "http://attacker.example",
                },
                403,
                "untrusted_origin",
            ),
            (
                {
                    "Host": f"127.0.0.1:{self.port}",
                    "Origin": f"http://127.0.0.1:{self.port}/",
                },
                403,
                "untrusted_origin",
            ),
        )
        for headers, expected_status, expected_code in cases:
            with self.subTest(headers=headers):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                connection.request("GET", "/api/v1/health", headers=headers)
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(expected_status, response.status)
                self.assertEqual(expected_code, payload["error"]["code"])
                self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
                connection.close()

    def test_exact_dashboard_origin_is_accepted(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(
            "GET",
            "/api/v1/health",
            headers={
                "Host": f"127.0.0.1:{self.port}",
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(200, response.status)
        self.assertEqual("ok", payload["status"])
        connection.close()

    def test_missing_or_duplicate_host_is_rejected(self):
        for host_values in ((), (f"127.0.0.1:{self.port}", "attacker.example")):
            with self.subTest(host_values=host_values):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                connection.putrequest("GET", "/api/v1/health", skip_host=True)
                for value in host_values:
                    connection.putheader("Host", value)
                connection.endheaders()
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(421, response.status)
                self.assertEqual("untrusted_host", payload["error"]["code"])
                connection.close()

    def test_server_exposes_only_allowlisted_assets(self):
        with urlopen(f"http://127.0.0.1:{self.port}/", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn("Inspect Dependency Source", page)
        for path in (
            "/assets/../scripts/_catalog_dashboard.py",
            "/assets/%2e%2e/scripts/_catalog_dashboard.py",
            "/api/v1/repositories/..%2Fsecret",
            "/api/v1/repositories/repo%20alpha",
            "/favicon.ico",
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=2)
            self.assertIn(error.exception.code, (400, 404))

    def test_rejected_request_is_not_written_to_diagnostic_log(self):
        captured = io.StringIO()
        request = (
            b"GET /missing?token=log-canary;\x1b[31mforged HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{self.port}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
        )
        with mock.patch.object(sys, "stderr", captured):
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as connection:
                connection.sendall(request)
                while connection.recv(4096):
                    pass

        log = captured.getvalue()
        self.assertEqual("", log)


class DashboardLifecycleTests(unittest.TestCase):
    @staticmethod
    def invoke_for_lifecycle_file(root, filename):
        if filename == "dashboard.log":
            return start_dashboard(root)
        return dashboard_status(root)

    def test_start_reuse_status_and_stop_are_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = start_dashboard(root)
            self.assertTrue(started["running"])
            self.assertFalse(started["reused"])
            self.assertEqual("127.0.0.1", started["host"])

            reused = start_dashboard(root)
            self.assertTrue(reused["running"])
            self.assertTrue(reused["reused"])
            self.assertEqual(started["pid"], reused["pid"])
            self.assertEqual(started["instance_id"], reused["instance_id"])

            status = dashboard_status(root)
            self.assertTrue(status["running"])
            self.assertEqual(started["url"], status["url"])

            stopped = stop_dashboard(root)
            self.assertTrue(stopped["stopped"])
            self.assertFalse(dashboard_status(root)["running"])

    def test_windows_lock_byte_initialization_does_not_grow_append_mode_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "dashboard.lock"
            for _ in range(20):
                with lock.open("a+b", buffering=0) as handle:
                    dashboard_module._prepare_windows_lock_byte(handle)
                    self.assertEqual(0, handle.tell())
            self.assertEqual(b"0", lock.read_bytes())

    def test_repeated_lifecycle_checks_keep_lock_file_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for _ in range(20):
                self.assertFalse(dashboard_status(root)["running"])
            expected_size = 1 if os.name == "nt" else 0
            self.assertEqual(expected_size, (root / "state" / "dashboard.lock").stat().st_size)

    def test_rejected_request_spam_does_not_grow_dashboard_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = start_dashboard(root)
            log = root / "state" / "dashboard.log"
            try:
                initial_size = log.stat().st_size
                for index in range(50):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(
                            f"{started['url']}missing?token=request-{index}", timeout=2
                        )
                    error.exception.close()
                self.assertEqual(initial_size, log.stat().st_size)
                self.assertLessEqual(log.stat().st_size, MAX_DASHBOARD_LOG_FILE_BYTES)
            finally:
                stop_dashboard(root)

    def test_status_recovers_from_stale_process_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata = root / "state" / "dashboard.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(
                json.dumps(
                    {
                        "pid": 2_000_000_000,
                        "port": 45161,
                        "instance_id": "stale-instance",
                        "started_at": "2026-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(dashboard_status(root)["running"])
            self.assertFalse(metadata.exists())

    @unittest.skipIf(sys.platform == "win32", "Symlink creation depends on Windows policy.")
    def test_state_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            (root / "state").symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaises(DashboardError):
                dashboard_status(root)
            self.assertFalse((Path(outside) / "dashboard.json").exists())

    @unittest.skipIf(sys.platform == "win32", "Symlink creation depends on Windows policy.")
    def test_precreated_lifecycle_file_symlinks_are_rejected_without_touching_target(self):
        for filename in ("dashboard.json", "dashboard.lock", "dashboard.log"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
                root = Path(temporary)
                state = root / "state"
                state.mkdir()
                target = Path(outside) / "protected.txt"
                target.write_text("protected", encoding="utf-8")
                (state / filename).symlink_to(target)

                with self.assertRaises(DashboardError):
                    self.invoke_for_lifecycle_file(root, filename)
                self.assertEqual("protected", target.read_text(encoding="utf-8"))

    def test_precreated_non_regular_lifecycle_entries_are_rejected(self):
        for filename in ("dashboard.json", "dashboard.lock", "dashboard.log"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                entry = root / "state" / filename
                entry.mkdir(parents=True)
                with self.assertRaises(DashboardError):
                    self.invoke_for_lifecycle_file(root, filename)

    def test_precreated_lifecycle_hardlinks_are_rejected_without_clobbering_victim(self):
        for filename in ("dashboard.json", "dashboard.lock", "dashboard.log"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "catalog"
                state = root / "state"
                state.mkdir(parents=True)
                victim = Path(temporary) / "victim.txt"
                victim.write_text("protected-hardlink-victim", encoding="utf-8")
                try:
                    (state / filename).hardlink_to(victim)
                except OSError as exc:
                    self.skipTest(f"Hard links are unavailable on this filesystem: {exc}")

                with self.assertRaises(DashboardError):
                    self.invoke_for_lifecycle_file(root, filename)
                self.assertEqual(
                    "protected-hardlink-victim", victim.read_text(encoding="utf-8")
                )

    def test_oversized_metadata_is_rejected_without_unbounded_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata = root / "state" / "dashboard.json"
            metadata.parent.mkdir()
            metadata.write_bytes(b"{" + b"x" * MAX_DASHBOARD_METADATA_BYTES + b"}")
            with self.assertRaises(DashboardError):
                dashboard_status(root)

    def test_log_tail_read_is_bounded_and_large_log_is_rotated_before_append(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "state" / "dashboard.log"
            log.parent.mkdir()
            log.write_bytes(
                b"old-prefix\n" * (MAX_DASHBOARD_LOG_READ_BYTES // 5) + b"bounded-tail-marker\n"
            )
            tail = _read_dashboard_log_tail(root)
            self.assertLessEqual(len(tail.encode("utf-8")), 1200)
            self.assertTrue(tail.endswith("bounded-tail-marker"))

            log.write_bytes(b"x" * (MAX_DASHBOARD_LOG_FILE_BYTES + 1))
            started = start_dashboard(root)
            try:
                self.assertLessEqual(log.stat().st_size, MAX_DASHBOARD_LOG_FILE_BYTES)
            finally:
                stop_dashboard(root)

    def test_occupied_port_fails_without_metadata(self):
        with tempfile.TemporaryDirectory() as temporary, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            root = Path(temporary)
            with self.assertRaises(DashboardError):
                start_dashboard(root, port=port)
            self.assertFalse((root / "state" / "dashboard.json").exists())

    def test_metadata_write_failure_terminates_spawned_dashboard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spawned_pid: int | None = None

            def fail_metadata_write(_root, process):
                nonlocal spawned_pid
                spawned_pid = process.pid
                raise OSError("simulated metadata write failure")

            with mock.patch.object(
                dashboard_module, "_write_process", side_effect=fail_metadata_write
            ):
                with self.assertRaisesRegex(OSError, "simulated metadata write failure"):
                    start_dashboard(root)

            self.assertIsNotNone(spawned_pid)
            assert spawned_pid is not None
            self.assertNotIn(spawned_pid, dashboard_module._OWNED_PROCESSES)
            self.assertTrue(
                wait_until(lambda: not dashboard_module._pid_exists(spawned_pid), timeout=3)
            )
            self.assertFalse((root / "state" / "dashboard.json").exists())


class DashboardReconciliationTests(unittest.TestCase):
    class CountingProvider(FakeProvider):
        def __init__(self, *, fail_first=False):
            self._lock = threading.Lock()
            self.call_count = 0
            self.fail_first = fail_first

        def reconcile_cached_metrics(self):
            with self._lock:
                self.call_count += 1
                call_count = self.call_count
            if self.fail_first and call_count == 1:
                raise RuntimeError("token=must-not-leak")

        def count(self):
            with self._lock:
                return self.call_count

    def test_optional_reconciliation_runs_on_cadence_and_stops_with_server(self):
        provider = self.CountingProvider()
        with running_server(
            provider,
            reconciliation_initial_delay=0.01,
            reconciliation_interval=0.03,
        ) as server:
            self.assertTrue(wait_until(lambda: provider.count() >= 2))
            port = server.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2) as response:
                reconciliation = json.loads(response.read())["reconciliation"]
            self.assertTrue(reconciliation["enabled"])
            self.assertTrue(reconciliation["worker_alive"])
            self.assertGreaterEqual(reconciliation["run_count"], 2)
            self.assertIsNotNone(reconciliation["last_completed_at"])
        stopped_count = provider.count()
        time.sleep(0.08)
        self.assertEqual(stopped_count, provider.count())

    def test_reconciliation_failure_does_not_stop_future_runs_or_leak_secret(self):
        provider = self.CountingProvider(fail_first=True)
        with running_server(
            provider,
            reconciliation_initial_delay=0,
            reconciliation_interval=0.02,
        ) as server:
            self.assertTrue(wait_until(lambda: provider.count() >= 2))
            port = server.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2) as response:
                health = json.loads(response.read())
            self.assertGreaterEqual(health["reconciliation"]["run_count"], 2)
            self.assertNotIn("must-not-leak", json.dumps(health))

    def test_reconciliation_diagnostics_have_a_finite_runtime_log_budget(self):
        class VaryingFailureProvider(self.CountingProvider):
            def reconcile_cached_metrics(self):
                with self._lock:
                    self.call_count += 1
                    call_count = self.call_count
                raise RuntimeError(f"failure-{call_count} token=must-not-leak " + "x" * 5000)

        provider = VaryingFailureProvider()
        reconciler = DashboardReconciler(provider)
        captured = io.StringIO()
        with mock.patch.object(sys, "stderr", captured):
            for _ in range(MAX_RECONCILIATION_DIAGNOSTIC_LINES + 8):
                reconciler._reconcile_once()

        lines = captured.getvalue().splitlines()
        self.assertEqual(MAX_RECONCILIATION_DIAGNOSTIC_LINES + 8, provider.count())
        self.assertEqual(MAX_RECONCILIATION_DIAGNOSTIC_LINES, len(lines))
        self.assertTrue(all(len(line) <= MAX_DASHBOARD_LOG_LINE_CHARACTERS for line in lines))
        self.assertNotIn("must-not-leak", captured.getvalue())

    def test_slow_reconciliation_never_blocks_summary_requests(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingProvider(FakeProvider):
            def reconcile_cached_metrics(self):
                started.set()
                release.wait(timeout=2)

        provider = BlockingProvider()
        try:
            with running_server(
                provider,
                reconciliation_initial_delay=0,
                reconciliation_interval=60,
            ) as server:
                self.assertTrue(started.wait(timeout=1))
                before = time.monotonic()
                with urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/api/v1/summary", timeout=1
                ) as response:
                    payload = json.loads(response.read())
                elapsed = time.monotonic() - before
                self.assertEqual(2, payload["summary"]["repository_count"])
                self.assertLess(elapsed, 0.3)
                release.set()
        finally:
            release.set()


class StoreAdapterTests(unittest.TestCase):
    def test_current_catalog_store_shape_is_projected_without_schema_leakage(self):
        from _catalog_store import CatalogStore

        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as source_temporary:
            store = CatalogStore(Path(temporary))
            store.initialize()
            store.add_repository(
                repository_id="local-one",
                provider="local",
                canonical_name="local-one",
                display_name="Local One",
                remote_url=None,
                origin_kind="local",
                aliases=["one"],
            )
            source_path = Path(source_temporary) / "local-source"
            source_path.mkdir()
            (source_path / "README.md").write_text("local source", encoding="utf-8")
            store.upsert_local_source_artifact(
                "local-one",
                {
                    "id": "local-source-one",
                    "path": str(source_path),
                    "canonical_path": str(source_path),
                    "detected_version": "1.0.0",
                    "branch": "release/1.0",
                    "commit_sha": "a" * 40,
                    "dirty": False,
                    "exists": True,
                    "markers": ["README.md"],
                },
                {
                    "id": "artifact-local-one",
                    "kind": "local",
                    "ref": "v1.0.0",
                    "detected_version": "1.0.0",
                    "source_path": str(source_path),
                    "status": "ready",
                    "resolution_kind": "exact_commit",
                    "actual_commit": "a" * 40,
                    "verification_state": "verified",
                    "external": True,
                },
            )
            store.bind_package(
                {
                    "id": "binding-one",
                    "ecosystem": "nuget",
                    "package_id": "Local.One",
                    "version": "1.0.0",
                    "repository_id": "local-one",
                    "artifact_id": "artifact-local-one",
                    "requested_ref": "1.0.0",
                    "resolved_ref": "v1.0.0",
                    "resolution_kind": "exact_commit",
                    "expected_commit": "a" * 40,
                }
            )
            provider = StoreDashboardProvider(store)
            summary = provider.summary()
            repositories = provider.repositories()
            detail = provider.repository("local-one")

            self.assertEqual(1, summary["repository_count"])
            self.assertIn("disk_used_bytes", summary)
            self.assertEqual("Local One", repositories[0]["name"])
            self.assertTrue(repositories[0]["has_local_source"])
            self.assertEqual(["Local.One 1.0.0"], repositories[0]["package_search_terms"])
            self.assertEqual("verified", repositories[0]["verification_state"])
            self.assertEqual("Local One", detail["name"])
            self.assertEqual("release/1.0", detail["local_sources"][0]["branch"])
            self.assertEqual(
                "exact_commit", detail["package_bindings"][0]["resolution_provenance"]
            )
            self.assertEqual([], provider.tags("local-one"))
            self.assertIsNone(provider.repository("missing"))

    def test_catalog_service_dashboard_process_reads_the_real_store(self):
        from _catalog_service import CatalogService

        with tempfile.TemporaryDirectory() as temporary:
            service = CatalogService(Path(temporary))
            service.store.add_repository(
                repository_id="service-repository",
                provider="local",
                canonical_name="service-repository",
                display_name="Service Repository",
                remote_url=None,
                origin_kind="local",
                aliases=["service"],
            )
            initialized = service.initialize(dashboard=True)
            try:
                dashboard = initialized["dashboard"]
                self.assertTrue(dashboard["running"])
                with urlopen(f"{dashboard['url']}api/v1/summary", timeout=2) as response:
                    summary = json.loads(response.read())["summary"]
                with urlopen(f"{dashboard['url']}api/v1/repositories", timeout=2) as response:
                    repositories = json.loads(response.read())["repositories"]
                with urlopen(
                    f"{dashboard['url']}api/v1/repositories/service-repository", timeout=2
                ) as response:
                    detail = json.loads(response.read())["repository"]

                latest_summary = {}

                def reconciliation_completed():
                    nonlocal latest_summary
                    try:
                        with urlopen(f"{dashboard['url']}api/v1/summary", timeout=0.5) as response:
                            latest_summary = json.loads(response.read())["summary"]
                    except (TimeoutError, URLError):
                        return False
                    return bool(latest_summary.get("reconciled_at"))

                self.assertEqual(1, summary["repository_count"])
                self.assertEqual("Service Repository", repositories[0]["name"])
                self.assertEqual("Service Repository", detail["name"])
                self.assertTrue(wait_until(reconciliation_completed, timeout=5))
                with urlopen(f"{dashboard['url']}api/v1/health", timeout=2) as response:
                    reconciliation = json.loads(response.read())["reconciliation"]
                self.assertTrue(reconciliation["enabled"])
                self.assertGreaterEqual(reconciliation["run_count"], 1)
                self.assertTrue(service.dashboard("status")["running"])
            finally:
                service.dashboard("stop")


if __name__ == "__main__":
    unittest.main()
