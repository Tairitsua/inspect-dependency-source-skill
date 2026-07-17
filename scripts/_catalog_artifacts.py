"""Transactional acquisition, extraction, promotion, and verification of source artifacts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from _catalog_models import (
    ArtifactStatus,
    ProviderError,
    ResolvedRef,
    ValidationError,
    VerificationState,
)
from _catalog_paths import (
    artifact_id,
    artifact_lock,
    directory_size,
    ensure_within,
    guarded_remove,
    is_link_or_reparse,
    is_windows_reparse_point,
)
from _catalog_providers import GitHubClient, fetch_generic_git, inspect_local_git
from _catalog_store import utc_now


MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200_000
MAX_MEMBER_PATH_LENGTH = 512
MAX_ARTIFACT_MANIFEST_BYTES = 1024 * 1024


class ArtifactManager:
    """Own staged provider output and atomic promotion into managed storage."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)

    @contextlib.contextmanager
    def repository_guard(
        self,
        repository_id: str,
        *,
        on_wait: Callable[[], None] | None = None,
        on_acquired: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        """Serialize repository acquisition and removal across agents and processes."""
        with artifact_lock(
            self._repository_lock_path(repository_id),
            on_wait=on_wait,
            on_acquired=on_acquired,
        ):
            yield

    def acquire_github(
        self,
        *,
        repository_id: str,
        full_name: str,
        resolved: ResolvedRef,
        client: GitHubClient,
        force: bool,
        on_wait: Callable[[], None] | None = None,
        on_acquired: Callable[[], None] | None = None,
        persist_validated: Callable[[dict[str, Any]], None] | None = None,
        load_trusted: Callable[[], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Download and atomically promote one exact GitHub archive."""
        identity = artifact_id(repository_id, "github_archive", resolved.resolved_ref)
        with artifact_lock(
            self._lock_path(repository_id, identity),
            on_wait=on_wait,
            on_acquired=on_acquired,
        ):
            self._recover_artifact(repository_id, identity)
            existing = self._existing_artifact(repository_id, identity)
            if existing and not force:
                artifact = self.describe_managed(
                    repository_id=repository_id,
                    identity=identity,
                    kind="github_archive",
                    resolved=resolved,
                )
                self._validate_trusted_artifact(
                    artifact,
                    load_trusted() if load_trusted else None,
                )
                if persist_validated:
                    persist_validated(artifact)
                return artifact
            stage = self._new_stage(identity)
            try:
                archive = stage / "archive.tar.gz"
                client.download_archive(
                    full_name,
                    resolved.commit_sha or resolved.resolved_ref,
                    archive,
                    max_bytes=MAX_ARCHIVE_BYTES,
                )
                raw = stage / "expanded"
                raw.mkdir()
                safe_extract_tar(archive, raw)
                source = _select_archive_root(raw)
                promoted_source = stage / "source"
                source.rename(promoted_source)
                guarded_remove(stage, raw)
                validate_managed_source_tree(promoted_source)
                manifest = self._artifact_manifest(
                    identity=identity,
                    kind="github_archive",
                    resolved=resolved,
                    source_path=promoted_source,
                    archive_path=archive,
                )
                _write_json(stage / "artifact.json", manifest)
                with self._promote(repository_id, identity, stage, force=force) as final:
                    artifact = self.describe_managed(
                        repository_id=repository_id,
                        identity=identity,
                        kind="github_archive",
                        resolved=resolved,
                        final=final,
                    )
                    if persist_validated:
                        persist_validated(artifact)
                    return artifact
            except Exception:
                if stage.exists():
                    guarded_remove(self.root / "staging", stage)
                raise

    def acquire_git(
        self,
        *,
        repository_id: str,
        remote_url: str,
        resolved: ResolvedRef,
        force: bool,
        on_wait: Callable[[], None] | None = None,
        on_acquired: Callable[[], None] | None = None,
        persist_validated: Callable[[dict[str, Any]], None] | None = None,
        load_trusted: Callable[[], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Fetch one exact generic Git ref into staging before atomic promotion."""
        identity = artifact_id(repository_id, "git_clone", resolved.resolved_ref)
        with artifact_lock(
            self._lock_path(repository_id, identity),
            on_wait=on_wait,
            on_acquired=on_acquired,
        ):
            self._recover_artifact(repository_id, identity)
            existing = self._existing_artifact(repository_id, identity)
            if existing and not force:
                artifact = self.describe_managed(
                    repository_id=repository_id,
                    identity=identity,
                    kind="git_clone",
                    resolved=resolved,
                )
                self._validate_trusted_artifact(
                    artifact,
                    load_trusted() if load_trusted else None,
                )
                if persist_validated:
                    persist_validated(artifact)
                return artifact
            stage = self._new_stage(identity)
            try:
                source = stage / "source"
                actual_commit = fetch_generic_git(remote_url, resolved.resolved_ref, source)
                validate_managed_source_tree(source)
                if resolved.commit_sha and not _commits_match(actual_commit, resolved.commit_sha):
                    raise ProviderError(
                        f"Fetched commit {actual_commit} does not match expected {resolved.commit_sha}."
                    )
                effective = ResolvedRef(
                    resolved.requested_ref,
                    resolved.resolved_ref,
                    actual_commit,
                    resolved.resolution_kind,
                )
                manifest = self._artifact_manifest(
                    identity=identity,
                    kind="git_clone",
                    resolved=effective,
                    source_path=source,
                    archive_path=None,
                )
                _write_json(stage / "artifact.json", manifest)
                with self._promote(repository_id, identity, stage, force=force) as final:
                    artifact = self.describe_managed(
                        repository_id=repository_id,
                        identity=identity,
                        kind="git_clone",
                        resolved=effective,
                        final=final,
                    )
                    if persist_validated:
                        persist_validated(artifact)
                    return artifact
            except Exception:
                if stage.exists():
                    guarded_remove(self.root / "staging", stage)
                raise

    def describe_managed(
        self,
        *,
        repository_id: str,
        identity: str,
        kind: str,
        resolved: ResolvedRef,
        final: Path | None = None,
    ) -> dict[str, Any]:
        """Build a verified storage record for one managed artifact directory."""
        expected_directory = self._artifact_dir(repository_id, identity)
        directory = expected_directory if final is None else ensure_within(
            self.root / "repos", final
        )
        if directory != expected_directory:
            raise ValidationError("Managed artifact directory does not match its identity.")
        source = directory / "source"
        archive = directory / "archive.tar.gz"
        if not source.is_dir() or is_link_or_reparse(source):
            raise ProviderError(f"Managed source artifact is missing or unsafe: {source}")
        validate_managed_source_tree(source)
        source_bytes, file_count = directory_size(source)
        archive_bytes = (
            archive.stat().st_size
            if archive.is_file() and not is_link_or_reparse(archive)
            else 0
        )
        actual_commit = resolved.commit_sha
        manifest = _load_artifact_manifest(directory / "artifact.json")
        current_source_digest = manifest.get("source_digest")
        current_archive_digest = manifest.get("archive_digest")
        if kind == "github_archive":
            current_source_digest = source_tree_digest(source)
            current_archive_digest = _sha256_file(archive) if archive.is_file() else None
            if (
                manifest.get("source_digest") != current_source_digest
                or manifest.get("archive_digest") != current_archive_digest
            ):
                raise ProviderError(
                    f"Cached artifact integrity check failed for {identity}; use --force to replace it."
                )
        elif kind == "git_clone":
            current_source_digest = source_tree_digest(
                source, excluded_top_level=frozenset({".git"})
            )
            if manifest.get("source_digest") != current_source_digest:
                raise ProviderError(
                    f"Cached Git working tree digest failed for {identity}; use --force to replace it."
                )
        if manifest.get("commit_sha"):
            actual_commit = str(manifest["commit_sha"])
        if resolved.commit_sha and (
            not actual_commit or not _commits_match(actual_commit, resolved.commit_sha)
        ):
            raise ProviderError(
                f"Cached artifact commit {actual_commit or 'unknown'} does not match expected {resolved.commit_sha}."
            )
        if kind == "git_clone":
            snapshot = inspect_local_git(source)
            if (
                not snapshot.get("commit_sha")
                or not actual_commit
                or not _commits_match(str(snapshot["commit_sha"]), str(actual_commit))
                or snapshot.get("dirty") is not False
            ):
                raise ProviderError(
                    f"Cached Git artifact integrity check failed for {identity}; use --force to replace it."
                )
        verified = bool(actual_commit) or kind == "github_archive"
        return {
            "id": identity,
            "kind": kind,
            "ref": resolved.resolved_ref,
            "source_path": str(source),
            "archive_path": str(archive) if archive.is_file() else None,
            "status": ArtifactStatus.READY,
            "resolution_kind": resolved.resolution_kind,
            "expected_commit": resolved.commit_sha,
            "actual_commit": actual_commit,
            "verification_state": VerificationState.VERIFIED if verified else VerificationState.UNVERIFIED,
            "acquired_at": manifest.get("acquired_at") or utc_now(),
            "verified_at": utc_now(),
            "last_accessed_at": utc_now(),
            "source_bytes": source_bytes,
            "archive_bytes": archive_bytes,
            "file_count": file_count,
            "source_digest": current_source_digest,
            "archive_digest": current_archive_digest,
            "external": False,
        }

    def verify_record(self, artifact: dict[str, Any]) -> dict[str, Any]:
        """Verify one persisted artifact without trusting its stored paths blindly."""
        source = Path(str(artifact["source_path"])).expanduser().resolve(strict=False)
        safe = True
        try:
            source = self.validated_source_path(artifact)
            if not artifact.get("external"):
                validate_managed_source_tree(source)
        except (ProviderError, ValidationError):
            safe = False
        if not safe:
            return {
                "status": ArtifactStatus.MISSING if not source.exists() else ArtifactStatus.INVALID,
                "verification_state": VerificationState.FAILED,
                "source_bytes": None,
                "archive_bytes": None,
                "file_count": None,
                "actual_commit": None,
            }
        if artifact.get("external"):
            # User-owned trees are not catalog cache usage. Walking a multi-gigabyte
            # external dependency on every resolve adds latency without strengthening
            # path or Git provenance checks.
            source_bytes = artifact.get("source_bytes")
            file_count = artifact.get("file_count")
        else:
            source_bytes, file_count = directory_size(source)
        kind = artifact.get("kind")
        archive_path = artifact.get("archive_path")
        archive = None
        archive_bytes = 0
        if archive_path:
            candidate = Path(str(archive_path)).expanduser()
            if not is_link_or_reparse(candidate):
                archive = candidate.resolve(strict=False)
        actual_commit = artifact.get("actual_commit")
        status = ArtifactStatus.READY
        verification = VerificationState.UNVERIFIED
        if kind == "github_archive":
            expected_directory = self._artifact_dir(
                str(artifact["repository_id"]), str(artifact["id"])
            )
            expected_archive = (expected_directory / "archive.tar.gz").resolve(strict=False)
            if archive and archive == expected_archive and archive.is_file():
                archive_bytes = archive.stat().st_size
            source_digest = source_tree_digest(source)
            archive_digest = _sha256_file(archive) if archive and archive == expected_archive else None
            if (
                not artifact.get("source_digest")
                or source_digest != artifact.get("source_digest")
                or not artifact.get("archive_digest")
                or archive_digest != artifact.get("archive_digest")
            ):
                status = ArtifactStatus.INVALID
                verification = VerificationState.FAILED
            else:
                actual_commit = artifact.get("expected_commit") or actual_commit
                verification = VerificationState.VERIFIED
        elif kind == "git_clone":
            snapshot = inspect_local_git(source)
            actual_commit = snapshot.get("commit_sha")
            expected = artifact.get("expected_commit")
            current_source_digest = source_tree_digest(
                source, excluded_top_level=frozenset({".git"})
            )
            if (
                not actual_commit
                or not expected
                or not _commits_match(str(actual_commit), str(expected))
                or snapshot.get("dirty") is not False
                or not artifact.get("source_digest")
                or current_source_digest != artifact.get("source_digest")
            ):
                status = ArtifactStatus.INVALID
                verification = VerificationState.FAILED
            else:
                verification = VerificationState.VERIFIED
        elif kind == "local":
            snapshot = inspect_local_git(source)
            actual_commit = snapshot.get("commit_sha")
            expected = artifact.get("expected_commit")
            if (
                actual_commit
                and expected
                and _commits_match(str(actual_commit), str(expected))
                and snapshot.get("dirty") is False
            ):
                verification = VerificationState.VERIFIED
        return {
            "status": status,
            "verification_state": verification,
            "source_bytes": source_bytes,
            "archive_bytes": archive_bytes,
            "file_count": file_count,
            "actual_commit": actual_commit,
        }

    def validated_source_path(self, artifact: dict[str, Any]) -> Path:
        """Return a usable source path after validating managed ownership and symlinks."""
        if artifact.get("external"):
            persisted_source = Path(str(artifact["source_path"])).expanduser()
            lexical_source = Path(os.path.abspath(persisted_source))
            source = lexical_source.resolve(strict=False)
            safe = (
                persisted_source.is_absolute()
                and source == lexical_source
                and source.is_dir()
                and not is_link_or_reparse(lexical_source)
            )
        else:
            expected = self._artifact_dir(str(artifact["repository_id"]), str(artifact["id"]))
            persisted_source = Path(str(artifact["source_path"])).expanduser()
            source = Path(os.path.abspath(persisted_source))
            expected_source = Path(os.path.abspath(expected / "source"))
            safe = (
                persisted_source.is_absolute()
                and source == expected_source
                and source.is_dir()
                and not is_link_or_reparse(source)
            )
        if not safe or is_link_or_reparse(source):
            raise ProviderError(f"Persisted source path is missing or unsafe: {source}")
        return source

    def purge_repository(self, repository_id: str) -> None:
        """Delete only a validated managed repository directory."""
        target = self._repository_root(repository_id)
        if target.exists() or is_link_or_reparse(target):
            guarded_remove(self.root / "repos", target)

    def repository_cache_path(self, repository_id: str) -> Path:
        """Return the validated managed directory targeted by a repository purge."""
        return self._repository_root(repository_id)

    @staticmethod
    def _validate_trusted_artifact(
        current: dict[str, Any], trusted: dict[str, Any] | None
    ) -> None:
        """Require current managed content to match the persisted catalog record."""
        kind = current.get("kind")
        if (
            trusted is None
            or trusted.get("id") != current.get("id")
            or kind not in {"github_archive", "git_clone"}
            or trusted.get("kind") != kind
            or trusted.get("status") != ArtifactStatus.READY
            or trusted.get("verification_state") != VerificationState.VERIFIED
            or not trusted.get("source_digest")
            or trusted.get("source_digest") != current.get("source_digest")
            or (
                kind == "github_archive"
                and (
                    not trusted.get("archive_digest")
                    or trusted.get("archive_digest") != current.get("archive_digest")
                )
            )
            or (
                trusted.get("expected_commit")
                and current.get("actual_commit")
                and not _commits_match(
                    str(current["actual_commit"]), str(trusted["expected_commit"])
                )
            )
        ):
            raise ProviderError(
                "Cached managed source does not match its trusted catalog record."
            )

    def _artifact_manifest(
        self,
        *,
        identity: str,
        kind: str,
        resolved: ResolvedRef,
        source_path: Path,
        archive_path: Path | None,
    ) -> dict[str, Any]:
        source_bytes, file_count = directory_size(source_path)
        source_digest = (
            source_tree_digest(
                source_path,
                excluded_top_level=(
                    frozenset({".git"}) if kind == "git_clone" else frozenset()
                ),
            )
            if kind in {"github_archive", "git_clone"}
            else None
        )
        return {
            "schema_version": 1,
            "artifact_id": identity,
            "kind": kind,
            "requested_ref": resolved.requested_ref,
            "resolved_ref": resolved.resolved_ref,
            "resolution_kind": resolved.resolution_kind,
            "commit_sha": resolved.commit_sha,
            "source_bytes": source_bytes,
            "archive_bytes": archive_path.stat().st_size if archive_path else 0,
            "file_count": file_count,
            "source_digest": source_digest,
            "archive_digest": _sha256_file(archive_path) if archive_path else None,
            "acquired_at": utc_now(),
        }

    def _new_stage(self, identity: str) -> Path:
        staging_root = self.root / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        stage = staging_root / f"{identity}-{uuid.uuid4().hex}"
        ensure_within(staging_root, stage)
        stage.mkdir()
        return stage

    @contextlib.contextmanager
    def _promote(
        self, repository_id: str, identity: str, stage: Path, *, force: bool
    ) -> Iterator[Path]:
        """Swap a staged artifact into place and retain its backup until validation succeeds."""
        repository_root = self._repository_root(repository_id)
        repository_root.mkdir(parents=True, exist_ok=True)
        artifacts_root = ensure_within(repository_root, repository_root / "artifacts")
        artifacts_root.mkdir(parents=True, exist_ok=True)
        final = ensure_within(artifacts_root, artifacts_root / identity)
        backup = artifacts_root / f".{identity}.previous-{uuid.uuid4().hex}"
        had_previous = final.exists()
        if had_previous and not force:
            raise ValidationError(f"Artifact already exists: {identity}")
        staged_promoted = False
        try:
            if had_previous:
                final.replace(backup)
            stage.replace(final)
            staged_promoted = True
            yield final
        except BaseException:
            try:
                if staged_promoted and (final.exists() or is_link_or_reparse(final)):
                    guarded_remove(artifacts_root, final)
                if had_previous and backup.exists() and not final.exists():
                    backup.replace(final)
            except Exception as rollback_error:
                raise ProviderError(
                    f"Artifact replacement failed and automatic rollback could not complete for {identity}."
                ) from rollback_error
            raise
        else:
            if backup.exists():
                with contextlib.suppress(OSError, ValidationError):
                    guarded_remove(artifacts_root, backup)

    def _recover_artifact(self, repository_id: str, identity: str) -> None:
        """Restore an interrupted force replacement and discard orphan staging safely."""
        repository_root = self._repository_root(repository_id)
        artifacts_root = ensure_within(repository_root, repository_root / "artifacts")
        final = ensure_within(artifacts_root, artifacts_root / identity)
        if artifacts_root.is_dir():
            backups = sorted(
                artifacts_root.glob(f".{identity}.previous-*"),
                key=lambda path: path.stat(follow_symlinks=False).st_mtime_ns,
                reverse=True,
            )
            if backups:
                newest = ensure_within(artifacts_root, backups.pop(0))
                if final.exists() or is_link_or_reparse(final):
                    guarded_remove(artifacts_root, final)
                newest.replace(final)
            for backup in backups:
                with contextlib.suppress(OSError, ValidationError):
                    guarded_remove(artifacts_root, backup)
        staging_root = ensure_within(self.root, self.root / "staging")
        if staging_root.is_dir():
            for orphan in staging_root.glob(f"{identity}-*"):
                with contextlib.suppress(OSError, ValidationError):
                    guarded_remove(staging_root, orphan)

    def _existing_artifact(self, repository_id: str, identity: str) -> bool:
        directory = self._artifact_dir(repository_id, identity)
        return directory.is_dir() and (directory / "source").is_dir()

    def _artifact_dir(self, repository_id: str, identity: str) -> Path:
        _validate_identifier(identity, "artifact")
        repository_root = self._repository_root(repository_id)
        return ensure_within(repository_root, repository_root / "artifacts" / identity)

    def _repository_root(self, repository_id: str) -> Path:
        _validate_identifier(repository_id, "repository")
        repos_root = ensure_within(self.root, self.root / "repos")
        return ensure_within(repos_root, repos_root / repository_id)

    def _lock_path(self, repository_id: str, identity: str) -> Path:
        _validate_identifier(repository_id, "repository")
        _validate_identifier(identity, "artifact")
        return self.root / "locks" / f"{repository_id}--{identity}.lock"

    def _repository_lock_path(self, repository_id: str) -> Path:
        _validate_identifier(repository_id, "repository")
        return self.root / "locks" / f"{repository_id}--repository.lock"


def safe_extract_tar(archive_path: Path, destination: Path) -> None:
    """Extract a tar archive after validating every member and total expansion."""
    destination_root = destination.resolve(strict=False)
    total_size = 0
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValidationError("Archive contains too many members.")
            for member in members:
                member_path = PurePosixPath(member.name)
                if (
                    not member.name
                    or member_path.is_absolute()
                    or ".." in member_path.parts
                    or "\\" in member.name
                    or "\0" in member.name
                    or any(":" in part for part in member_path.parts)
                    or len(member.name) > MAX_MEMBER_PATH_LENGTH
                    or member.isdev()
                    or member.isfifo()
                ):
                    raise ValidationError(f"Unsafe archive member: {member.name!r}")
                if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                    raise ValidationError(f"Unsupported archive member: {member.name!r}")
                if member.issym() or member.islnk():
                    _validated_link_parts(member)
                total_size += max(member.size, 0)
                if total_size > MAX_EXPANDED_BYTES:
                    raise ValidationError("Archive expanded size exceeds the safety limit.")
                candidate = (destination_root / Path(*member_path.parts)).resolve(strict=False)
                try:
                    candidate.relative_to(destination_root)
                except ValueError as exc:
                    raise ValidationError(f"Archive member escapes extraction root: {member.name!r}") from exc
            for member in members:
                if member.issym() or member.islnk():
                    continue
                target = destination_root / Path(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValidationError(f"Archive member has no readable content: {member.name!r}")
                with source, target.open("xb") as handle:
                    shutil.copyfileobj(source, handle, length=1024 * 1024)
                os.chmod(target, member.mode & 0o777 & ~0o6000)
            for member in members:
                if not (member.issym() or member.islnk()):
                    continue
                target = destination_root / Path(*PurePosixPath(member.name).parts)
                linked = destination_root / Path(*_validated_link_parts(member))
                if not linked.exists():
                    raise ValidationError(
                        f"Archive link target is missing: {member.name!r} -> {member.linkname!r}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.islnk():
                    os.link(linked, target)
                elif os.name != "nt":
                    os.symlink(member.linkname, target, target_is_directory=linked.is_dir())
                elif linked.is_dir():
                    linked_bytes, _ = directory_size(linked)
                    total_size += linked_bytes
                    if total_size > MAX_EXPANDED_BYTES:
                        raise ValidationError("Archive link expansion exceeds the safety limit.")
                    shutil.copytree(linked, target)
                else:
                    total_size += linked.stat().st_size
                    if total_size > MAX_EXPANDED_BYTES:
                        raise ValidationError("Archive link expansion exceeds the safety limit.")
                    shutil.copy2(linked, target)
    except (tarfile.TarError, OSError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ProviderError(f"Unable to extract source archive: {exc}") from exc


def validate_managed_source_tree(source_root: Path) -> None:
    """Reject repository symlinks whose lexical targets escape the managed source root."""
    root = source_root.resolve(strict=False)
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*dirnames, *filenames]:
            candidate = current_path / name
            if is_windows_reparse_point(candidate):
                raise ValidationError(
                    f"Managed source cannot contain a Windows reparse point: {candidate}"
                )
            if not candidate.is_symlink():
                status = candidate.stat(follow_symlinks=False)
                if not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)):
                    raise ValidationError(f"Managed source contains a special file: {candidate}")
                continue
            try:
                target = candidate.resolve(strict=False)
                target.relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValidationError(f"Managed source symlink escapes its root: {candidate}") from exc
        dirnames[:] = [name for name in dirnames if not is_link_or_reparse(current_path / name)]


def source_tree_digest(
    source_root: Path, *, excluded_top_level: frozenset[str] = frozenset()
) -> str:
    """Hash relative paths, safe symlink targets, modes, and file contents deterministically."""
    root = source_root.resolve(strict=False)
    digest = hashlib.sha256()
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        if current_path == root and excluded_top_level:
            dirnames[:] = [name for name in dirnames if name not in excluded_top_level]
            filenames = [name for name in filenames if name not in excluded_top_level]
        dirnames.sort()
        filenames.sort()
        entries = [*dirnames, *filenames]
        for name in entries:
            candidate = current_path / name
            relative = candidate.relative_to(root).as_posix()
            _hash_field(digest, relative.encode("utf-8", errors="surrogateescape"))
            if is_windows_reparse_point(candidate):
                raise ValidationError(
                    f"Managed source cannot contain a Windows reparse point: {candidate}"
                )
            if candidate.is_symlink():
                digest.update(b"L")
                _hash_field(
                    digest,
                    os.readlink(candidate).encode("utf-8", errors="surrogateescape"),
                )
                digest.update(b"\xff")
                continue
            status = candidate.stat(follow_symlinks=False)
            if stat.S_ISREG(status.st_mode):
                entry_type = b"F"
            elif stat.S_ISDIR(status.st_mode):
                entry_type = b"D"
            else:
                raise ValidationError(f"Managed source contains a special file: {candidate}")
            digest.update(entry_type)
            digest.update((status.st_mode & 0o777).to_bytes(2, "big"))
            if entry_type == b"F":
                digest.update(status.st_size.to_bytes(8, "big"))
                with candidate.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
            digest.update(b"\xff")
        dirnames[:] = [name for name in dirnames if not is_link_or_reparse(current_path / name)]
    return f"sha256:{digest.hexdigest()}"


def _hash_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _select_archive_root(expanded: Path) -> Path:
    children = list(expanded.iterdir())
    if len(children) == 1 and children[0].is_dir() and not is_link_or_reparse(children[0]):
        return children[0]
    if not children:
        raise ProviderError("Source archive is empty.")
    synthetic = expanded.parent / "selected-source"
    synthetic.mkdir()
    for child in children:
        child.rename(synthetic / child.name)
    return synthetic


def _validated_link_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    """Resolve an archive link lexically and reject any root escape."""
    link = PurePosixPath(member.linkname)
    if (
        not member.linkname
        or link.is_absolute()
        or "\\" in member.linkname
        or "\0" in member.linkname
        or any(":" in part for part in link.parts)
    ):
        raise ValidationError(
            f"Unsafe archive link: {member.name!r} -> {member.linkname!r}"
        )
    base = [] if member.islnk() else list(PurePosixPath(member.name).parent.parts)
    for part in link.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not base:
                raise ValidationError(
                    f"Archive link escapes extraction root: {member.name!r} -> {member.linkname!r}"
                )
            base.pop()
        else:
            base.append(part)
    if not base:
        raise ValidationError(
            f"Archive link cannot target the extraction root: {member.name!r}"
        )
    return tuple(base)


def _load_artifact_manifest(path: Path) -> dict[str, Any]:
    try:
        if is_link_or_reparse(path):
            raise ValueError("manifest cannot be a link or reparse point")
        status = path.stat(follow_symlinks=False)
        if not path.is_file() or status.st_nlink != 1:
            raise ValueError("manifest must be a single regular file")
        if status.st_size > MAX_ARTIFACT_MANIFEST_BYTES:
            raise ValueError("manifest exceeds the 1 MiB safety limit")
        with path.open("rb") as handle:
            content = handle.read(MAX_ARTIFACT_MANIFEST_BYTES + 1)
        if len(content) > MAX_ARTIFACT_MANIFEST_BYTES:
            raise ValueError("manifest exceeds the 1 MiB safety limit")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("manifest root must be an object")
        return payload
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderError("Managed artifact manifest is missing or invalid.") from exc


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _commits_match(actual: str, expected: str) -> bool:
    actual_lower = actual.casefold()
    expected_lower = expected.casefold()
    return actual_lower.startswith(expected_lower) or expected_lower.startswith(actual_lower)


def _validate_identifier(value: str, label: str) -> None:
    if (
        not value
        or len(value) > 180
        or not all(character.isalnum() or character in "-_" for character in value)
    ):
        raise ValidationError(f"Invalid managed {label} identifier.")
