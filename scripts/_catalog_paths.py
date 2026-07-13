"""User-level paths, safe managed paths, redaction, and file locking."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import time
import uuid
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit

from _catalog_models import CatalogError, ValidationError

if os.name == "nt":
    import msvcrt
else:
    import fcntl


APP_SLUG = "inspect-dependency-source"
APP_TITLE = "Inspect Dependency Source"
HOME_ENV = "INSPECT_DEPENDENCY_SOURCE_HOME"
LOCK_TIMEOUT_SECONDS = 30 * 60
STALE_STAGE_SECONDS = 24 * 60 * 60
MANAGED_ROOT_ENTRIES = frozenset({"state", "repos", "locks", "staging", "dashboard"})


def is_windows_reparse_point(path: Path) -> bool:
    """Return whether an existing path is a Windows reparse point, including junctions."""
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except (FileNotFoundError, OSError):
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and attributes & flag)


def is_link_or_reparse(path: Path) -> bool:
    """Detect symbolic links and Windows reparse points without following them."""
    try:
        status = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(flag and attributes & flag)


def _expanded_absolute(path: str | Path) -> Path:
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        raise ValidationError(f"Expected an absolute path: {path}")
    if is_link_or_reparse(expanded):
        raise ValidationError(f"Catalog root cannot be a link or reparse point: {expanded}")
    resolved = expanded.resolve(strict=False)
    _reject_volume_root(resolved)
    return resolved


def _reject_volume_root(path: Path) -> None:
    if path == Path(path.anchor):
        raise ValidationError(f"Catalog root must not be a filesystem volume root: {path}")


def _validate_dedicated_catalog_root(path: Path) -> None:
    _reject_volume_root(path)
    if is_link_or_reparse(path):
        raise ValidationError(f"Catalog root cannot be a link or reparse point: {path}")
    if path.exists() and not path.is_dir():
        raise ValidationError(f"Catalog root must be a directory: {path}")
    if path.is_dir():
        unexpected = sorted(entry.name for entry in path.iterdir() if entry.name not in MANAGED_ROOT_ENTRIES)
        if unexpected:
            preview = ", ".join(unexpected[:5])
            raise ValidationError(
                f"Catalog root must be a dedicated directory; unexpected entries: {preview}"
            )


def platform_config_path(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    home: Path | None = None,
) -> Path:
    """Return the stable bootstrap config path for one OS user."""
    env = environ if environ is not None else os.environ
    os_name = system or platform.system()
    user_home = home or Path.home()
    if os_name == "Windows":
        base = Path(env.get("APPDATA") or env.get("LOCALAPPDATA") or user_home / "AppData" / "Roaming")
        return base / APP_TITLE / "config.json"
    if os_name == "Darwin":
        return user_home / "Library" / "Application Support" / APP_TITLE / "config.json"
    base = Path(env.get("XDG_CONFIG_HOME") or user_home / ".config")
    return base / APP_SLUG / "config.json"


def platform_data_root(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    home: Path | None = None,
) -> Path:
    """Return the platform-standard default data root for one OS user."""
    env = environ if environ is not None else os.environ
    os_name = system or platform.system()
    user_home = home or Path.home()
    if os_name == "Windows":
        base = Path(env.get("LOCALAPPDATA") or user_home / "AppData" / "Local")
        return (base / APP_TITLE).resolve(strict=False)
    if os_name == "Darwin":
        return (user_home / "Library" / "Application Support" / APP_TITLE).resolve(strict=False)
    base = Path(env.get("XDG_DATA_HOME") or user_home / ".local" / "share")
    return (base / APP_SLUG).resolve(strict=False)


def load_user_config(config_path: Path | None = None) -> dict:
    """Read the user bootstrap config and reject malformed content."""
    path = config_path or platform_config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"User config is invalid: {path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"User config must contain a JSON object: {path}")
    root = payload.get("root_dir")
    if root is not None and (not isinstance(root, str) or not Path(root).expanduser().is_absolute()):
        raise ValidationError(f"User config root_dir must be an absolute path: {path}")
    return payload


def resolve_catalog_root(
    cli_root: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    config_path: Path | None = None,
    system: str | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve catalog root with CLI, environment, config, then platform precedence."""
    env = environ if environ is not None else os.environ
    if cli_root:
        return _expanded_absolute(cli_root)
    if env.get(HOME_ENV):
        return _expanded_absolute(env[HOME_ENV])
    config = load_user_config(config_path or platform_config_path(environ=env, system=system, home=home))
    if config.get("root_dir"):
        return _expanded_absolute(config["root_dir"])
    return platform_data_root(environ=env, system=system, home=home)


def save_catalog_root(root: str | Path, config_path: Path | None = None) -> Path:
    """Persist a new active catalog root without moving existing data."""
    resolved = _expanded_absolute(root)
    _validate_dedicated_catalog_root(resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    path = config_path or platform_config_path()
    atomic_write_json(path, {"version": 1, "root_dir": str(resolved)})
    return resolved


def ensure_catalog_layout(root: Path) -> None:
    """Create the global catalog's managed directory layout."""
    resolved = root.resolve(strict=False)
    _validate_dedicated_catalog_root(resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    if is_link_or_reparse(resolved):
        raise ValidationError(f"Catalog root cannot be a link or reparse point: {resolved}")
    if os.name != "nt":
        resolved.chmod(0o700)
    for child in ("state", "repos", "locks", "staging", "dashboard"):
        path = resolved / child
        if is_link_or_reparse(path):
            raise ValidationError(
                f"Managed catalog directory cannot be a link or reparse point: {path}"
            )
        path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)


def ensure_within(
    root: Path,
    candidate: Path,
    *,
    allow_root: bool = False,
) -> Path:
    """Validate lexical containment and reject symlinked managed ancestors."""
    lexical_root = Path(os.path.abspath(root))
    lexical_candidate = Path(os.path.abspath(candidate))
    if is_link_or_reparse(lexical_root):
        raise ValidationError(f"Managed root cannot be a link or reparse point: {lexical_root}")
    try:
        relative = lexical_candidate.relative_to(lexical_root)
    except ValueError as exc:
        raise ValidationError(f"Managed path escapes catalog root: {candidate}") from exc
    if lexical_candidate == lexical_root:
        if allow_root:
            return lexical_candidate
        raise ValidationError(f"Refusing to operate on the managed root itself: {lexical_root}")
    current = lexical_root
    for index, part in enumerate(relative.parts):
        current = current / part
        if is_link_or_reparse(current):
            raise ValidationError(f"Managed path contains a link or reparse point: {current}")
    resolved_root = lexical_root.resolve(strict=False)
    resolved_candidate = lexical_candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValidationError(f"Managed path escapes catalog root: {candidate}") from exc
    return lexical_candidate


def guarded_remove(root: Path, candidate: Path) -> None:
    """Delete only a derived managed child after containment and symlink checks."""
    managed = ensure_within(root, candidate)
    if managed.is_file():
        managed.unlink(missing_ok=True)
    elif managed.is_dir():
        shutil.rmtree(managed)


def atomic_write_json(path: Path, payload: dict) -> None:
    """Replace one JSON file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def normalize_text(value: str | None) -> str:
    """Normalize user-facing identity text for matching."""
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join("".join(character if character.isalnum() else " " for character in normalized).split())


def slug(value: str, *, limit: int = 48) -> str:
    """Create a readable, non-special path segment."""
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:limit].rstrip("-")
    return result or "source"


def stable_id(prefix: str, value: str) -> str:
    """Create a readable collision-safe opaque identifier."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}--{slug(value)}--{digest}"


def artifact_id(repository_id: str, kind: str, ref: str) -> str:
    """Create a globally unique artifact ID from repository, kind, and exact ref."""
    return stable_id("artifact", f"{repository_id}\0{kind}\0{ref}")


def sanitize_remote(remote: str | None) -> str | None:
    """Remove userinfo, query strings, and fragments from a persisted remote."""
    if not remote:
        return None
    value = remote.strip()
    if not value:
        return None
    if "://" in value:
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname or ""
            port = parsed.port
        except ValueError as exc:
            raise ValidationError(f"Remote URL is malformed: {exc}") from exc
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        if port:
            hostname = f"{hostname}:{port}"
        if parsed.scheme.casefold() in {"ssh", "git+ssh"} and parsed.username and not parsed.password:
            hostname = f"{parsed.username}@{hostname}"
        clean = SplitResult(parsed.scheme, hostname, parsed.path, "", "")
        return urlunsplit(clean)
    scp_value = re.split(r"[?#]", value, maxsplit=1)[0]
    scp = re.fullmatch(r"(?:(?P<user>[^@\s]+)@)?(?P<host>[^:\s]+):(?P<path>.+)", scp_value)
    if scp:
        user = f"{scp.group('user')}@" if scp.group("user") else ""
        return f"{user}{scp.group('host')}:{scp.group('path')}"
    return value


def redact_text(value: str) -> str:
    """Remove common URL credentials and known token values from diagnostic text."""
    redacted = re.sub(r"([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1", value, flags=re.IGNORECASE)
    redacted = re.sub(
        r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 [REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(\b(?:access[_-]?token|token|password|passwd|api[_-]?key|secret)\b\s*[:=]\s*)[^\s&;,]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "[REDACTED]", redacted)
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name)
        if token:
            redacted = redacted.replace(token, "[REDACTED]")
    return redacted


def directory_size(path: Path) -> tuple[int, int]:
    """Return total regular-file bytes and file count without following symlinks."""
    total = 0
    count = 0
    if not path.exists():
        return total, count
    for root, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [
            name for name in dirnames if not is_link_or_reparse(Path(root) / name)
        ]
        for filename in filenames:
            candidate = Path(root) / filename
            if is_link_or_reparse(candidate):
                continue
            try:
                total += candidate.stat().st_size
                count += 1
            except OSError:
                continue
    return total, count


def cleanup_stale_staging(
    root: Path,
    *,
    max_age_seconds: float = STALE_STAGE_SECONDS,
    now: float | None = None,
) -> dict[str, int]:
    """Remove old abandoned stages only while holding their artifact lock.

    Acquisition creates its persistent lock file before creating a stage. That lets
    the janitor recover the exact per-artifact lock from the collision-safe stage
    name without consulting mutable database state. Unknown entries are preserved.
    """
    if max_age_seconds < 0:
        raise ValidationError("Stale staging age must not be negative.")
    staging_root = ensure_within(root, root / "staging")
    locks_root = ensure_within(root, root / "locks")
    current_time = time.time() if now is None else now
    removed = 0
    busy = 0
    skipped = 0
    if not staging_root.is_dir():
        return {"removed": removed, "busy": busy, "skipped": skipped}
    for stage in staging_root.iterdir():
        try:
            status = stage.stat(follow_symlinks=False)
        except OSError:
            skipped += 1
            continue
        if (
            is_link_or_reparse(stage)
            or not stat.S_ISDIR(status.st_mode)
            or current_time - status.st_mtime < max_age_seconds
        ):
            skipped += 1
            continue
        identity, separator, nonce = stage.name.rpartition("-")
        if (
            not separator
            or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or not identity.startswith("artifact--")
        ):
            skipped += 1
            continue
        lock_candidates = list(locks_root.glob(f"*--{identity}.lock"))
        if len(lock_candidates) != 1:
            skipped += 1
            continue
        lock = ArtifactLock(lock_candidates[0], timeout=0.05)
        try:
            lock.acquire()
        except CatalogError:
            busy += 1
            continue
        try:
            try:
                current_status = stage.stat(follow_symlinks=False)
            except OSError:
                continue
            if (
                not is_link_or_reparse(stage)
                and stat.S_ISDIR(current_status.st_mode)
                and current_time - current_status.st_mtime >= max_age_seconds
            ):
                guarded_remove(staging_root, stage)
                removed += 1
        finally:
            lock.release()
    return {"removed": removed, "busy": busy, "skipped": skipped}


class ArtifactLock:
    """Cross-platform advisory lock scoped to one repository artifact."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self.path = path
        self.timeout = timeout
        self._handle = None
        self._locked = False

    def acquire(self, on_wait=None, on_acquired=None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if is_link_or_reparse(self.path):
            raise ValidationError(f"Lock file cannot be a link or reparse point: {self.path}")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NOINHERIT"):
            flags |= os.O_NOINHERIT
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise ValidationError(f"Unable to open lock file safely: {self.path} ({exc})") from exc
        file_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_status.st_mode)
            or file_status.st_nlink != 1
            or is_link_or_reparse(self.path)
        ):
            os.close(descriptor)
            raise ValidationError(f"Lock file must be a single regular file: {self.path}")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        self._handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            if os.name == "nt" and self.path.stat().st_size == 0:
                self._handle.write(" ")
                self._handle.flush()
            started = time.monotonic()
            waiting_reported = False
            while not self._try_lock():
                if time.monotonic() - started >= self.timeout:
                    raise CatalogError(f"Timed out waiting for artifact lock: {self.path.name}")
                if not waiting_reported and on_wait:
                    on_wait()
                    waiting_reported = True
                time.sleep(0.02)
            self._locked = True
            if waiting_reported and on_acquired:
                on_acquired()
            self._handle.seek(0)
            self._handle.truncate()
            json.dump({"pid": os.getpid(), "acquired_at": int(time.time())}, self._handle)
            self._handle.flush()
        except BaseException:
            self.release()
            raise

    def _try_lock(self) -> bool:
        assert self._handle is not None
        if os.name == "nt":
            self._handle.seek(0)
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if self._locked:
                if os.name == "nt":
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self._locked = False

    def __enter__(self) -> "ArtifactLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


@contextmanager
def artifact_lock(
    path: Path,
    *,
    timeout: float = LOCK_TIMEOUT_SECONDS,
    on_wait=None,
    on_acquired=None,
) -> Iterator[None]:
    """Acquire and release an artifact lock around one operation."""
    lock = ArtifactLock(path, timeout=timeout)
    lock.acquire(on_wait=on_wait, on_acquired=on_acquired)
    try:
        yield
    finally:
        lock.release()
