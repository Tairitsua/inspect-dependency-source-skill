"""Transactional SQLite persistence for the global source catalog."""

from __future__ import annotations

import difflib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from _catalog_models import (
    APP_VERSION,
    SCHEMA_VERSION,
    AmbiguousError,
    CatalogError,
    NotFoundError,
    OperationStatus,
    ValidationError,
)
from _catalog_paths import (
    ArtifactLock,
    directory_size,
    ensure_catalog_layout,
    ensure_within,
    is_link_or_reparse,
    normalize_text,
    redact_text,
    sanitize_remote,
)


def utc_now() -> str:
    """Return a stable second-precision UTC timestamp."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_utc(value: str | None) -> datetime | None:
    """Parse one persisted UTC timestamp, treating malformed metadata as stale."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class CatalogStore:
    """Own the versioned catalog database and read-only dashboard projections."""

    def __init__(self, root: str | Path) -> None:
        lexical_root = Path(root).expanduser()
        if is_link_or_reparse(lexical_root):
            raise ValidationError(
                f"Catalog root cannot be a link or reparse point: {lexical_root}"
            )
        self.root = lexical_root.resolve(strict=False)
        self.database_path = self.root / "state" / "catalog.sqlite3"

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open one short SQLite transaction with consistent safety pragmas."""
        ensure_catalog_layout(self.root)
        self._validate_database_path()
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            self._validate_database_path()
            if os.name != "nt":
                self.database_path.chmod(0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the clean, intentionally non-legacy schema."""
        ensure_catalog_layout(self.root)
        if self._schema_is_ready():
            return
        bootstrap_lock = ArtifactLock(self.root / "state" / "catalog.bootstrap.lock", timeout=60)
        with bootstrap_lock:
            if self._schema_is_ready():
                return
            connection = sqlite3.connect(self.database_path, timeout=30)
            try:
                self._validate_database_path()
                if os.name != "nt":
                    self.database_path.chmod(0o600)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 30000")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS repositories (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL CHECK(provider IN ('github','git','local')),
                    canonical_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL,
                    remote_url TEXT,
                    origin_kind TEXT NOT NULL CHECK(origin_kind IN ('github','git','local','hybrid')),
                    preferred_artifact_id TEXT,
                    tags_refreshed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_verified_at TEXT,
                    manifest_status TEXT NOT NULL DEFAULT 'missing'
                        CHECK(manifest_status IN ('missing','stub','enriched','invalid')),
                    manifest_summary TEXT,
                    manifest_error TEXT
                );

                CREATE TABLE IF NOT EXISTS aliases (
                    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    PRIMARY KEY(repository_id, normalized_alias)
                );

                CREATE TABLE IF NOT EXISTS local_sources (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    canonical_path TEXT NOT NULL UNIQUE,
                    detected_version TEXT,
                    branch TEXT,
                    commit_sha TEXT,
                    dirty INTEGER,
                    exists_state INTEGER NOT NULL,
                    markers_json TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('github_archive','git_clone','local')),
                    ref TEXT NOT NULL,
                    detected_version TEXT,
                    source_path TEXT NOT NULL,
                    archive_path TEXT,
                    status TEXT NOT NULL CHECK(status IN ('staging','ready','missing','invalid')),
                    resolution_kind TEXT NOT NULL CHECK(resolution_kind IN ('exact_commit','exact_tag','heuristic_tag','unresolved')),
                    expected_commit TEXT,
                    actual_commit TEXT,
                    verification_state TEXT NOT NULL CHECK(verification_state IN ('verified','unverified','failed')),
                    acquired_at TEXT NOT NULL,
                    verified_at TEXT,
                    last_accessed_at TEXT,
                    source_bytes INTEGER,
                    archive_bytes INTEGER,
                    file_count INTEGER,
                    source_digest TEXT,
                    archive_digest TEXT,
                    external INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(repository_id, kind, ref)
                );

                CREATE TABLE IF NOT EXISTS package_bindings (
                    id TEXT PRIMARY KEY,
                    ecosystem TEXT NOT NULL,
                    package_id TEXT NOT NULL COLLATE NOCASE,
                    version TEXT NOT NULL COLLATE NOCASE,
                    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                    artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
                    requested_ref TEXT,
                    resolved_ref TEXT,
                    resolution_kind TEXT NOT NULL CHECK(resolution_kind IN ('exact_commit','exact_tag','heuristic_tag','unresolved')),
                    expected_commit TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(ecosystem, package_id, version)
                );

                CREATE TABLE IF NOT EXISTS tags (
                    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    commit_sha TEXT,
                    refreshed_at TEXT NOT NULL,
                    PRIMARY KEY(repository_id, name)
                );

                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    command TEXT NOT NULL,
                    repository_id TEXT REFERENCES repositories(id) ON DELETE SET NULL,
                    artifact_id TEXT,
                    target TEXT,
                    status TEXT NOT NULL CHECK(status IN ('queued','waiting_lock','running','completed','failed','interrupted')),
                    phase TEXT NOT NULL,
                    progress_current INTEGER,
                    progress_total INTEGER,
                    progress_unit TEXT,
                    message TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    pid INTEGER NOT NULL,
                    owner_token TEXT,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS operation_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress_current INTEGER,
                    progress_total INTEGER,
                    progress_unit TEXT,
                    message TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_artifacts_repository ON artifacts(repository_id);
                CREATE INDEX IF NOT EXISTS ix_tags_repository ON tags(repository_id, name);
                CREATE INDEX IF NOT EXISTS ix_operations_updated ON operations(updated_at DESC);
                CREATE INDEX IF NOT EXISTS ix_events_operation ON operation_events(operation_id, sequence);
                COMMIT;
                """
                )
                existing = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if existing and int(existing["value"]) != SCHEMA_VERSION:
                    raise ValidationError(
                        f"Unsupported catalog schema {existing['value']}; expected {SCHEMA_VERSION}."
                    )
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key,value) VALUES('app_version',?)",
                    (APP_VERSION,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _schema_is_ready(self) -> bool:
        """Check the version marker without taking a write lock or changing journal mode."""
        self._validate_database_path()
        if not self.database_path.is_file():
            return False
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.database_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).casefold():
                return False
            raise
        finally:
            if connection is not None:
                connection.close()
        if row is None or str(journal_mode).casefold() != "wal":
            return False
        try:
            version = int(row["value"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("Catalog schema version marker is malformed.") from exc
        if version != SCHEMA_VERSION:
            raise ValidationError(
                f"Unsupported catalog schema {version}; expected {SCHEMA_VERSION}."
            )
        return True

    def _validate_database_path(self) -> None:
        """Reject unsafe SQLite database and journal/coordination sidecar paths."""
        candidates = [
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
            Path(f"{self.database_path}-journal"),
        ]
        for candidate in candidates:
            try:
                status = candidate.lstat()
            except FileNotFoundError:
                continue
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            attributes = getattr(status, "st_file_attributes", 0)
            if stat.S_ISLNK(status.st_mode) or bool(reparse_flag and attributes & reparse_flag):
                raise ValidationError(
                    f"SQLite catalog file cannot be a link or reparse point: {candidate}"
                )
            if candidate != self.database_path and status.st_nlink == 0:
                # WAL sidecars can be unlinked by a concurrent final connection
                # immediately after lstat. That unreachable inode is not a hard-link
                # escape; a newly reachable replacement is validated on the next pass.
                continue
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise ValidationError(
                    f"SQLite catalog file must be a single regular file: {candidate}"
                )

    def add_repository(
        self,
        *,
        repository_id: str,
        provider: str,
        canonical_name: str,
        display_name: str,
        remote_url: str | None,
        origin_kind: str,
        aliases: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Insert or promote one repository, preferring an existing remote identity."""
        now = utc_now()
        remote_url = sanitize_remote(remote_url)
        with self.connect(write=True) as connection:
            existing = None
            if remote_url:
                existing = connection.execute(
                    "SELECT * FROM repositories WHERE remote_url=? COLLATE BINARY",
                    (remote_url,),
                ).fetchone()
            if existing is None:
                existing = connection.execute(
                    "SELECT * FROM repositories WHERE canonical_name=? COLLATE NOCASE OR id=?",
                    (canonical_name, repository_id),
                ).fetchone()
            if existing:
                repository_id = existing["id"]
                next_origin = _merge_origin(existing["origin_kind"], origin_kind)
                next_provider = _merge_provider(existing["provider"], provider)
                connection.execute(
                    """
                    UPDATE repositories
                    SET provider=?, display_name=?, remote_url=COALESCE(?, remote_url),
                        origin_kind=?, updated_at=?
                    WHERE id=?
                    """,
                    (next_provider, display_name, remote_url, next_origin, now, repository_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO repositories(
                        id,provider,canonical_name,display_name,remote_url,origin_kind,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        repository_id,
                        provider,
                        canonical_name,
                        display_name,
                        remote_url,
                        origin_kind,
                        now,
                        now,
                    ),
                )
            for alias in aliases:
                normalized = normalize_text(alias)
                if normalized:
                    connection.execute(
                        "INSERT OR IGNORE INTO aliases(repository_id,alias,normalized_alias) VALUES(?,?,?)",
                        (repository_id, alias.strip(), normalized),
                    )
        return self.repository(repository_id)

    def repository_by_remote(self, remote_url: str) -> dict[str, Any] | None:
        """Resolve one repository by its already-sanitized remote URL."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM repositories WHERE remote_url=? COLLATE BINARY", (remote_url,)
            ).fetchone()
        return self.repository(row["id"]) if row else None

    def repository_by_canonical_name(self, canonical_name: str) -> dict[str, Any] | None:
        """Return one repository by its case-insensitive canonical identity."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM repositories WHERE canonical_name=? COLLATE NOCASE",
                (canonical_name,),
            ).fetchone()
        return self.repository(row["id"]) if row else None

    def local_source_by_path(self, canonical_path: str) -> dict[str, Any] | None:
        """Return a registered local source by its resolved absolute path."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM local_sources WHERE canonical_path=?", (canonical_path,)
            ).fetchone()
        return dict(row) if row else None

    def add_aliases(self, repository_id: str, aliases: Sequence[str]) -> None:
        """Add case-insensitively unique aliases."""
        with self.connect(write=True) as connection:
            for alias in aliases:
                normalized = normalize_text(alias)
                if normalized:
                    connection.execute(
                        "INSERT OR IGNORE INTO aliases(repository_id,alias,normalized_alias) VALUES(?,?,?)",
                        (repository_id, alias.strip(), normalized),
                    )

    def update_manifest_cache(
        self,
        repository_id: str,
        *,
        status: str,
        summary: str | None = None,
        error: str | None = None,
    ) -> None:
        """Persist the bounded manifest projection used by inventory polling."""
        with self.connect(write=True) as connection:
            connection.execute(
                """
                UPDATE repositories
                SET manifest_status=?,manifest_summary=?,manifest_error=?
                WHERE id=?
                """,
                (status, summary, redact_text(error) if error else None, repository_id),
            )

    def upsert_local_source_artifact(
        self,
        repository_id: str,
        source: dict[str, Any],
        artifact: dict[str, Any],
    ) -> None:
        """Atomically assign one local path and its current artifact to a repository."""
        now = utc_now()
        with self.connect(write=True) as connection:
            previous = connection.execute(
                "SELECT repository_id FROM local_sources WHERE canonical_path=?",
                (source["canonical_path"],),
            ).fetchone()
            previous_repository_id = previous["repository_id"] if previous else None
            stale_artifacts = connection.execute(
                """
                SELECT id,repository_id FROM artifacts
                WHERE kind='local' AND source_path=? AND repository_id<>?
                """,
                (source["canonical_path"], repository_id),
            ).fetchall()
            for stale in stale_artifacts:
                connection.execute(
                    """
                    UPDATE repositories SET preferred_artifact_id=NULL,updated_at=?
                    WHERE id=? AND preferred_artifact_id=?
                    """,
                    (now, stale["repository_id"], stale["id"]),
                )
                connection.execute("DELETE FROM artifacts WHERE id=?", (stale["id"],))
            connection.execute(
                """
                INSERT INTO local_sources(
                    id,repository_id,path,canonical_path,detected_version,branch,commit_sha,dirty,
                    exists_state,markers_json,added_at,verified_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(canonical_path) DO UPDATE SET
                    repository_id=excluded.repository_id,
                    path=excluded.path,
                    detected_version=excluded.detected_version,
                    branch=excluded.branch,
                    commit_sha=excluded.commit_sha,
                    dirty=excluded.dirty,
                    exists_state=excluded.exists_state,
                    markers_json=excluded.markers_json,
                    verified_at=excluded.verified_at,
                    generation=local_sources.generation+1
                """,
                (
                    source["id"],
                    repository_id,
                    source["path"],
                    source["canonical_path"],
                    source.get("detected_version"),
                    source.get("branch"),
                    source.get("commit_sha"),
                    None if source.get("dirty") is None else int(bool(source["dirty"])),
                    int(bool(source.get("exists", True))),
                    _json(source.get("markers", [])),
                    source.get("added_at", now),
                    now,
                ),
            )
            self._upsert_artifact_row(connection, repository_id, artifact)
            changed = connection.execute(
                """
                UPDATE repositories SET preferred_artifact_id=?,updated_at=?
                WHERE id=? AND EXISTS(
                    SELECT 1 FROM artifacts WHERE id=? AND repository_id=?
                )
                """,
                (artifact["id"], now, repository_id, artifact["id"], repository_id),
            ).rowcount
            if changed != 1:
                raise ValidationError("Local artifact could not be assigned to its repository.")
            connection.execute(
                "UPDATE repositories SET origin_kind=?,updated_at=?,last_verified_at=? WHERE id=?",
                (_origin_with_local_source(connection, repository_id), now, now, repository_id),
            )
            if previous_repository_id and previous_repository_id != repository_id:
                connection.execute(
                    "UPDATE repositories SET origin_kind=?,updated_at=? WHERE id=?",
                    (
                        _origin_with_local_source(connection, previous_repository_id),
                        now,
                        previous_repository_id,
                    ),
                )

    def upsert_artifact(self, repository_id: str, artifact: dict[str, Any], *, prefer: bool = True) -> None:
        """Persist one artifact and optionally make it the preferred source."""
        with self.connect(write=True) as connection:
            self._upsert_artifact_row(connection, repository_id, artifact)
            if prefer:
                changed = connection.execute(
                    """
                    UPDATE repositories SET preferred_artifact_id=?,updated_at=?
                    WHERE id=? AND EXISTS(
                        SELECT 1 FROM artifacts WHERE id=? AND repository_id=?
                    )
                    """,
                    (
                        artifact["id"],
                        utc_now(),
                        repository_id,
                        artifact["id"],
                        repository_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise ValidationError("Artifact could not be assigned to its repository.")

    @staticmethod
    def _upsert_artifact_row(
        connection: sqlite3.Connection,
        repository_id: str,
        artifact: dict[str, Any],
    ) -> None:
        existing = connection.execute(
            "SELECT repository_id FROM artifacts WHERE id=?", (artifact["id"],)
        ).fetchone()
        if existing and existing["repository_id"] != repository_id:
            raise ValidationError("Artifact identity is already owned by another repository.")
        connection.execute(
            """
            INSERT INTO artifacts(
                id,repository_id,kind,ref,detected_version,source_path,archive_path,
                status,resolution_kind,expected_commit,actual_commit,verification_state,acquired_at,
                verified_at,last_accessed_at,source_bytes,archive_bytes,file_count,source_digest,
                archive_digest,external
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                ref=excluded.ref,
                detected_version=excluded.detected_version,
                source_path=excluded.source_path,
                archive_path=excluded.archive_path,
                status=excluded.status,
                resolution_kind=excluded.resolution_kind,
                expected_commit=excluded.expected_commit,
                actual_commit=excluded.actual_commit,
                verification_state=excluded.verification_state,
                acquired_at=excluded.acquired_at,
                verified_at=excluded.verified_at,
                last_accessed_at=excluded.last_accessed_at,
                source_bytes=excluded.source_bytes,
                archive_bytes=excluded.archive_bytes,
                file_count=excluded.file_count,
                source_digest=excluded.source_digest,
                archive_digest=excluded.archive_digest,
                external=excluded.external,
                generation=artifacts.generation+1
            """,
            (
                artifact["id"],
                repository_id,
                artifact["kind"],
                artifact["ref"],
                artifact.get("detected_version"),
                artifact["source_path"],
                artifact.get("archive_path"),
                artifact["status"],
                artifact["resolution_kind"],
                artifact.get("expected_commit"),
                artifact.get("actual_commit"),
                artifact["verification_state"],
                artifact.get("acquired_at", utc_now()),
                artifact.get("verified_at"),
                artifact.get("last_accessed_at"),
                artifact.get("source_bytes"),
                artifact.get("archive_bytes"),
                artifact.get("file_count"),
                artifact.get("source_digest"),
                artifact.get("archive_digest"),
                int(bool(artifact.get("external"))),
            ),
        )

    def update_artifact_health(
        self,
        artifact_id: str,
        *,
        status: str,
        verification_state: str,
        source_bytes: int | None,
        archive_bytes: int | None,
        file_count: int | None,
        actual_commit: str | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """CAS-update artifact health without overwriting a concurrent replacement."""
        now = utc_now()
        with self.connect(write=True) as connection:
            changed = connection.execute(
                """
                UPDATE artifacts
                SET status=?,verification_state=?,source_bytes=?,archive_bytes=?,file_count=?,
                    actual_commit=?,verified_at=?
                WHERE id=? AND (? IS NULL OR generation=?)
                """,
                (
                    status,
                    verification_state,
                    source_bytes,
                    archive_bytes,
                    file_count,
                    actual_commit,
                    now,
                    artifact_id,
                    expected_generation,
                    expected_generation,
                ),
            ).rowcount
        return changed == 1

    def bind_package(self, binding: dict[str, Any]) -> None:
        """Bind an exact package version to its resolved source provenance."""
        binding_id = binding.get("id") or str(uuid.uuid4())
        with self.connect(write=True) as connection:
            artifact_identity = binding.get("artifact_id")
            owns_artifact = (
                connection.execute(
                    "SELECT 1 FROM artifacts WHERE id=? AND repository_id=?",
                    (artifact_identity, binding["repository_id"]),
                ).fetchone()
                if artifact_identity
                else None
            )
            if owns_artifact is None:
                raise ValidationError(
                    "Package bindings require an artifact owned by the same repository."
                )
            connection.execute(
                """
                INSERT INTO package_bindings(
                    id,ecosystem,package_id,version,repository_id,artifact_id,requested_ref,resolved_ref,
                    resolution_kind,expected_commit,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ecosystem,package_id,version) DO UPDATE SET
                    repository_id=excluded.repository_id,
                    artifact_id=excluded.artifact_id,
                    requested_ref=excluded.requested_ref,
                    resolved_ref=excluded.resolved_ref,
                    resolution_kind=excluded.resolution_kind,
                    expected_commit=excluded.expected_commit,
                    created_at=excluded.created_at
                """,
                (
                    binding_id,
                    binding["ecosystem"],
                    binding["package_id"],
                    binding["version"],
                    binding["repository_id"],
                    artifact_identity,
                    binding.get("requested_ref"),
                    binding.get("resolved_ref"),
                    binding["resolution_kind"],
                    binding.get("expected_commit"),
                    utc_now(),
                ),
            )

    def replace_tags(self, repository_id: str, tags: Sequence[dict[str, Any]]) -> None:
        """Replace a repository's cached tag snapshot atomically."""
        now = utc_now()
        with self.connect(write=True) as connection:
            connection.execute("DELETE FROM tags WHERE repository_id=?", (repository_id,))
            connection.executemany(
                "INSERT INTO tags(repository_id,name,commit_sha,refreshed_at) VALUES(?,?,?,?)",
                [(repository_id, tag["name"], tag.get("commit_sha"), now) for tag in tags],
            )
            connection.execute(
                "UPDATE repositories SET tags_refreshed_at=?,updated_at=? WHERE id=?",
                (now, now, repository_id),
            )

    def create_operation(
        self,
        command: str,
        *,
        repository_id: str | None = None,
        artifact_id: str | None = None,
        target: str | None = None,
        phase: str = "queued",
        message: str = "Queued.",
    ) -> str:
        """Create an operation and its first append-only event."""
        operation_id = uuid.uuid4().hex
        now = utc_now()
        target = redact_text(target) if target else None
        message = redact_text(message)
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO operations(
                    id,command,repository_id,artifact_id,target,status,phase,message,pid,owner_token,
                    started_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    operation_id,
                    command,
                    repository_id,
                    artifact_id,
                    target,
                    OperationStatus.QUEUED,
                    phase,
                    message,
                    os.getpid(),
                    _process_start_token(os.getpid()),
                    now,
                    now,
                ),
            )
            self._insert_event(connection, operation_id, OperationStatus.QUEUED, phase, message, now)
        return operation_id

    def update_operation(
        self,
        operation_id: str,
        *,
        status: str,
        phase: str,
        message: str,
        progress_current: int | None = None,
        progress_total: int | None = None,
        progress_unit: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        artifact_id: str | None = None,
    ) -> None:
        """Update one operation snapshot and append a matching event."""
        now = utc_now()
        ended_at = now if status in {
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.INTERRUPTED,
        } else None
        safe_message = redact_text(message)
        safe_error = redact_text(error_message) if error_message else None
        with self.connect(write=True) as connection:
            changed = connection.execute(
                """
                UPDATE operations SET
                    status=?,phase=?,message=?,progress_current=?,progress_total=?,progress_unit=?,
                    error_code=?,error_message=?,artifact_id=COALESCE(?,artifact_id),updated_at=?,ended_at=?
                WHERE id=?
                """,
                (
                    status,
                    phase,
                    safe_message,
                    progress_current,
                    progress_total,
                    progress_unit,
                    error_code,
                    safe_error,
                    artifact_id,
                    now,
                    ended_at,
                    operation_id,
                ),
            ).rowcount
            if changed != 1:
                raise NotFoundError(f"Operation not found: {operation_id}")
            self._insert_event(
                connection,
                operation_id,
                status,
                phase,
                safe_message,
                now,
                progress_current,
                progress_total,
                progress_unit,
            )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        operation_id: str,
        status: str,
        phase: str,
        message: str,
        timestamp: str,
        progress_current: int | None = None,
        progress_total: int | None = None,
        progress_unit: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO operation_events(
                operation_id,status,phase,progress_current,progress_total,progress_unit,message,timestamp
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                operation_id,
                status,
                phase,
                progress_current,
                progress_total,
                progress_unit,
                message,
                timestamp,
            ),
        )

    def recover_stale_operations(self) -> int:
        """Atomically interrupt active operations whose owning process identity is gone."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id,pid,owner_token FROM operations
                WHERE status IN ('queued','waiting_lock','running')
                """
            ).fetchall()
        recovered = 0
        for row in rows:
            current_token = _process_start_token(row["pid"])
            if _pid_is_alive(row["pid"]) and (
                row["owner_token"] is None
                or current_token is None
                or current_token == row["owner_token"]
            ):
                continue
            now = utc_now()
            message = "Operation owner process is no longer running."
            with self.connect(write=True) as connection:
                changed = connection.execute(
                    """
                    UPDATE operations SET status=?,phase=?,message=?,error_code=?,error_message=?,
                        updated_at=?,ended_at=?
                    WHERE id=? AND pid=? AND owner_token IS ?
                        AND status IN ('queued','waiting_lock','running')
                    """,
                    (
                        OperationStatus.INTERRUPTED,
                        "recovered",
                        message,
                        "owner_process_missing",
                        "The operation was interrupted before completion.",
                        now,
                        now,
                        row["id"],
                        row["pid"],
                        row["owner_token"],
                    ),
                ).rowcount
                if changed:
                    self._insert_event(
                        connection,
                        row["id"],
                        OperationStatus.INTERRUPTED,
                        "recovered",
                        message,
                        now,
                    )
                    recovered += 1
        return recovered

    def resolve_repository(self, query: str) -> dict[str, Any]:
        """Resolve one repository by exact, prefix, substring, or conservative fuzzy match."""
        query_norm = normalize_text(query)
        if not query_norm:
            raise ValidationError("Repository query must not be empty.")
        scored: list[tuple[int, dict[str, Any]]] = []
        for repository in self.repositories():
            score = _score_repository(repository, query_norm)
            if score:
                scored.append((score, repository))
        if not scored:
            raise NotFoundError(f"No repository matched '{query}'.")
        scored.sort(key=lambda item: (-item[0], item[1]["canonical_name"].casefold()))
        if len(scored) == 1 or scored[0][0] >= scored[1][0] + 200:
            return scored[0][1]
        exact = [item for score, item in scored if score >= 1000]
        if len(exact) == 1:
            return exact[0]
        candidates = [
            {"repository_id": item["id"], "canonical_name": item["canonical_name"], "score": score}
            for score, item in scored[:10]
        ]
        raise AmbiguousError(f"Query '{query}' matched multiple repositories.", details={"candidates": candidates})

    def resolve_artifact(self, repository_id: str, ref: str | None = None) -> dict[str, Any] | None:
        """Resolve a ready artifact by exact ref or repository preference."""
        with self.connect(write=True) as connection:
            if ref:
                row = connection.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE repository_id=? AND ref=? COLLATE BINARY AND status='ready'
                    ORDER BY acquired_at DESC LIMIT 1
                    """,
                    (repository_id, ref),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT a.* FROM repositories r
                    LEFT JOIN artifacts a ON a.id=r.preferred_artifact_id
                    WHERE r.id=? AND a.status='ready'
                    """,
                    (repository_id,),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        SELECT * FROM artifacts WHERE repository_id=? AND status='ready'
                        ORDER BY external DESC, acquired_at DESC LIMIT 1
                        """,
                        (repository_id,),
                    ).fetchone()
            if row:
                connection.execute(
                    "UPDATE artifacts SET last_accessed_at=? WHERE id=?", (utc_now(), row["id"])
                )
        return dict(row) if row else None

    def artifacts(self, repository_id: str | None = None) -> list[dict[str, Any]]:
        """Return persisted artifacts for verification and maintenance workflows."""
        with self.connect() as connection:
            if repository_id:
                rows = connection.execute(
                    "SELECT * FROM artifacts WHERE repository_id=? ORDER BY acquired_at DESC",
                    (repository_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM artifacts ORDER BY acquired_at DESC"
                ).fetchall()
        return [dict(row) for row in rows]

    def artifact(self, artifact_identity: str) -> dict[str, Any] | None:
        """Return one artifact by its opaque public identity."""
        _validate_opaque_id(artifact_identity)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_identity,)
            ).fetchone()
        return dict(row) if row else None

    def package_binding(
        self, package_id: str, version: str | None = None
    ) -> dict[str, Any] | None:
        """Return one exact or uniquely latest package binding."""
        with self.connect() as connection:
            if version:
                row = connection.execute(
                    """
                    SELECT * FROM package_bindings
                    WHERE package_id=? COLLATE NOCASE AND version=? COLLATE NOCASE
                    """,
                    (package_id, version),
                ).fetchone()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM package_bindings WHERE package_id=? COLLATE NOCASE
                    ORDER BY created_at DESC
                    """,
                    (package_id,),
                ).fetchall()
                row = rows[0] if rows else None
        return dict(row) if row else None

    def repositories(self) -> list[dict[str, Any]]:
        """Return dashboard-ready repository summaries."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.*,
                    (SELECT COUNT(*) FROM artifacts a WHERE a.repository_id=r.id) AS artifact_count,
                    (SELECT COUNT(*) FROM artifacts a WHERE a.repository_id=r.id AND a.status='ready') AS ready_artifact_count,
                    (SELECT COUNT(*) FROM artifacts a WHERE a.repository_id=r.id AND (
                        a.status!='ready' OR a.verification_state='failed'
                        OR (a.external=0 AND a.verification_state!='verified')
                    )) AS artifact_issue_count,
                    (SELECT COUNT(*) FROM tags t WHERE t.repository_id=r.id) AS tag_count,
                    (SELECT COUNT(*) FROM local_sources l WHERE l.repository_id=r.id) AS local_source_count,
                    pa.ref AS preferred_ref, pa.source_path AS preferred_source_path,
                    pa.status AS preferred_status, pa.verification_state AS preferred_verification_state,
                    pa.resolution_kind AS preferred_resolution_kind, pa.detected_version AS preferred_version,
                    pa.actual_commit AS preferred_commit, pa.source_bytes AS preferred_source_bytes,
                    pa.archive_bytes AS preferred_archive_bytes
                FROM repositories r
                LEFT JOIN artifacts pa ON pa.id=r.preferred_artifact_id
                ORDER BY r.canonical_name COLLATE NOCASE
                """
            ).fetchall()
            aliases = _group_aliases(connection)
            package_search_terms = _group_package_search_terms(connection)
        return [
            _repository_summary(
                dict(row),
                aliases.get(row["id"], []),
                package_search_terms.get(row["id"], []),
                self.root,
            )
            for row in rows
        ]

    def repository(self, repository_id: str) -> dict[str, Any]:
        """Return a complete repository detail projection."""
        _validate_opaque_id(repository_id)
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM repositories WHERE id=?", (repository_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Repository not found: {repository_id}")
            aliases = [
                item["alias"]
                for item in connection.execute(
                    "SELECT alias FROM aliases WHERE repository_id=? ORDER BY alias COLLATE NOCASE",
                    (repository_id,),
                )
            ]
            artifacts = [dict(item) for item in connection.execute(
                "SELECT * FROM artifacts WHERE repository_id=? ORDER BY acquired_at DESC", (repository_id,)
            )]
            local_sources = []
            for item in connection.execute(
                "SELECT * FROM local_sources WHERE repository_id=? ORDER BY added_at DESC", (repository_id,)
            ):
                payload = dict(item)
                payload["markers"] = json.loads(payload.pop("markers_json"))
                payload["dirty"] = None if payload["dirty"] is None else bool(payload["dirty"])
                payload["exists"] = bool(payload.pop("exists_state"))
                payload.pop("generation", None)
                local_sources.append(payload)
            packages = [dict(item) for item in connection.execute(
                "SELECT * FROM package_bindings WHERE repository_id=? ORDER BY ecosystem,package_id,version",
                (repository_id,),
            )]
        row_payload = dict(row)
        manifest = _cached_manifest(row_payload, self.root, repository_id)
        payload = row_payload
        payload.update(
            {
                "aliases": aliases,
                "artifacts": [_artifact_projection(item) for item in artifacts],
                "local_sources": local_sources,
                "packages": packages,
                "manifest": manifest,
            }
        )
        preferred = next((item for item in payload["artifacts"] if item["id"] == payload["preferred_artifact_id"]), None)
        payload["preferred_artifact"] = preferred
        payload["health"] = _repository_health(payload)
        return payload

    def tags(self, repository_id: str) -> list[dict[str, Any]]:
        """Return all cached tags for one validated repository ID."""
        _validate_opaque_id(repository_id)
        with self.connect() as connection:
            exists = connection.execute("SELECT 1 FROM repositories WHERE id=?", (repository_id,)).fetchone()
            if not exists:
                raise NotFoundError(f"Repository not found: {repository_id}")
            return [dict(row) for row in connection.execute(
                "SELECT name,commit_sha,refreshed_at FROM tags WHERE repository_id=? ORDER BY name COLLATE NOCASE",
                (repository_id,),
            )]

    def operations(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return recent operation snapshots newest first."""
        limit = min(max(limit, 1), 1000)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM operations ORDER BY updated_at DESC LIMIT ?", (limit,)
            )]

    def events(self, after_sequence: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Return incremental operation events by global sequence."""
        limit = min(max(limit, 1), 2000)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                """
                SELECT * FROM operation_events WHERE sequence>? ORDER BY sequence ASC LIMIT ?
                """,
                (max(after_sequence, 0), limit),
            )]

    def summary(self) -> dict[str, Any]:
        """Return fast aggregate catalog and storage health from cached metadata."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM repositories) AS repository_count,
                    (SELECT COUNT(*) FROM repositories WHERE origin_kind='local') AS local_repository_count,
                    (SELECT COUNT(*) FROM artifacts) AS artifact_count,
                    (SELECT COUNT(*) FROM artifacts WHERE status='ready') AS ready_artifact_count,
                    (SELECT COUNT(*) FROM artifacts WHERE verification_state='verified') AS verified_artifact_count,
                    (SELECT COUNT(*) FROM artifacts WHERE verification_state='verified'
                        AND resolution_kind IN ('exact_commit','exact_tag')) AS verified_exact_count,
                    (SELECT COUNT(*) FROM tags) AS tag_count,
                    (SELECT COALESCE(SUM(source_bytes),0) FROM artifacts WHERE external=0) AS source_bytes,
                    (SELECT COALESCE(SUM(archive_bytes),0) FROM artifacts WHERE external=0) AS archive_bytes,
                    (SELECT COUNT(*) FROM operations WHERE status IN ('queued','waiting_lock','running')) AS active_operation_count,
                    (SELECT COUNT(*) FROM operations WHERE status='failed') AS failed_operation_count,
                    (SELECT COUNT(*) FROM artifacts a WHERE
                        a.status!='ready' OR a.verification_state='failed'
                        OR (a.external=0 AND a.verification_state!='verified'))
                        AS artifact_attention_count,
                    (SELECT COUNT(*) FROM repositories
                        WHERE remote_url IS NOT NULL AND (
                            tags_refreshed_at IS NULL OR julianday(tags_refreshed_at) < julianday('now','-1 day')
                        )) AS stale_tag_count
                """
            ).fetchone()
            cached = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key,value FROM metadata WHERE key LIKE 'reconciled_%'"
                )
            }
        usage = shutil.disk_usage(self.root)
        payload = dict(row)
        payload.update(
            {
                "catalog_root": str(self.root),
                "schema_version": SCHEMA_VERSION,
                "app_version": APP_VERSION,
                "free_bytes": usage.free,
                "disk_free_bytes": usage.free,
                "total_bytes": usage.total,
                "measured_at": utc_now(),
                "reconciled_at": cached.get("reconciled_at"),
                "deep_reconciled_at": cached.get("reconciled_deep_at"),
            }
        )
        manifest_warnings = int(cached.get("reconciled_manifest_warning_count", "0"))
        payload["attention_count"] = payload.pop("artifact_attention_count") + manifest_warnings
        payload["integrity_warning_count"] = payload["attention_count"]
        payload["manifest_ready_count"] = int(cached.get("reconciled_manifest_ready_count", "0"))
        payload["disk_used_bytes"] = int(
            cached.get(
                "reconciled_disk_used_bytes",
                str(payload["source_bytes"] + payload["archive_bytes"]),
            )
        )
        return payload

    def reconcile_cached_metrics(self, *, force_deep: bool = False) -> dict[str, Any]:
        """Refresh cached dashboard metrics without repeatedly hashing every artifact.

        Manifest and local-source snapshots are inexpensive enough to refresh on every
        dashboard cycle. Full artifact digests and recursive disk measurements are
        throttled because a user-level catalog can contain many large source trees.
        """
        from _catalog_artifacts import ArtifactManager
        from _catalog_providers import inspect_local_git

        with self.connect() as connection:
            repository_ids = [
                row["id"] for row in connection.execute("SELECT id FROM repositories")
            ]
            artifacts = [dict(row) for row in connection.execute("SELECT * FROM artifacts")]
            local_sources = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,canonical_path,generation FROM local_sources"
                )
            ]
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key,value FROM metadata WHERE key LIKE 'reconciled_%'"
                )
            }
        deep_at = _parse_utc(metadata.get("reconciled_deep_at"))
        deep_due = force_deep or deep_at is None or (
            datetime.now(timezone.utc) - deep_at >= timedelta(hours=6)
        )
        manifest_ready = 0
        manifest_warnings = 0
        for repository_id in repository_ids:
            manifest = _manifest_status(self.root, repository_id)
            status = manifest["status"]
            manifest_ready += int(status == "enriched")
            manifest_warnings += int(status == "invalid")
            self.update_manifest_cache(
                repository_id,
                status=status,
                summary=manifest.get("summary"),
                error=manifest.get("error"),
            )
        artifact_results: list[tuple[Any, ...]] = []
        if deep_due:
            manager = ArtifactManager(self.root)
            for artifact in artifacts:
                try:
                    result = manager.verify_record(artifact)
                except (CatalogError, OSError, RuntimeError):
                    result = {
                        "status": "invalid",
                        "verification_state": "failed",
                        "source_bytes": None,
                        "archive_bytes": None,
                        "file_count": None,
                        "actual_commit": None,
                    }
                artifact_results.append(
                    (
                        result["status"],
                        result["verification_state"],
                        result.get("source_bytes"),
                        result.get("archive_bytes"),
                        result.get("file_count"),
                        result.get("actual_commit"),
                        utc_now(),
                        artifact["id"],
                        artifact["generation"],
                    )
                )
        local_results: list[tuple[Any, ...]] = []
        for source in local_sources:
            candidate = Path(str(source["canonical_path"])).expanduser()
            exists = candidate.is_dir() and not is_link_or_reparse(candidate)
            snapshot: dict[str, Any] = {}
            if exists:
                try:
                    snapshot = inspect_local_git(candidate)
                except (CatalogError, OSError, RuntimeError):
                    snapshot = {}
            dirty = snapshot.get("dirty")
            local_results.append(
                (
                    int(exists),
                    snapshot.get("branch"),
                    snapshot.get("commit_sha"),
                    None if dirty is None else int(bool(dirty)),
                    utc_now(),
                    source["id"],
                    source["generation"],
                )
            )
        if artifact_results or local_results:
            with self.connect(write=True) as connection:
                if artifact_results:
                    connection.executemany(
                        """
                        UPDATE artifacts SET status=?,verification_state=?,source_bytes=?,
                            archive_bytes=?,file_count=?,actual_commit=?,verified_at=?
                        WHERE id=? AND generation=?
                        """,
                        artifact_results,
                    )
                if local_results:
                    connection.executemany(
                        """
                        UPDATE local_sources
                        SET exists_state=?,branch=?,commit_sha=?,dirty=?,verified_at=?
                        WHERE id=? AND generation=?
                        """,
                        local_results,
                    )
        reconciled_at = utc_now()
        if deep_due:
            repos_bytes, _ = directory_size(self.root / "repos")
            staging_bytes, _ = directory_size(self.root / "staging")
            disk_used_bytes = repos_bytes + staging_bytes
        else:
            disk_used_bytes = int(
                metadata.get(
                    "reconciled_disk_used_bytes",
                    str(
                        sum(int(item.get("source_bytes") or 0) for item in artifacts)
                        + sum(int(item.get("archive_bytes") or 0) for item in artifacts)
                    ),
                )
            )
        values = {
            "reconciled_manifest_ready_count": str(manifest_ready),
            "reconciled_manifest_warning_count": str(manifest_warnings),
            "reconciled_disk_used_bytes": str(disk_used_bytes),
            "reconciled_at": reconciled_at,
        }
        if deep_due:
            values["reconciled_deep_at"] = reconciled_at
        with self.connect(write=True) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                list(values.items()),
            )
        return {
            "manifest_ready_count": manifest_ready,
            "manifest_warning_count": manifest_warnings,
            "disk_used_bytes": disk_used_bytes,
            "reconciled_at": reconciled_at,
            "deep_reconciled": deep_due,
            "deep_reconciled_at": values.get(
                "reconciled_deep_at", metadata.get("reconciled_deep_at")
            ),
        }

    def health(self) -> dict[str, Any]:
        """Return dashboard health even when the catalog is empty."""
        return {
            "status": "ok",
            "app_version": APP_VERSION,
            "schema_version": SCHEMA_VERSION,
            "catalog_root": str(self.root),
            "database": str(self.database_path),
            "timestamp": utc_now(),
        }

    def delete_repository(self, repository_id: str) -> None:
        """Delete repository metadata; managed files are handled separately and safely."""
        with self.connect(write=True) as connection:
            changed = connection.execute("DELETE FROM repositories WHERE id=?", (repository_id,)).rowcount
            if changed != 1:
                raise NotFoundError(f"Repository not found: {repository_id}")


def _merge_origin(current: str, incoming: str) -> str:
    if current == incoming:
        return current
    if "local" in {current, incoming} or current == "hybrid" or incoming == "hybrid":
        return "hybrid"
    return incoming


def _merge_provider(current: str, incoming: str) -> str:
    """Keep the provider with the strongest remote-specific capabilities."""
    precedence = {"local": 0, "git": 1, "github": 2}
    return current if precedence[current] >= precedence[incoming] else incoming


def _origin_with_local_source(
    connection: sqlite3.Connection,
    repository_id: str,
) -> str:
    row = connection.execute(
        """
        SELECT provider,
            EXISTS(SELECT 1 FROM local_sources WHERE repository_id=repositories.id) AS has_local
        FROM repositories WHERE id=?
        """,
        (repository_id,),
    ).fetchone()
    if row is None:
        raise ValidationError(f"Repository not found: {repository_id}")
    if row["has_local"] and row["provider"] in {"git", "github"}:
        return "hybrid"
    return row["provider"] if row["provider"] in {"git", "github"} else "local"


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_start_token(pid: int) -> str | None:
    """Read an OS process creation token so PID reuse cannot impersonate an operation owner."""
    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            fields = proc_stat.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
            return f"proc:{fields[19]}" if len(fields) > 19 else None
        except (OSError, IndexError):
            return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                if not kernel32.GetProcessTimes(
                    process,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                return f"windows:{value}"
            finally:
                kernel32.CloseHandle(process)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    started = completed.stdout.strip()
    return f"ps:{started}" if completed.returncode == 0 and started else None


def _validate_opaque_id(value: str) -> None:
    if not value or len(value) > 160 or not all(character.isalnum() or character in "-_" for character in value):
        raise ValidationError("Invalid opaque repository identifier.")


def _group_aliases(connection: sqlite3.Connection) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for row in connection.execute("SELECT repository_id,alias FROM aliases ORDER BY alias COLLATE NOCASE"):
        grouped.setdefault(row["repository_id"], []).append(row["alias"])
    return grouped


def _group_package_search_terms(connection: sqlite3.Connection) -> dict[str, list[str]]:
    """Return package/version search labels in one bulk query."""

    grouped: dict[str, list[str]] = {}
    for row in connection.execute(
        """
        SELECT repository_id,package_id,version
        FROM package_bindings
        ORDER BY package_id COLLATE NOCASE,version COLLATE NOCASE
        """
    ):
        grouped.setdefault(row["repository_id"], []).append(
            f"{row['package_id']} {row['version']}"
        )
    return grouped


def _score_repository(repository: dict[str, Any], query_norm: str) -> int:
    terms = [
        repository["id"],
        repository["canonical_name"],
        repository["display_name"],
        *(repository.get("aliases") or []),
    ]
    best = 0
    compact_query = query_norm.replace(" ", "")
    for term in terms:
        normalized = normalize_text(str(term))
        if normalized == query_norm:
            return 1000
        if normalized.replace(" ", "") == compact_query:
            best = max(best, 920)
        elif normalized.startswith(query_norm):
            best = max(best, 760)
        elif query_norm in normalized:
            best = max(best, 620)
        else:
            ratio = difflib.SequenceMatcher(None, query_norm, normalized).ratio()
            if ratio >= 0.8:
                best = max(best, int(ratio * 500))
    return best


def _artifact_projection(artifact: dict[str, Any]) -> dict[str, Any]:
    payload = dict(artifact)
    payload.pop("generation", None)
    payload["source_exists"] = payload.get("status") == "ready"
    payload["archive_exists"] = bool(
        payload.get("archive_path") and payload.get("status") == "ready"
    )
    payload["external"] = bool(payload.get("external"))
    return payload


def _manifest_status(root: Path, repository_id: str) -> dict[str, Any]:
    try:
        repos_root = ensure_within(root, root / "repos")
        repo_root = ensure_within(repos_root, repos_root / repository_id)
    except ValidationError as exc:
        return {"status": "invalid", "path": None, "error": str(exc)}
    enriched = repo_root / "manifest.json"
    stub = repo_root / "manifest.stub.json"
    selected = (
        enriched
        if enriched.exists() or is_link_or_reparse(enriched)
        else stub
        if stub.exists() or is_link_or_reparse(stub)
        else None
    )
    if selected is None:
        return {"status": "missing", "path": None}
    if is_link_or_reparse(selected):
        return {
            "status": "invalid",
            "path": str(selected),
            "error": "Manifest cannot be a link or reparse point.",
        }
    try:
        file_status = selected.stat(follow_symlinks=False)
        if not stat.S_ISREG(file_status.st_mode) or file_status.st_nlink != 1:
            raise ValueError("manifest must be a single regular file")
        if file_status.st_size > 2 * 1024 * 1024:
            raise ValueError("manifest exceeds the 2 MiB safety limit")
        with selected.open("rb") as handle:
            content = handle.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("manifest exceeds the 2 MiB safety limit")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("manifest root must be an object")
        summary = payload.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise ValueError("manifest summary must be a string or null")
        if summary is not None and len(summary) > 8192:
            raise ValueError("manifest summary exceeds the 8 KiB safety limit")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "invalid", "path": str(selected), "error": redact_text(str(exc))}
    return {
        "status": "enriched" if selected == enriched else "stub",
        "path": str(selected),
        "summary": redact_text(summary) if summary else summary,
    }


def _repository_summary(
    row: dict[str, Any],
    aliases: list[str],
    package_search_terms: list[str],
    root: Path,
) -> dict[str, Any]:
    payload = dict(row)
    payload["aliases"] = aliases
    payload["package_search_terms"] = package_search_terms
    payload["preferred_source_exists"] = payload.get("preferred_status") == "ready"
    payload["manifest"] = _cached_manifest(payload, root, payload["id"])
    payload["health"] = _summary_health(payload)
    return payload


def _cached_manifest(row: dict[str, Any], root: Path, repository_id: str) -> dict[str, Any]:
    status = row.pop("manifest_status", "missing")
    summary = row.pop("manifest_summary", None)
    error = row.pop("manifest_error", None)
    filename = "manifest.json" if status == "enriched" else "manifest.stub.json" if status == "stub" else None
    payload: dict[str, Any] = {
        "status": status,
        "path": str(root / "repos" / repository_id / filename) if filename else None,
        "summary": summary,
    }
    if error:
        payload["error"] = error
    return payload


def _summary_health(repository: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    if repository.get("ready_artifact_count", 0) == 0:
        issues.append({"code": "source_not_cached", "severity": "info", "message": "No source artifact is available."})
    elif not repository.get("preferred_source_exists"):
        issues.append({"code": "preferred_source_missing", "severity": "error", "message": "The preferred source path is missing."})
    if repository.get("preferred_verification_state") == "failed":
        issues.append({"code": "verification_failed", "severity": "error", "message": "Source verification failed."})
    elif repository.get("preferred_verification_state") == "unverified":
        issues.append({"code": "source_unverified", "severity": "warning", "message": "The preferred source is unverified."})
    if repository.get("artifact_issue_count", 0):
        issues.append(
            {
                "code": "artifact_integrity_warning",
                "severity": "error",
                "message": "One or more cached artifacts require integrity attention.",
            }
        )
    if repository.get("manifest", {}).get("status") == "invalid":
        issues.append({"code": "manifest_invalid", "severity": "warning", "message": "The enriched manifest is invalid."})
    severity = "ok"
    for candidate in ("error", "warning", "info"):
        if any(issue["severity"] == candidate for issue in issues):
            severity = candidate
            break
    return {"severity": severity, "issues": issues}


def _repository_health(repository: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "ready_artifact_count": sum(1 for item in repository["artifacts"] if item["status"] == "ready"),
        "preferred_source_exists": bool(
            repository.get("preferred_artifact") and repository["preferred_artifact"]["source_exists"]
        ),
        "preferred_verification_state": (
            repository["preferred_artifact"]["verification_state"]
            if repository.get("preferred_artifact")
            else None
        ),
        "artifact_issue_count": sum(
            1
            for item in repository["artifacts"]
            if item["status"] != "ready"
            or item["verification_state"] == "failed"
            or (not item["external"] and item["verification_state"] != "verified")
        ),
        "manifest": repository["manifest"],
    }
    return _summary_health(summary)
