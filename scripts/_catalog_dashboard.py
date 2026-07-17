#!/usr/bin/env python3
"""Local, read-only dashboard for Inspect Dependency Source.

The HTTP layer deliberately depends on a small read-only provider protocol. The
catalog store remains the only component allowed to understand the persistence
schema; this module only serializes already-projected dashboard views.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from _catalog_models import CatalogError
from _catalog_paths import is_link_or_reparse, is_windows_reparse_point, redact_text

HOST = "127.0.0.1"
SERVICE_NAME = "inspect-dependency-source-dashboard"
API_VERSION = 1
DEFAULT_START_TIMEOUT_SECONDS = 8.0
DEFAULT_STOP_TIMEOUT_SECONDS = 8.0
DEFAULT_RECONCILIATION_INITIAL_DELAY_SECONDS = 0.25
DEFAULT_RECONCILIATION_INTERVAL_SECONDS = 60.0
DEFAULT_RECONCILIATION_STOP_TIMEOUT_SECONDS = 2.0
MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DASHBOARD_METADATA_BYTES = 64 * 1024
MAX_DASHBOARD_LOG_READ_BYTES = 64 * 1024
MAX_DASHBOARD_LOG_FILE_BYTES = 2 * 1024 * 1024
MAX_DASHBOARD_LOG_LINE_CHARACTERS = 4096
MAX_RECONCILIATION_DIAGNOSTIC_LINES = 16
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'none'; frame-ancestors 'none'; "
        "img-src 'self' data:; manifest-src 'self'; object-src 'none'; "
        "script-src 'self'; style-src 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "microphone=(), payment=(), usb=()"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
SENSITIVE_KEY_PARTS = (
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
LOG_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_OWNED_PROCESSES: dict[int, subprocess.Popen[Any]] = {}


class DashboardError(CatalogError):
    """Raised when dashboard lifecycle management cannot complete safely."""

    code = "dashboard_error"


@runtime_checkable
class DashboardProvider(Protocol):
    """Read-only projections consumed by the dashboard HTTP API."""

    def summary(self) -> Mapping[str, Any]:
        """Return aggregate catalog and cache health metrics."""

    def repositories(self) -> Sequence[Mapping[str, Any]]:
        """Return searchable repository summaries."""

    def repository(self, repository_id: str) -> Mapping[str, Any] | None:
        """Return details for one repository, or ``None`` when absent."""

    def tags(self, repository_id: str) -> Sequence[Mapping[str, Any] | str] | None:
        """Return cached tags for one repository, or ``None`` when absent."""

    def operations(self) -> Sequence[Mapping[str, Any]]:
        """Return recent and active operations, newest first."""

    def events(self, after_sequence: int) -> Sequence[Mapping[str, Any]]:
        """Return operation events whose sequence is greater than the cursor."""


class UnavailableDashboardProvider:
    """Safe empty provider used until the catalog storage adapter is available."""

    def __init__(self, message: str | None = None) -> None:
        self._message = message

    def summary(self) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "repository_count": 0,
            "artifact_count": 0,
            "verified_exact_count": 0,
            "manifest_ready_count": 0,
            "active_operation_count": 0,
            "failed_operation_count": 0,
            "stale_tag_count": 0,
            "integrity_warning_count": 0,
            "disk_used_bytes": 0,
            "disk_free_bytes": None,
            "reconciled_at": None,
        }
        if self._message:
            result["notice"] = self._message
        return result

    def repositories(self) -> Sequence[Mapping[str, Any]]:
        return ()

    def repository(self, repository_id: str) -> Mapping[str, Any] | None:
        del repository_id
        return None

    def tags(self, repository_id: str) -> Sequence[Mapping[str, Any] | str] | None:
        del repository_id
        return None

    def operations(self) -> Sequence[Mapping[str, Any]]:
        return ()

    def events(self, after_sequence: int) -> Sequence[Mapping[str, Any]]:
        del after_sequence
        return ()


class StoreDashboardProvider:
    """Adapter for a store that exposes explicit dashboard projection methods."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def summary(self) -> Mapping[str, Any]:
        source = dict(self._store.summary())
        source.setdefault("verified_exact_count", source.get("verified_artifact_count", 0))
        source.setdefault("manifest_ready_count", source.get("enriched_manifest_count", 0))
        source.setdefault("integrity_warning_count", source.get("attention_count", 0))
        source.setdefault(
            "disk_used_bytes",
            int(source.get("source_bytes") or 0) + int(source.get("archive_bytes") or 0),
        )
        source.setdefault("disk_free_bytes", source.get("free_bytes"))
        source.setdefault("reconciled_at", source.get("measured_at"))
        return source

    def repositories(self) -> Sequence[Mapping[str, Any]]:
        return [self._repository_summary(item) for item in self._store.repositories()]

    def repository(self, repository_id: str) -> Mapping[str, Any] | None:
        try:
            repository = dict(self._store.repository(repository_id))
        except Exception as exc:
            if getattr(exc, "code", None) == "not_found":
                return None
            raise
        preferred = repository.get("preferred_artifact") or {}
        repository.setdefault("name", repository.get("display_name"))
        repository.setdefault("status", self._health_status(repository))
        repository.setdefault("preferred_source_path", preferred.get("source_path"))
        repository.setdefault("selected_ref", preferred.get("ref"))
        repository.setdefault("commit_sha", preferred.get("actual_commit"))
        repository.setdefault("resolution_provenance", preferred.get("resolution_kind"))
        repository.setdefault("verification_state", preferred.get("verification_state"))
        repository["package_bindings"] = [
            self._package_binding(item)
            for item in repository.get("package_bindings", repository.get("packages", []))
        ]
        repository["local_sources"] = [
            dict(item) for item in repository.get("local_sources", [])
        ]
        repository.setdefault("has_local_source", bool(repository["local_sources"]))
        return repository

    def tags(self, repository_id: str) -> Sequence[Mapping[str, Any] | str] | None:
        try:
            return self._store.tags(repository_id)
        except Exception as exc:
            if getattr(exc, "code", None) == "not_found":
                return None
            raise

    def operations(self) -> Sequence[Mapping[str, Any]]:
        return self._store.operations()

    def events(self, after_sequence: int) -> Sequence[Mapping[str, Any]]:
        projected = []
        for event in self._store.events(after_sequence):
            item = dict(event)
            current = item.get("progress_current")
            total = item.get("progress_total")
            if current is not None and total:
                item["progress"] = round(100 * int(current) / int(total))
            projected.append(item)
        return projected

    @property
    def reconcile_cached_metrics(self) -> Callable[[], Any]:
        """Expose the store's optional expensive-metric refresh capability."""

        callback = getattr(self._store, "reconcile_cached_metrics", None)
        if not callable(callback):
            raise AttributeError("The catalog store does not expose cached-metric reconciliation.")
        return callback

    @classmethod
    def _repository_summary(cls, source: Mapping[str, Any]) -> Mapping[str, Any]:
        repository = dict(source)
        repository.setdefault("name", repository.get("display_name"))
        repository.setdefault("status", cls._health_status(repository))
        repository.setdefault("resolution_provenance", repository.get("preferred_resolution_kind"))
        repository.setdefault("selected_ref", repository.get("preferred_ref"))
        repository.setdefault("commit_sha", repository.get("preferred_commit"))
        repository.setdefault(
            "verification_state", repository.get("preferred_verification_state")
        )
        repository.setdefault(
            "has_local_source", bool(repository.get("local_source_count"))
        )
        repository.setdefault("package_search_terms", [])
        return repository

    @staticmethod
    def _package_binding(source: Mapping[str, Any]) -> Mapping[str, Any]:
        binding = dict(source)
        binding.setdefault(
            "resolution_provenance", binding.get("resolution_kind", "unresolved")
        )
        return binding

    @staticmethod
    def _health_status(repository: Mapping[str, Any]) -> str:
        health = repository.get("health")
        severity = health.get("severity") if isinstance(health, Mapping) else health
        if severity in {"error", "warning", "info"}:
            return "warning" if severity in {"warning", "info"} else "error"
        resolution = repository.get("preferred_resolution_kind")
        verification = repository.get("preferred_verification_state")
        if resolution in {"exact_commit", "exact_tag"} and verification == "verified":
            return "verified"
        return "ready"


def load_default_provider(catalog_root: Path) -> DashboardProvider:
    """Create the standard store adapter without making HTTP depend on SQLite."""

    try:
        from _catalog_store import CatalogStore  # type: ignore[import-not-found]

        store = CatalogStore(catalog_root)
        store.initialize()
        store.recover_stale_operations()
        return StoreDashboardProvider(store)
    except Exception as exc:  # The dashboard remains inspectable during store recovery.
        return UnavailableDashboardProvider(f"Catalog data is unavailable: {exc}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact_string(value: str) -> str:
    return redact_text(value)


def _truncate_log_line(value: str) -> str:
    if len(value) > MAX_DASHBOARD_LOG_LINE_CHARACTERS:
        return f"{value[: MAX_DASHBOARD_LOG_LINE_CHARACTERS - 1]}…"
    return value


def _safe_log_line(value: str) -> str:
    """Redact credentials and render control characters inert on one physical line."""

    redacted = _redact_string(value)
    safe = LOG_CONTROL_CHARACTER_PATTERN.sub(
        lambda match: f"\\u{ord(match.group(0)):04x}", redacted
    )
    return _truncate_log_line(safe)


def sanitize_payload(value: Any, *, key: str | None = None) -> Any:
    """Convert projections to JSON-safe values while redacting obvious secrets."""

    if key and any(part in key.casefold() for part in SENSITIVE_KEY_PARTS):
        return "[redacted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_payload(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_payload(item) for item in value]
    return str(value)


@dataclass(frozen=True)
class DashboardProcess:
    """Verified dashboard process metadata stored under the global catalog root."""

    pid: int
    port: int
    instance_id: str
    started_at: str

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/"

    def to_dict(self, *, running: bool = True) -> dict[str, Any]:
        return {
            "running": running,
            "pid": self.pid,
            "host": HOST,
            "port": self.port,
            "url": self.url,
            "instance_id": self.instance_id,
            "started_at": self.started_at,
        }


def _state_dir(catalog_root: Path) -> Path:
    state_dir = catalog_root.resolve() / "state"
    if is_link_or_reparse(state_dir):
        raise DashboardError(
            f"Dashboard state directory must not be a link or reparse point: {state_dir}"
        )
    if state_dir.exists() and not state_dir.is_dir():
        raise DashboardError(f"Dashboard state path is not a directory: {state_dir}")
    return state_dir


def _metadata_path(catalog_root: Path) -> Path:
    return _state_dir(catalog_root) / "dashboard.json"


def _lock_path(catalog_root: Path) -> Path:
    return _state_dir(catalog_root) / "dashboard.lock"


def _log_path(catalog_root: Path) -> Path:
    return _state_dir(catalog_root) / "dashboard.log"


def _validate_regular_entry(path: Path, *, allow_missing: bool) -> os.stat_result | None:
    """Reject links and special files before any dashboard-owned file operation."""

    try:
        entry = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    if (
        stat.S_ISLNK(entry.st_mode)
        or is_windows_reparse_point(path)
        or not stat.S_ISREG(entry.st_mode)
    ):
        raise DashboardError(f"Dashboard lifecycle path is not a regular file: {path}")
    if entry.st_nlink != 1:
        raise DashboardError(f"Dashboard lifecycle file must have exactly one hard link: {path}")
    return entry


def _open_regular_fd(path: Path, flags: int, *, create: bool = False) -> int:
    """Open one regular file without following a pre-created symlink."""

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    non_blocking = getattr(os, "O_NONBLOCK", 0)
    binary = getattr(os, "O_BINARY", 0)
    for _attempt in range(3):
        existing = _validate_regular_entry(path, allow_missing=create)
        open_flags = flags | no_follow | non_blocking | binary
        if existing is None:
            open_flags |= os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, open_flags, 0o600)
        except FileExistsError:
            continue
        except FileNotFoundError:
            if create:
                continue
            raise
        except OSError as exc:
            raise DashboardError(f"Unable to open dashboard lifecycle file safely: {path}: {exc}") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise DashboardError(f"Dashboard lifecycle path is not a regular file: {path}")
            if opened.st_nlink != 1:
                raise DashboardError(
                    f"Dashboard lifecycle file must have exactly one hard link: {path}"
                )
            current = _validate_regular_entry(path, allow_missing=False)
            if current is None or not os.path.samestat(opened, current):
                raise DashboardError(f"Dashboard lifecycle path changed while opening: {path}")
            if existing is not None and not os.path.samestat(existing, opened):
                raise DashboardError(f"Dashboard lifecycle path changed while opening: {path}")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
    raise DashboardError(f"Dashboard lifecycle path could not be opened without a race: {path}")


def _read_regular_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    descriptor = _open_regular_fd(path, os.O_RDONLY)
    try:
        size = os.fstat(descriptor).st_size
        if size > maximum_bytes:
            raise DashboardError(
                f"Dashboard lifecycle file exceeds the {maximum_bytes}-byte read limit: {path}"
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise DashboardError(
                f"Dashboard lifecycle file exceeds the {maximum_bytes}-byte read limit: {path}"
            )
        return payload
    finally:
        os.close(descriptor)


def _read_dashboard_log_tail(catalog_root: Path) -> str:
    path = _log_path(catalog_root)
    try:
        descriptor = _open_regular_fd(path, os.O_RDONLY)
    except FileNotFoundError:
        return ""
    try:
        size = os.fstat(descriptor).st_size
        os.lseek(descriptor, max(0, size - MAX_DASHBOARD_LOG_READ_BYTES), os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = MAX_DASHBOARD_LOG_READ_BYTES
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        return payload.decode("utf-8", errors="replace")[-1200:].strip()
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _dashboard_log(catalog_root: Path):
    path = _log_path(catalog_root)
    descriptor = _open_regular_fd(path, os.O_WRONLY | os.O_APPEND, create=True)
    try:
        if os.fstat(descriptor).st_size > MAX_DASHBOARD_LOG_FILE_BYTES:
            os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "ab", buffering=0) as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _prepare_windows_lock_byte(handle: Any) -> None:
    """Initialize the byte-range lock target once without growing an append-mode file."""

    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)


@contextlib.contextmanager
def _lifecycle_lock(catalog_root: Path):
    state_dir = _state_dir(catalog_root)
    state_dir.mkdir(parents=True, exist_ok=True)
    descriptor = _open_regular_fd(_lock_path(catalog_root), os.O_RDWR, create=True)
    handle = os.fdopen(descriptor, "a+b", buffering=0)
    try:
        if os.name == "nt":
            import msvcrt

            _prepare_windows_lock_byte(handle)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _read_process(catalog_root: Path) -> DashboardProcess | None:
    path = _metadata_path(catalog_root)
    try:
        raw = json.loads(
            _read_regular_bytes(path, maximum_bytes=MAX_DASHBOARD_METADATA_BYTES).decode("utf-8")
        )
        process = DashboardProcess(
            pid=int(raw["pid"]),
            port=int(raw["port"]),
            instance_id=str(raw["instance_id"]),
            started_at=str(raw["started_at"]),
        )
        if process.pid <= 0 or not 1 <= process.port <= 65535 or not process.instance_id:
            raise ValueError("invalid dashboard metadata")
        return process
    except (
        FileNotFoundError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None


def _write_process(catalog_root: Path, process: DashboardProcess) -> None:
    path = _metadata_path(catalog_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    payload = (json.dumps(process.to_dict(), indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_DASHBOARD_METADATA_BYTES:
        raise DashboardError("Dashboard process metadata exceeded its size limit.")
    descriptor = _open_regular_fd(temporary, os.O_WRONLY, create=True)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_regular_entry(path, allow_missing=True)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _clear_process(catalog_root: Path) -> None:
    path = _metadata_path(catalog_root)
    _validate_regular_entry(path, allow_missing=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("linux"):
        try:
            process_state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
            if process_state == "Z":
                with contextlib.suppress(ChildProcessError, OSError):
                    os.waitpid(pid, os.WNOHANG)
                return False
        except (FileNotFoundError, IndexError, OSError):
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _health(process: DashboardProcess, *, timeout: float = 0.75) -> bool:
    if not _pid_exists(process.pid):
        return False
    request = Request(f"http://{HOST}:{process.port}/api/v1/health", method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(MAX_API_RESPONSE_BYTES))
        return (
            response.status == HTTPStatus.OK
            and payload.get("service") == SERVICE_NAME
            and payload.get("instance_id") == process.instance_id
        )
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False


def _available_port(requested_port: int | None) -> int:
    if requested_port is not None and not 1 <= requested_port <= 65535:
        raise DashboardError("Dashboard port must be between 1 and 65535.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, requested_port or 0))
        except OSError as exc:
            raise DashboardError(f"Dashboard port {requested_port} is unavailable: {exc}") from exc
        return int(probe.getsockname()[1])


def dashboard_status(catalog_root: Path) -> dict[str, Any]:
    """Return verified status and clear malformed or stale metadata."""

    root = catalog_root.expanduser().resolve()
    with _lifecycle_lock(root):
        process = _read_process(root)
        if process is not None and _health(process):
            return process.to_dict()
        _clear_process(root)
        return {"running": False, "host": HOST, "url": None}


def start_dashboard(
    catalog_root: Path,
    *,
    port: int | None = None,
    assets_dir: Path | None = None,
    timeout: float = DEFAULT_START_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Start or reuse the single verified dashboard for a global catalog."""

    root = catalog_root.expanduser().resolve()
    assets = (assets_dir or default_assets_dir()).resolve()
    _validate_assets(assets)
    with _lifecycle_lock(root):
        current = _read_process(root)
        if current is not None and _health(current):
            result = current.to_dict()
            result["reused"] = True
            return result
        _clear_process(root)

        selected_port = _available_port(port)
        process = DashboardProcess(
            pid=0,
            port=selected_port,
            instance_id=secrets.token_urlsafe(18),
            started_at=_utc_now(),
        )
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "serve",
            f"--catalog-root={root}",
            f"--assets-dir={assets}",
            f"--port={selected_port}",
            f"--instance-id={process.instance_id}",
        ]
        state_dir = _state_dir(root)
        state_dir.mkdir(parents=True, exist_ok=True)
        with _dashboard_log(root) as log_handle:
            popen_kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "cwd": str(root),
                "close_fds": True,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                )
            else:
                popen_kwargs["start_new_session"] = True
            child = subprocess.Popen(command, **popen_kwargs)
            _OWNED_PROCESSES[child.pid] = child

        process = DashboardProcess(
            pid=child.pid,
            port=process.port,
            instance_id=process.instance_id,
            started_at=process.started_at,
        )
        try:
            _write_process(root, process)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    break
                if _health(process, timeout=0.3):
                    result = process.to_dict()
                    result["reused"] = False
                    return result
                time.sleep(0.05)

            tail = ""
            with contextlib.suppress(OSError, DashboardError):
                tail = _read_dashboard_log_tail(root)
            detail = f" Last log output: {tail}" if tail else ""
            raise DashboardError(f"Dashboard failed to start on {HOST}:{selected_port}.{detail}")
        except BaseException:
            _cleanup_failed_start(root, process, child)
            raise


def _cleanup_failed_start(
    catalog_root: Path,
    process: DashboardProcess,
    child: subprocess.Popen[Any],
) -> None:
    """Leave no child or trusted metadata behind after any post-spawn failure."""

    with contextlib.suppress(Exception):
        _terminate_process(process, timeout=1.0)
    _OWNED_PROCESSES.pop(child.pid, None)
    if child.poll() is None:
        with contextlib.suppress(Exception):
            child.kill()
        with contextlib.suppress(Exception):
            child.wait(timeout=1.0)
    with contextlib.suppress(OSError, DashboardError):
        _clear_process(catalog_root)


def stop_dashboard(
    catalog_root: Path, *, timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Stop only the dashboard instance whose identity is verified by health."""

    root = catalog_root.expanduser().resolve()
    with _lifecycle_lock(root):
        process = _read_process(root)
        if process is None or not _health(process):
            _clear_process(root)
            return {"running": False, "stopped": False, "host": HOST, "url": None}
        _terminate_process(process, timeout=timeout)
        if _pid_exists(process.pid):
            raise DashboardError(f"Dashboard process {process.pid} did not stop.")
        _clear_process(root)
        result = process.to_dict(running=False)
        result["stopped"] = True
        return result


def _terminate_process(process: DashboardProcess, *, timeout: float) -> None:
    owned = _OWNED_PROCESSES.pop(process.pid, None)
    if owned is not None:
        if owned.poll() is None:
            owned.terminate()
            try:
                owned.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                owned.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    owned.wait(timeout=1.0)
        return
    if not _pid_exists(process.pid):
        return
    try:
        os.kill(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(process.pid):
            return
        time.sleep(0.05)
    if hasattr(signal, "SIGKILL"):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(process.pid, signal.SIGKILL)
        kill_deadline = time.monotonic() + 1.0
        while time.monotonic() < kill_deadline and _pid_exists(process.pid):
            time.sleep(0.05)


def default_assets_dir() -> Path:
    """Return the packaged dashboard asset directory."""

    return Path(__file__).resolve().parent.parent / "assets" / "dashboard"


def _validate_assets(assets_dir: Path) -> None:
    for name in ("index.html", "app.css", "app.js"):
        candidate = assets_dir / name
        if not candidate.is_file() or is_link_or_reparse(candidate):
            raise DashboardError(f"Dashboard asset is missing or unsafe: {candidate}")


def _safe_repository_id(raw_segment: str) -> str | None:
    try:
        decoded = unquote(raw_segment, errors="strict")
    except UnicodeError:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", decoded):
        return None
    return decoded


def _json_bytes(payload: Any) -> bytes:
    encoded = json.dumps(
        sanitize_payload(payload), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_API_RESPONSE_BYTES:
        raise DashboardError("Dashboard response exceeded the safety limit.")
    return encoded


def make_handler(
    provider: DashboardProvider, assets_dir: Path, instance_id: str
) -> type[BaseHTTPRequestHandler]:
    """Build an isolated request handler bound to a read-only provider."""

    _validate_assets(assets_dir)
    assets = {
        "/": ((assets_dir / "index.html").read_bytes(), "text/html; charset=utf-8"),
        "/assets/app.css": ((assets_dir / "app.css").read_bytes(), "text/css; charset=utf-8"),
        "/assets/app.js": ((assets_dir / "app.js").read_bytes(), "text/javascript; charset=utf-8"),
    }

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        server_version = "InspectDependencySourceDashboard/1"
        sys_version = ""

        def __getattr__(self, name: str) -> Any:
            if name.startswith("do_"):
                return self._method_not_allowed
            raise AttributeError(name)

        def log_message(self, format_string: str, *args: Any) -> None:
            del format_string, args
            # This localhost dashboard has no access-log retention requirement.
            # Suppression prevents sensitive query strings and request spam from
            # turning the lifecycle diagnostic log into a disclosure or disk-fill vector.

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch(send_body=True)

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch(send_body=False)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._method_not_allowed()

        def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._method_not_allowed()

        def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._method_not_allowed()

        def do_TRACE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._method_not_allowed()

        def do_CONNECT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._method_not_allowed()

        def _dispatch(self, *, send_body: bool) -> None:
            if not self._validate_request_authority(send_body=send_body):
                return
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
                self._bad_request(
                    "invalid_request_target",
                    "Only origin-form request targets are accepted.",
                    send_body,
                )
                return
            if parsed.path in assets and not parsed.query:
                body, content_type = assets[parsed.path]
                self._send_bytes(
                    HTTPStatus.OK,
                    body,
                    content_type,
                    send_body=send_body,
                    cache_control="no-cache",
                )
                return
            if parsed.path.startswith("/api/v1/"):
                self._dispatch_api(parsed.path, parsed.query, send_body=send_body)
                return
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "not_found", "message": "Resource not found."}},
                send_body=send_body,
            )

        def _dispatch_api(self, path: str, query: str, *, send_body: bool) -> None:
            try:
                if path == "/api/v1/health" and not query:
                    reconciler = getattr(self.server, "reconciler", None)
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "service": SERVICE_NAME,
                            "api_version": API_VERSION,
                            "instance_id": instance_id,
                            "timestamp": _utc_now(),
                            "reconciliation": (
                                reconciler.status() if reconciler is not None else {"enabled": False}
                            ),
                        },
                        send_body=send_body,
                    )
                    return
                if path == "/api/v1/summary" and not query:
                    self._send_json(
                        HTTPStatus.OK,
                        {"summary": provider.summary()},
                        send_body=send_body,
                    )
                    return
                if path == "/api/v1/repositories" and not query:
                    repositories = list(provider.repositories())
                    self._send_json(
                        HTTPStatus.OK,
                        {"repositories": repositories, "count": len(repositories)},
                        send_body=send_body,
                    )
                    return
                if path == "/api/v1/operations" and not query:
                    operations = list(provider.operations())
                    self._send_json(
                        HTTPStatus.OK,
                        {"operations": operations, "count": len(operations)},
                        send_body=send_body,
                    )
                    return
                if path == "/api/v1/events":
                    after_sequence = _parse_event_cursor(query)
                    if after_sequence is None:
                        self._bad_request("invalid_cursor", "after_sequence must be a non-negative integer.", send_body)
                        return
                    events = list(provider.events(after_sequence))
                    sequences = [
                        int(item.get("sequence", 0))
                        for item in events
                        if isinstance(item, Mapping)
                        and isinstance(item.get("sequence", 0), int)
                    ]
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "events": events,
                            "count": len(events),
                            "last_sequence": max([after_sequence, *sequences]),
                        },
                        send_body=send_body,
                    )
                    return

                repository_route = re.fullmatch(
                    r"/api/v1/repositories/([^/]+)(/tags)?", path
                )
                if repository_route and not query:
                    repository_id = _safe_repository_id(repository_route.group(1))
                    if repository_id is None:
                        self._bad_request("invalid_repository_id", "Repository id is invalid.", send_body)
                        return
                    if repository_route.group(2):
                        tags = provider.tags(repository_id)
                        if tags is None:
                            self._not_found("repository_not_found", "Repository not found.", send_body)
                            return
                        self._send_json(
                            HTTPStatus.OK,
                            {"repository_id": repository_id, "tags": list(tags)},
                            send_body=send_body,
                        )
                        return
                    repository = provider.repository(repository_id)
                    if repository is None:
                        self._not_found("repository_not_found", "Repository not found.", send_body)
                        return
                    self._send_json(
                        HTTPStatus.OK,
                        {"repository": repository},
                        send_body=send_body,
                    )
                    return

                self._not_found("not_found", "Resource not found.", send_body)
            except Exception as exc:
                # The HTTP boundary must always return a redacted envelope; otherwise
                # socketserver can print provider exception details to local stderr.
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "error": {
                            "code": "dashboard_provider_error",
                            "message": _redact_string(str(exc)),
                        }
                    },
                    send_body=send_body,
                )

        def _method_not_allowed(self) -> None:
            if not self._validate_request_authority(send_body=True):
                return
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, HEAD")
            self._security_headers(cache_control="no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _validate_request_authority(self, *, send_body: bool) -> bool:
            """Reject DNS-rebinding and cross-origin requests before route handling."""

            port = int(self.server.server_address[1])
            expected_authority = f"{HOST}:{port}"
            host_headers = self.headers.get_all("Host", failobj=[])
            if len(host_headers) != 1 or host_headers[0] != expected_authority:
                self._send_json(
                    HTTPStatus.MISDIRECTED_REQUEST,
                    {
                        "error": {
                            "code": "untrusted_host",
                            "message": "Request Host does not match the bound dashboard origin.",
                        }
                    },
                    send_body=send_body,
                )
                return False

            origin_headers = self.headers.get_all("Origin", failobj=[])
            expected_origin = f"http://{expected_authority}"
            if len(origin_headers) > 1 or (
                origin_headers and origin_headers[0] != expected_origin
            ):
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {
                        "error": {
                            "code": "untrusted_origin",
                            "message": "Request Origin does not match the dashboard origin.",
                        }
                    },
                    send_body=send_body,
                )
                return False
            return True

        def _bad_request(self, code: str, message: str, send_body: bool) -> None:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"code": code, "message": message}},
                send_body=send_body,
            )

        def _not_found(self, code: str, message: str, send_body: bool) -> None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": code, "message": message}},
                send_body=send_body,
            )

        def _send_json(self, status: HTTPStatus, payload: Any, *, send_body: bool) -> None:
            self._send_bytes(
                status,
                _json_bytes(payload),
                "application/json; charset=utf-8",
                send_body=send_body,
                cache_control="no-store",
            )

        def _send_bytes(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
            *,
            send_body: bool,
            cache_control: str,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self._security_headers(cache_control=cache_control)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _security_headers(self, *, cache_control: str) -> None:
            self.send_header("Cache-Control", cache_control)
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)

    return DashboardRequestHandler


def _parse_event_cursor(query: str) -> int | None:
    if not query:
        return 0
    pairs = query.split("&")
    if len(pairs) != 1 or "=" not in pairs[0]:
        return None
    name, raw_value = pairs[0].split("=", 1)
    if name != "after_sequence" or not raw_value.isdigit():
        return None
    value = int(raw_value)
    return value if 0 <= value <= 9_223_372_036_854_775_807 else None


class DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded localhost server with prompt process shutdown behavior."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        reconciler: "DashboardReconciler",
    ) -> None:
        self.reconciler = reconciler
        super().__init__(server_address, request_handler)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.reconciler.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self.reconciler.stop()

    def server_close(self) -> None:
        self.reconciler.stop()
        super().server_close()


class DashboardReconciler:
    """Run optional expensive catalog reconciliation away from HTTP requests."""

    def __init__(
        self,
        provider: DashboardProvider,
        *,
        initial_delay: float = DEFAULT_RECONCILIATION_INITIAL_DELAY_SECONDS,
        interval: float = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
        stop_timeout: float = DEFAULT_RECONCILIATION_STOP_TIMEOUT_SECONDS,
    ) -> None:
        if initial_delay < 0:
            raise ValueError("Reconciliation initial delay must not be negative.")
        if interval <= 0:
            raise ValueError("Reconciliation interval must be greater than zero.")
        if stop_timeout < 0:
            raise ValueError("Reconciliation stop timeout must not be negative.")
        callback = getattr(provider, "reconcile_cached_metrics", None)
        self._callback: Callable[[], Any] | None = callback if callable(callback) else None
        self._initial_delay = initial_delay
        self._interval = interval
        self._stop_timeout = stop_timeout
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_deadline: float | None = None
        self._reconciling = False
        self._run_count = 0
        self._last_started_at: str | None = None
        self._last_completed_at: str | None = None
        self._last_error: str | None = None
        self._last_logged_error: str | None = None
        self._diagnostic_log_count = 0

    @property
    def enabled(self) -> bool:
        return self._callback is not None

    def start(self) -> None:
        """Start one daemon worker when the provider supports reconciliation."""

        if self._callback is None:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._stop_deadline = None
            self._thread = threading.Thread(
                target=self._run,
                name="catalog-dashboard-reconciliation",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Request shutdown and wait only a bounded time for an active refresh."""

        self._stop_event.set()
        with self._lock:
            thread = self._thread
            if self._stop_deadline is None:
                self._stop_deadline = time.monotonic() + self._stop_timeout
            remaining = max(0.0, self._stop_deadline - time.monotonic())
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=remaining)
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None

    def status(self) -> dict[str, Any]:
        """Return non-sensitive worker health for the dashboard health endpoint."""

        with self._lock:
            thread_alive = bool(self._thread and self._thread.is_alive())
            return {
                "enabled": self.enabled,
                "worker_alive": thread_alive,
                "reconciling": self._reconciling,
                "run_count": self._run_count,
                "last_started_at": self._last_started_at,
                "last_completed_at": self._last_completed_at,
                "last_error": self._last_error,
                "interval_seconds": self._interval if self.enabled else None,
            }

    def _run(self) -> None:
        if self._stop_event.wait(self._initial_delay):
            return
        while not self._stop_event.is_set():
            self._reconcile_once()
            if self._stop_event.wait(self._interval):
                return

    def _reconcile_once(self) -> None:
        callback = self._callback
        if callback is None:
            return
        with self._lock:
            self._reconciling = True
            self._last_started_at = _utc_now()
            self._last_error = None
        try:
            callback()
        except Exception as exc:  # One failed refresh must not stop future reconciliation.
            safe_error = _safe_log_line(str(exc))
            with self._lock:
                self._last_error = safe_error
                should_log = (
                    self._diagnostic_log_count < MAX_RECONCILIATION_DIAGNOSTIC_LINES
                    and safe_error != self._last_logged_error
                )
                if should_log:
                    self._last_logged_error = safe_error
                    self._diagnostic_log_count += 1
            if should_log:
                message = _truncate_log_line(
                    f"Dashboard reconciliation failed: {safe_error}"
                )
                sys.stderr.write(f"{message}\n")
        finally:
            with self._lock:
                self._reconciling = False
                self._run_count += 1
                self._last_completed_at = _utc_now()


def create_http_server(
    provider: DashboardProvider,
    *,
    port: int = 0,
    assets_dir: Path | None = None,
    instance_id: str | None = None,
    reconciliation_initial_delay: float = DEFAULT_RECONCILIATION_INITIAL_DELAY_SECONDS,
    reconciliation_interval: float = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    reconciliation_stop_timeout: float = DEFAULT_RECONCILIATION_STOP_TIMEOUT_SECONDS,
) -> DashboardHTTPServer:
    """Create, but do not start, a localhost-only dashboard server."""

    assets = (assets_dir or default_assets_dir()).resolve()
    handler = make_handler(provider, assets, instance_id or secrets.token_urlsafe(18))
    reconciler = DashboardReconciler(
        provider,
        initial_delay=reconciliation_initial_delay,
        interval=reconciliation_interval,
        stop_timeout=reconciliation_stop_timeout,
    )
    return DashboardHTTPServer((HOST, port), handler, reconciler)


@contextlib.contextmanager
def running_server(
    provider: DashboardProvider,
    *,
    assets_dir: Path | None = None,
    instance_id: str | None = None,
    reconciliation_initial_delay: float = DEFAULT_RECONCILIATION_INITIAL_DELAY_SECONDS,
    reconciliation_interval: float = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    reconciliation_stop_timeout: float = DEFAULT_RECONCILIATION_STOP_TIMEOUT_SECONDS,
):
    """Run an injected provider in a background thread for tests and tooling."""

    server = create_http_server(
        provider,
        port=0,
        assets_dir=assets_dir,
        instance_id=instance_id,
        reconciliation_initial_delay=reconciliation_initial_delay,
        reconciliation_interval=reconciliation_interval,
        reconciliation_stop_timeout=reconciliation_stop_timeout,
    )
    thread = threading.Thread(target=server.serve_forever, name="catalog-dashboard", daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def serve(
    catalog_root: Path,
    *,
    port: int,
    assets_dir: Path,
    instance_id: str,
) -> None:
    """Serve a catalog until the process receives a termination signal."""

    provider = load_default_provider(catalog_root)
    server = create_http_server(
        provider, port=port, assets_dir=assets_dir, instance_id=instance_id
    )
    shutdown_started = threading.Event()

    def request_shutdown(signum: int, frame: Any) -> None:
        del signum, frame
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Inspect Dependency Source dashboard.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve", help=argparse.SUPPRESS)
    serve_parser.add_argument("--catalog-root", type=Path, required=True)
    serve_parser.add_argument("--assets-dir", type=Path, required=True)
    serve_parser.add_argument("--port", type=int, required=True)
    serve_parser.add_argument("--instance-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        serve(
            args.catalog_root.expanduser().resolve(),
            port=args.port,
            assets_dir=args.assets_dir.expanduser().resolve(),
            instance_id=args.instance_id,
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
