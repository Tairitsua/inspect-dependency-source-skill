"""Use-case orchestration for the global dependency source catalog."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Iterator

from _catalog_artifacts import ArtifactManager
from _catalog_dashboard import dashboard_status, start_dashboard, stop_dashboard
from _catalog_models import (
    APP_VERSION,
    ArtifactStatus,
    CapabilityError,
    CatalogError,
    NotFoundError,
    OperationStatus,
    ProviderError,
    RefNotFoundError,
    ResolutionKind,
    ResolvedRef,
    SourceUnavailableError,
    ValidationError,
    VerificationState,
)
from _catalog_paths import (
    artifact_id,
    atomic_write_json,
    cleanup_stale_staging,
    ensure_within,
    is_link_or_reparse,
    sanitize_remote,
    stable_id,
)
from _catalog_providers import (
    GitHubClient,
    NuGetClient,
    choose_nuget_ref,
    detect_version,
    generic_default_branch,
    generic_tags,
    github_name_from_remote,
    inspect_local_source,
    normalized_git_remote_identity,
    nuget_tag_candidates,
    resolve_generic_ref,
    scan_local_roots,
    validate_git_remote,
    validate_github_name,
    validate_ref,
)
from _catalog_store import CatalogStore, utc_now


class OperationTracker:
    """Persist a mutation's authoritative operation lifecycle and checkpoints."""

    def __init__(
        self,
        store: CatalogStore,
        command: str,
        *,
        target: str | None = None,
        repository_id: str | None = None,
        artifact_identity: str | None = None,
    ) -> None:
        self.store = store
        self.operation_id = store.create_operation(
            command,
            target=target,
            repository_id=repository_id,
            artifact_id=artifact_identity,
        )
        self.finished = False

    def __enter__(self) -> "OperationTracker":
        self.store.update_operation(
            self.operation_id,
            status=OperationStatus.RUNNING,
            phase="running",
            message="Operation started.",
        )
        return self

    def checkpoint(self, phase: str, message: str) -> None:
        """Record a factual, non-inferred operation checkpoint."""
        self.store.update_operation(
            self.operation_id,
            status=OperationStatus.RUNNING,
            phase=phase,
            message=message,
        )

    def waiting_for_lock(self) -> None:
        """Record that another process owns a source-acquisition coordination lock."""
        self.store.update_operation(
            self.operation_id,
            status=OperationStatus.WAITING_LOCK,
            phase="source_lock",
            message="Waiting for repository source work in another process.",
        )

    def acquired_lock(self) -> None:
        """Return a waiting operation to running after it obtains the source lock."""
        self.store.update_operation(
            self.operation_id,
            status=OperationStatus.RUNNING,
            phase="source_acquisition",
            message="Source acquisition lock acquired; work resumed.",
        )

    def complete(self, message: str, *, artifact_identity: str | None = None) -> None:
        """Complete the operation exactly once."""
        self.store.update_operation(
            self.operation_id,
            status=OperationStatus.COMPLETED,
            phase="completed",
            message=message,
            artifact_id=artifact_identity,
        )
        self.finished = True

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc is None:
            if not self.finished:
                self.complete("Operation completed.")
            return False
        interrupted = isinstance(exc, KeyboardInterrupt)
        if isinstance(exc, CatalogError):
            code = exc.code
        elif interrupted:
            code = "interrupted"
        else:
            code = "internal_error"
        with contextlib.suppress(Exception):
            self.store.update_operation(
                self.operation_id,
                status=OperationStatus.INTERRUPTED if interrupted else OperationStatus.FAILED,
                phase="interrupted" if interrupted else "failed",
                message="Operation interrupted." if interrupted else "Operation failed.",
                error_code=code,
                error_message=str(exc),
            )
        return False


class CatalogService:
    """Expose provider-independent catalog workflows to CLI and tests."""

    def __init__(
        self,
        root: str | Path,
        *,
        run_startup_maintenance: bool = True,
    ) -> None:
        self.store = CatalogStore(Path(root).expanduser())
        self.root = self.store.root
        self.store.initialize()
        self.artifacts = ArtifactManager(self.root)
        if run_startup_maintenance:
            self.recovered_operations = self.store.recover_stale_operations()
            self.staging_cleanup = cleanup_stale_staging(self.root)
        else:
            self.recovered_operations = 0
            self.staging_cleanup = {"removed": 0, "busy": 0, "skipped": 0}

    def initialize(self, *, dashboard: bool = True) -> dict[str, Any]:
        """Initialize the empty global catalog and optionally reuse/start its dashboard."""
        dashboard_result = start_dashboard(self.root) if dashboard else None
        return {
            "catalog_root": str(self.root),
            "database": str(self.store.database_path),
            "recovered_operations": self.recovered_operations,
            "stale_staging": self.staging_cleanup,
            "dashboard": dashboard_result,
        }

    def add_github(self, full_name: str, *, aliases: Iterable[str] = ()) -> dict[str, Any]:
        """Register one GitHub repository using direct provider metadata."""
        requested_name = validate_github_name(full_name)
        with OperationTracker(self.store, "repo add-github", target=requested_name) as operation:
            operation.checkpoint("provider_metadata", "Reading GitHub repository metadata.")
            metadata = GitHubClient().repository(requested_name)
            canonical = metadata["full_name"]
            repository_id = stable_id("repo", f"github\0{canonical.casefold()}")
            with self._registration_guard(
                repository_id, canonical, metadata["remote_url"]
            ) as locked_repository_id:
                repository = self.store.add_repository(
                    repository_id=repository_id,
                    provider="github",
                    canonical_name=canonical,
                    display_name=metadata["display_name"],
                    remote_url=metadata["remote_url"],
                    origin_kind="github",
                    aliases=tuple(aliases),
                )
                _require_locked_repository(repository, locked_repository_id)
                self._write_stub_manifest(repository)
                result = self.store.repository(repository["id"])
            operation.complete(f"Registered GitHub repository {canonical}.")
            return result

    def add_git(self, remote_url: str, *, aliases: Iterable[str] = ()) -> dict[str, Any]:
        """Register a generic Git remote without requiring GitHub-specific tooling."""
        remote = validate_git_remote(remote_url)
        repository_id, canonical, display_name = _remote_repository_identity(remote)
        with OperationTracker(self.store, "repo add-git", target=remote) as operation:
            with self._registration_guard(
                repository_id, canonical, remote
            ) as locked_repository_id:
                repository = self.store.add_repository(
                    repository_id=repository_id,
                    provider="git",
                    canonical_name=canonical,
                    display_name=display_name,
                    remote_url=remote,
                    origin_kind="git",
                    aliases=tuple(aliases),
                )
                _require_locked_repository(repository, locked_repository_id)
                self._write_stub_manifest(repository)
                result = self.store.repository(repository["id"])
            operation.complete(f"Registered Git repository {canonical}.")
            return result

    def add_local(self, source_path: str | Path, *, aliases: Iterable[str] = ()) -> dict[str, Any]:
        """Register and verify one external user-owned source tree."""
        metadata = inspect_local_source(source_path)
        canonical_source = Path(metadata["canonical_path"])
        if _path_is_within(self.root, canonical_source) or _path_is_within(
            canonical_source, self.root
        ):
            raise ValidationError(
                "Registered local source must not overlap the selected catalog root."
            )
        remote = metadata.get("remote_url")
        existing = self.store.repository_by_remote(remote) if remote else None
        if existing:
            repository_id = existing["id"]
            canonical = existing["canonical_name"]
            display_name = existing["display_name"]
        elif remote:
            repository_id, canonical, display_name = _remote_repository_identity(remote)
        else:
            repository_id = stable_id(
                "repo", f"local\0{_path_identity(metadata['canonical_path'])}"
            )
            canonical = _local_canonical_name(metadata)
            display_name = metadata["display_name"]
        with OperationTracker(
            self.store, "repo add-local", target=metadata["canonical_path"]
        ) as operation:
            with self._registration_guard(
                repository_id, canonical, remote
            ) as locked_repository_id:
                existing = self.store.repository_by_remote(remote) if remote else None
                if existing:
                    repository = existing
                    self.store.add_aliases(repository["id"], tuple(aliases))
                else:
                    repository = self.store.add_repository(
                        repository_id=repository_id,
                        provider="local",
                        canonical_name=canonical,
                        display_name=display_name,
                        remote_url=remote,
                        origin_kind="local",
                        aliases=tuple(aliases),
                    )
                _require_locked_repository(repository, locked_repository_id)
                local_identity = stable_id("local", metadata["canonical_path"])
                source_record = {
                    "id": local_identity,
                    **metadata,
                    "exists": True,
                    "added_at": utc_now(),
                }
                clean_commit = (
                    metadata.get("commit_sha") if metadata.get("dirty") is False else None
                )
                resolution_kind = (
                    ResolutionKind.EXACT_COMMIT
                    if clean_commit
                    else ResolutionKind.UNRESOLVED
                )
                path_suffix = hashlib.sha256(
                    metadata["canonical_path"].encode("utf-8")
                ).hexdigest()[:10]
                source_ref = (
                    f"{clean_commit or metadata.get('detected_version') or 'working-tree'}"
                    f"@local-{path_suffix}"
                )
                identity = artifact_id(repository["id"], "local", metadata["canonical_path"])
                artifact = {
                    "id": identity,
                    "kind": "local",
                    "ref": source_ref,
                    "detected_version": metadata.get("detected_version"),
                    "source_path": metadata["canonical_path"],
                    "archive_path": None,
                    "status": ArtifactStatus.READY,
                    "resolution_kind": resolution_kind,
                    "expected_commit": clean_commit,
                    "actual_commit": metadata.get("commit_sha"),
                    "verification_state": (
                        VerificationState.VERIFIED
                        if clean_commit
                        else VerificationState.UNVERIFIED
                    ),
                    "acquired_at": utc_now(),
                    "verified_at": utc_now(),
                    "last_accessed_at": utc_now(),
                    "source_bytes": None,
                    "archive_bytes": 0,
                    "file_count": None,
                    "external": True,
                }
                self.store.upsert_local_source_artifact(
                    repository["id"], source_record, artifact
                )
                repository = self.store.repository(repository["id"])
                self._write_stub_manifest(repository)
                result = self.store.repository(repository["id"])
            operation.complete(
                f"Registered local source {metadata['canonical_path']}.", artifact_identity=identity
            )
            return result

    def scan_local(
        self,
        base_path: str | Path,
        *,
        max_depth: int,
        update_existing: bool,
    ) -> dict[str, Any]:
        """Discover and register source roots beneath one explicit absolute directory."""
        canonical_base = Path(base_path).expanduser().resolve(strict=False)
        if _path_is_within(self.root, canonical_base):
            raise ValidationError("Local scan root must not be inside the selected catalog root.")
        roots = scan_local_roots(base_path, max_depth=max_depth, excluded=(self.root,))
        added: list[dict[str, Any]] = []
        skipped: list[str] = []
        failed: list[dict[str, str]] = []
        for root in roots:
            canonical = str(root.resolve(strict=False))
            if self.store.local_source_by_path(canonical) and not update_existing:
                skipped.append(canonical)
                continue
            try:
                added.append(self.add_local(root))
            except CatalogError as exc:
                failed.append({"path": canonical, "code": exc.code, "message": str(exc)})
        return {
            "base_path": str(Path(base_path).expanduser().resolve(strict=False)),
            "discovered_count": len(roots),
            "registered_count": len(added),
            "skipped": skipped,
            "failures": failed,
            "repositories": added,
        }

    def refresh_repository(self, query: str) -> dict[str, Any]:
        """Refresh provider tags for one repository."""
        repository = self.store.resolve_repository(query)
        with OperationTracker(
            self.store,
            "repo refresh",
            repository_id=repository["id"],
            target=repository["canonical_name"],
        ) as operation:
            operation.checkpoint("provider_tags", "Refreshing provider tag metadata.")
            tags = self._provider_tags(repository)
            self.store.replace_tags(repository["id"], tags)
            operation.complete(f"Refreshed {len(tags)} tags.")
        return self.store.repository(repository["id"])

    def refresh_all(self) -> dict[str, Any]:
        """Refresh all remotely backed repositories and retain per-repository failures."""
        refreshed: list[str] = []
        failures: list[dict[str, str]] = []
        for repository in self.store.repositories():
            if not repository.get("remote_url"):
                continue
            try:
                self.refresh_repository(repository["id"])
                refreshed.append(repository["canonical_name"])
            except CatalogError as exc:
                failures.append(
                    {
                        "repository": repository["canonical_name"],
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
        return {"refreshed": refreshed, "failures": failures}

    def repository_tags(
        self,
        query: str,
        *,
        match: str | None = None,
        limit: int = 100,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Return cached or freshly refreshed repository tags."""
        repository = self.store.resolve_repository(query)
        if refresh:
            self.refresh_repository(repository["id"])
        tags = self.store.tags(repository["id"])
        if match:
            needle = match.casefold()
            tags = [item for item in tags if needle in str(item["name"]).casefold()]
        return tags[: min(max(limit, 1), 5000)]

    def fetch(
        self,
        query: str,
        *,
        ref: str | None,
        default_branch: bool,
        force: bool,
    ) -> dict[str, Any]:
        """Fetch one exact requested ref or explicitly requested default branch."""
        if bool(ref) == bool(default_branch):
            raise ValidationError("Choose exactly one of --ref or --default-branch.")
        repository = self.store.resolve_repository(query)
        remote = repository.get("remote_url")
        if not remote:
            raise CapabilityError("This local-only repository has no remote source provider.")
        github_name = github_name_from_remote(remote) if repository["provider"] != "git" else None
        requested = validate_ref(ref) if ref else None
        if github_name:
            client = GitHubClient()
            selected = client.default_branch(github_name) if default_branch else requested
            assert selected is not None
            resolved = client.resolve_ref(github_name, selected)
            if default_branch:
                resolved = ResolvedRef(selected, selected, resolved.commit_sha, ResolutionKind.EXACT_COMMIT)
            else:
                resolved = ResolvedRef(
                    selected,
                    selected,
                    resolved.commit_sha,
                    ResolutionKind.EXACT_TAG if client.is_tag(github_name, selected) else ResolutionKind.EXACT_COMMIT,
                )
        else:
            selected = generic_default_branch(remote) if default_branch else requested
            assert selected is not None
            resolved = resolve_generic_ref(remote, selected)
        return self._acquire(repository, resolved, force=force)

    def fetch_nuget(
        self,
        package_id: str,
        version: str,
        *,
        aliases: Iterable[str] = (),
        force: bool,
    ) -> dict[str, Any]:
        """Resolve an exact NuGet package to source and persist package provenance."""
        with OperationTracker(
            self.store, "package fetch-nuget", target=f"{package_id} {version}"
        ) as operation:
            operation.checkpoint("package_metadata", "Reading exact NuGet package provenance.")
            metadata = NuGetClient().metadata(package_id, version)
            remote = metadata.repository_url or metadata.project_url
            if not remote:
                raise SourceUnavailableError(
                    f"NuGet package {package_id} {version} does not declare a source repository."
                )
            github_name = github_name_from_remote(remote)
            if github_name:
                repository = self._ensure_github_repository(
                    github_name, aliases=(package_id, *tuple(aliases))
                )
            else:
                repository = self._ensure_git_repository(
                    remote,
                    aliases=(package_id, *tuple(aliases)),
                    allow_file=False,
                )
            github_client = GitHubClient() if github_name else None
            resolved: ResolvedRef | None = None
            if not metadata.repository_commit:
                operation.checkpoint(
                    "provider_tags", "Resolving exact package-version tag candidates."
                )
                for candidate in nuget_tag_candidates(metadata):
                    if github_client and github_name:
                        if not github_client.is_tag(github_name, candidate):
                            continue
                        provider_ref = github_client.resolve_ref(github_name, candidate)
                    else:
                        try:
                            provider_ref = resolve_generic_ref(str(repository["remote_url"]), candidate)
                        except RefNotFoundError:
                            continue
                        if provider_ref.resolution_kind != ResolutionKind.EXACT_TAG:
                            continue
                    resolved = ResolvedRef(
                        metadata.version,
                        candidate,
                        provider_ref.commit_sha,
                        ResolutionKind.EXACT_TAG,
                    )
                    break
                if resolved is None:
                    operation.checkpoint(
                        "provider_tags", "Searching bounded repository tags for one heuristic match."
                    )
                    tags = self._provider_tags(repository, limit=5000)
                    self.store.replace_tags(repository["id"], tags)
                    resolved = choose_nuget_ref(metadata, tags)
            else:
                resolved = choose_nuget_ref(metadata, [])
            if github_client and github_name:
                provider_ref = github_client.resolve_ref(github_name, resolved.resolved_ref)
                full_commit = provider_ref.commit_sha
                if not full_commit:
                    raise ProviderError("GitHub did not return an exact commit identifier.")
                if resolved.commit_sha and not full_commit.startswith(resolved.commit_sha.casefold()):
                    raise ProviderError(
                        "GitHub resolved the NuGet-declared revision to a different commit."
                    )
                resolved = ResolvedRef(
                    resolved.requested_ref,
                    resolved.resolved_ref,
                    full_commit,
                    resolved.resolution_kind,
                )
            operation.checkpoint("source_acquisition", "Acquiring the resolved repository source.")
            with self.artifacts.repository_guard(
                repository["id"],
                on_wait=operation.waiting_for_lock,
                on_acquired=operation.acquired_lock,
            ):
                repository = self.store.repository(repository["id"])
                artifact = self._acquire(
                    repository,
                    resolved,
                    force=force,
                    nested_operation=False,
                    repository_guarded=True,
                    wait_callback=operation.waiting_for_lock,
                    resume_callback=operation.acquired_lock,
                )
                self.store.add_aliases(repository["id"], (package_id, *tuple(aliases)))
                self.store.bind_package(
                    {
                        "ecosystem": "nuget",
                        "package_id": metadata.package_id,
                        "version": metadata.version,
                        "repository_id": repository["id"],
                        "artifact_id": artifact["id"],
                        "requested_ref": resolved.requested_ref,
                        "resolved_ref": resolved.resolved_ref,
                        "resolution_kind": resolved.resolution_kind,
                        "expected_commit": resolved.commit_sha,
                    }
                )
                result = self.resolve(package_id, ref=metadata.version)
                operation.complete(
                    f"Resolved {metadata.package_id} {metadata.version} to local source with "
                    f"{resolved.resolution_kind.value} provenance.",
                    artifact_identity=artifact["id"],
                )
        return result

    def resolve(self, query: str, *, ref: str | None = None) -> dict[str, Any]:
        """Return the stable integration contract for one available source artifact."""
        package_binding = self.store.package_binding(query, ref) if ref else None
        if package_binding:
            repository = self.store.repository(package_binding["repository_id"])
            artifact_identity = package_binding.get("artifact_id")
            if not artifact_identity:
                raise SourceUnavailableError(
                    f"Package {package_binding['package_id']} {package_binding['version']} "
                    "has no bound source artifact."
                )
            artifact = self.store.artifact(artifact_identity)
            if artifact and artifact.get("repository_id") != repository["id"]:
                raise SourceUnavailableError(
                    "Package binding source ownership does not match its repository."
                )
        else:
            repository = self.store.resolve_repository(query)
            artifact = self.store.resolve_artifact(repository["id"], ref=ref)
        if (
            artifact is None
            or artifact.get("status") != ArtifactStatus.READY
            or artifact.get("verification_state") == VerificationState.FAILED
        ):
            qualifier = f" at ref '{ref}'" if ref else ""
            raise SourceUnavailableError(
                f"Repository {repository['canonical_name']} has no ready source artifact{qualifier}."
            )
        artifact, health = self._refresh_artifact_health(artifact)
        if (
            health["status"] != ArtifactStatus.READY
            or health["verification_state"] == VerificationState.FAILED
            or (
                not artifact.get("external")
                and health["verification_state"] != VerificationState.VERIFIED
            )
        ):
            raise SourceUnavailableError(
                f"Resolved source artifact failed integrity verification: {artifact['id']}"
            )
        try:
            source = self.artifacts.validated_source_path(artifact)
        except (ProviderError, ValidationError):
            self.store.update_artifact_health(
                artifact["id"],
                status=ArtifactStatus.MISSING,
                verification_state=VerificationState.FAILED,
                source_bytes=None,
                archive_bytes=None,
                file_count=None,
                expected_generation=artifact["generation"],
            )
            raise SourceUnavailableError(
                f"Resolved source path is unavailable or unsafe: {artifact['source_path']}"
            )
        detail = self.store.repository(repository["id"])
        packages = [
            package
            for package in detail["packages"]
            if package.get("artifact_id") == artifact["id"]
        ]
        package = package_binding or (packages[0] if packages else None)
        return {
            "status": "ok",
            "source_path": str(source),
            "verification_state": artifact["verification_state"],
            "resolution_kind": artifact["resolution_kind"],
            "repository": {
                "id": repository["id"],
                "canonical_name": repository["canonical_name"],
                "display_name": repository["display_name"],
                "provider": repository["provider"],
                "remote_url": sanitize_remote(repository.get("remote_url")),
                "aliases": detail["aliases"],
            },
            "artifact": {
                "id": artifact["id"],
                "kind": artifact["kind"],
                "ref": artifact["ref"],
                "detected_version": artifact.get("detected_version"),
                "expected_commit": artifact.get("expected_commit"),
                "actual_commit": artifact.get("actual_commit"),
                "verification_state": artifact["verification_state"],
                "resolution_kind": artifact["resolution_kind"],
                "verified_at": artifact.get("verified_at"),
            },
            "package": (
                {
                    "ecosystem": package["ecosystem"],
                    "id": package["package_id"],
                    "version": package["version"],
                    "requested_ref": package.get("requested_ref"),
                    "resolved_ref": package.get("resolved_ref"),
                    "resolution_kind": package["resolution_kind"],
                    "expected_commit": package.get("expected_commit"),
                }
                if package
                else None
            ),
        }

    def verify(self, query: str | None = None) -> list[dict[str, Any]]:
        """Reconcile artifact paths and cached measurements for one or all repositories."""
        repository_id = self.store.resolve_repository(query)["id"] if query else None
        results: list[dict[str, Any]] = []
        for artifact in self.store.artifacts(repository_id):
            artifact, result = self._refresh_artifact_health(artifact)
            results.append(
                {
                    "artifact_id": artifact["id"],
                    "repository_id": artifact["repository_id"],
                    **{key: str(value) if hasattr(value, "value") else value for key, value in result.items()},
                }
            )
        self.store.reconcile_cached_metrics()
        return results

    def _refresh_artifact_health(
        self, artifact: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Verify and CAS-persist one current artifact, retrying one concurrent replacement."""
        current = artifact
        for _attempt in range(2):
            try:
                result = self.artifacts.verify_record(current)
            except (ProviderError, ValidationError, OSError, RuntimeError) as exc:
                replacement = self.store.artifact(current["id"])
                if (
                    replacement is not None
                    and replacement["generation"] != current["generation"]
                ):
                    current = replacement
                    continue
                source = Path(str(current["source_path"])).expanduser()
                self.store.update_artifact_health(
                    current["id"],
                    status=(
                        ArtifactStatus.MISSING
                        if not source.exists()
                        else ArtifactStatus.INVALID
                    ),
                    verification_state=VerificationState.FAILED,
                    source_bytes=None,
                    archive_bytes=None,
                    file_count=None,
                    actual_commit=None,
                    expected_generation=current["generation"],
                )
                replacement = self.store.artifact(current["id"])
                if (
                    replacement is not None
                    and replacement["generation"] != current["generation"]
                ):
                    current = replacement
                    continue
                raise SourceUnavailableError(
                    f"Source artifact could not be verified: {current['id']}"
                ) from exc
            if self.store.update_artifact_health(
                current["id"],
                **result,
                expected_generation=current["generation"],
            ):
                persisted = self.store.artifact(current["id"])
                if persisted is None:
                    break
                return persisted, result
            replacement = self.store.artifact(current["id"])
            if replacement is None:
                break
            current = replacement
        raise SourceUnavailableError(
            f"Source artifact changed concurrently during verification: {artifact['id']}"
        )

    def removal_plan(self, query: str) -> dict[str, Any]:
        """Preview one exact repository removal without mutating catalog state."""
        repository = self.store.resolve_repository(query)
        return self._build_removal_plan(repository)

    def _build_removal_plan(self, repository: dict[str, Any]) -> dict[str, Any]:
        detail = self.store.repository(repository["id"])
        tags = self.store.tags(repository["id"])
        managed_cache = self.artifacts.repository_cache_path(repository["id"])
        managed_artifacts: list[dict[str, Any]] = []
        preserved_by_path: dict[str, dict[str, Any]] = {}
        blocking_reasons: list[str] = []
        local_sources_excluded = True

        def preserve(path_value: Any, reason: str) -> None:
            nonlocal local_sources_excluded
            if not path_value:
                return
            path = str(path_value)
            preserved_path, classification_error = _classify_preserved_path(path)
            entry = preserved_by_path.setdefault(
                path,
                {
                    "path": path,
                    "exists": Path(path).exists(),
                    "path_classified": classification_error is None,
                    "reasons": [],
                },
            )
            if reason not in entry["reasons"]:
                entry["reasons"].append(reason)
            if classification_error is not None or preserved_path is None:
                entry["path_classified"] = False
                local_sources_excluded = False
                blocking_reasons.append(
                    "Preserved path cannot be safely classified: "
                    f"{path} ({classification_error})"
                )
                return
            if _path_is_within(managed_cache, preserved_path) or _path_is_within(
                preserved_path, managed_cache
            ):
                local_sources_excluded = False
                blocking_reasons.append(
                    f"Preserved path overlaps the managed purge target: {path}"
                )

        for local_source in detail["local_sources"]:
            preserve(
                local_source.get("canonical_path") or local_source.get("path"),
                "registered_local_source",
            )

        for artifact in detail["artifacts"]:
            if artifact["external"]:
                preserve(artifact.get("source_path"), "external_artifact")
                preserve(artifact.get("archive_path"), "external_artifact_archive")
                continue
            paths = [
                str(value)
                for value in (artifact.get("source_path"), artifact.get("archive_path"))
                if value
            ]
            inside_managed_cache = all(
                _path_is_within(managed_cache, Path(path)) for path in paths
            )
            if not inside_managed_cache:
                blocking_reasons.append(
                    f"Managed artifact points outside the purge target: {artifact['id']}"
                )
            managed_artifacts.append(
                {
                    "id": artifact["id"],
                    "kind": artifact["kind"],
                    "ref": artifact["ref"],
                    "source_path": artifact.get("source_path"),
                    "archive_path": artifact.get("archive_path"),
                    "inside_managed_cache": inside_managed_cache,
                }
            )

        plan = {
            "status": "ok",
            "operation": "repository_removal_plan",
            "repository": {
                "id": detail["id"],
                "canonical_name": detail["canonical_name"],
                "display_name": detail["display_name"],
                "provider": detail["provider"],
            },
            "metadata_removal": {
                "aliases": len(detail["aliases"]),
                "artifacts": len(detail["artifacts"]),
                "local_source_registrations": len(detail["local_sources"]),
                "package_bindings": len(detail["packages"]),
                "tags": len(tags),
                "deletion_set_digest": _removal_deletion_set_digest(detail, tags),
                "managed_cache_preserved": True,
            },
            "purge": {
                "requires_explicit_authorization": True,
                "required_flags": ["--purge-managed-cache", "--yes"],
                "managed_cache_path": str(managed_cache),
                "managed_cache_exists": managed_cache.exists(),
                "managed_artifacts": managed_artifacts,
                "preserved_local_sources": sorted(
                    preserved_by_path.values(), key=lambda item: item["path"]
                ),
                "local_sources_excluded": local_sources_excluded,
                "safe_to_purge": not blocking_reasons,
                "blocking_reasons": blocking_reasons,
            },
        }
        plan["plan_token"] = _removal_plan_token(plan)
        return plan

    def remove(
        self,
        query: str,
        *,
        purge_managed_cache: bool,
        yes: bool,
        plan_token: str | None = None,
    ) -> dict[str, Any]:
        """Remove catalog metadata and optionally its guarded managed cache."""
        repository = self.store.resolve_repository(query)
        if query != repository["id"]:
            raise ValidationError(
                "Repository removal requires the exact repository.id returned by "
                "repo remove <query> --plan --json."
            )
        if yes and not purge_managed_cache:
            raise ValidationError("--yes requires --purge-managed-cache.")
        if purge_managed_cache and not yes:
            raise ValidationError("--purge-managed-cache requires --yes.")
        if not plan_token:
            raise ValidationError(
                "Repository removal requires --plan-token from the latest removal preview."
            )
        with OperationTracker(
            self.store,
            "repo remove",
            repository_id=repository["id"],
            target=repository["canonical_name"],
        ) as operation:
            with self.artifacts.repository_guard(repository["id"]):
                with self.store.connect(write=True) as transaction:
                    plan = self._build_removal_plan(repository)
                    if not hmac.compare_digest(plan_token, plan["plan_token"]):
                        raise ValidationError(
                            "Repository removal plan changed; generate a new preview and "
                            "request authorization again."
                        )
                    if purge_managed_cache and not plan["purge"]["safe_to_purge"]:
                        raise ValidationError(
                            "Managed cache purge is blocked by an unsafe removal plan.",
                            details={
                                "blocking_reasons": plan["purge"]["blocking_reasons"]
                            },
                        )
                    if purge_managed_cache:
                        self.artifacts.purge_repository(repository["id"])
                    preserved_local_sources = _post_verify_preserved_paths(
                        plan["purge"]["preserved_local_sources"]
                    )
                    self.store.delete_repository(
                        repository["id"], connection=transaction
                    )
            operation.complete(f"Removed repository {repository['canonical_name']} from the catalog.")
        return {
            "removed": repository["canonical_name"],
            "repository_id": repository["id"],
            "purged_managed_cache": purge_managed_cache,
            "preserved_local_sources": preserved_local_sources,
        }

    def list_repositories(self, query: str | None = None) -> list[dict[str, Any]]:
        """Return all repository summaries or one resolved match."""
        return [self.store.resolve_repository(query)] if query else self.store.repositories()

    def status(self) -> dict[str, Any]:
        """Return catalog summary and verified dashboard lifecycle."""
        return {**self.store.summary(), "dashboard": dashboard_status(self.root)}

    def doctor(self) -> dict[str, Any]:
        """Check local runtime capabilities without forcing a network request."""
        checks: list[dict[str, Any]] = []
        checks.append(
            {
                "name": "python",
                "ok": sys.version_info >= (3, 11),
                "detail": sys.version.split()[0],
            }
        )
        checks.append(
            {"name": "sqlite", "ok": True, "detail": sqlite3.sqlite_version}
        )
        checks.append(
            {
                "name": "catalog_root",
                "ok": os.access(self.root, os.R_OK | os.W_OK | os.X_OK),
                "detail": str(self.root),
            }
        )
        checks.append(
            {
                "name": "git",
                "ok": shutil.which("git") is not None,
                "required": False,
                "detail": shutil.which("git") or "not installed (needed only for generic Git)",
            }
        )
        checks.append(
            {
                "name": "github_auth",
                "ok": bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or shutil.which("gh")),
                "required": False,
                "detail": "optional; public GitHub access works without gh",
            }
        )
        required = [check for check in checks if check.get("required", True)]
        return {
            "status": "ok" if all(check["ok"] for check in required) else "error",
            "catalog_root": str(self.root),
            "checks": checks,
        }

    def dashboard(self, action: str, *, port: int | None = None) -> dict[str, Any]:
        """Control the one process-verified localhost dashboard."""
        if action == "start":
            return start_dashboard(self.root, port=port)
        if action == "status":
            return dashboard_status(self.root)
        if action == "stop":
            return stop_dashboard(self.root)
        raise ValidationError(f"Unknown dashboard action: {action}")

    def _acquire(
        self,
        repository: dict[str, Any],
        resolved: ResolvedRef,
        *,
        force: bool,
        nested_operation: bool = True,
        repository_guarded: bool = False,
        wait_callback=None,
        resume_callback=None,
    ) -> dict[str, Any]:
        remote = repository.get("remote_url")
        if not remote:
            raise CapabilityError("Repository has no remote source provider.")

        def acquire(operation: OperationTracker | None) -> dict[str, Any]:
            on_wait = wait_callback or (operation.waiting_for_lock if operation else None)
            on_acquired = resume_callback or (operation.acquired_lock if operation else None)

            def acquire_locked() -> dict[str, Any]:
                current = self.store.repository(repository["id"])
                current_remote = current.get("remote_url")
                if not current_remote:
                    raise CapabilityError("Repository has no remote source provider.")
                github_name = (
                    github_name_from_remote(current_remote)
                    if current["provider"] != "git"
                    else None
                )
                expected_identity = artifact_id(
                    current["id"],
                    "github_archive" if github_name else "git_clone",
                    resolved.resolved_ref,
                )
                persisted: dict[str, Any] | None = None

                def persist_validated(artifact: dict[str, Any]) -> None:
                    nonlocal persisted
                    artifact["detected_version"] = detect_version(
                        Path(artifact["source_path"])
                    )
                    self.store.upsert_artifact(current["id"], artifact)
                    persisted = artifact

                if github_name:
                    artifact = self.artifacts.acquire_github(
                        repository_id=current["id"],
                        full_name=github_name,
                        resolved=resolved,
                        client=GitHubClient(),
                        force=force,
                        on_wait=on_wait,
                        on_acquired=on_acquired,
                        persist_validated=persist_validated,
                        load_trusted=lambda: self.store.artifact(expected_identity),
                    )
                else:
                    artifact = self.artifacts.acquire_git(
                        repository_id=current["id"],
                        remote_url=current_remote,
                        resolved=resolved,
                        force=force,
                        on_wait=on_wait,
                        on_acquired=on_acquired,
                        persist_validated=persist_validated,
                        load_trusted=lambda: self.store.artifact(expected_identity),
                    )
                if persisted is None:
                    raise ProviderError("Validated artifact was not persisted transactionally.")
                self._write_stub_manifest(self.store.repository(current["id"]))
                return artifact

            if repository_guarded:
                return acquire_locked()
            with self.artifacts.repository_guard(
                repository["id"], on_wait=on_wait, on_acquired=on_acquired
            ):
                return acquire_locked()

        if not nested_operation:
            return acquire(None)
        with OperationTracker(
            self.store,
            "repo fetch",
            repository_id=repository["id"],
            target=f"{repository['canonical_name']}@{resolved.resolved_ref}",
        ) as operation:
            operation.checkpoint("source_acquisition", "Acquiring exact repository source.")
            artifact = acquire(operation)
            operation.complete(
                f"Source is ready at {artifact['source_path']}.", artifact_identity=artifact["id"]
            )
            return artifact

    def _provider_tags(
        self, repository: dict[str, Any], *, limit: int = 5000
    ) -> list[dict[str, Any]]:
        remote = repository.get("remote_url")
        if not remote:
            raise CapabilityError("Repository has no remote for tag discovery.")
        github_name = github_name_from_remote(remote) if repository["provider"] != "git" else None
        if github_name:
            return GitHubClient().list_tags(github_name, limit=limit)
        return generic_tags(remote)[:limit]

    def _ensure_github_repository(
        self, full_name: str, *, aliases: Iterable[str]
    ) -> dict[str, Any]:
        canonical = validate_github_name(full_name)
        existing = self.store.repository_by_canonical_name(canonical)
        if existing:
            with self._registration_guard(
                existing["id"],
                existing["canonical_name"],
                existing.get("remote_url"),
            ) as locked_repository_id:
                current = self.store.repository(existing["id"])
                _require_locked_repository(current, locked_repository_id)
                self.store.add_aliases(current["id"], tuple(aliases))
                return self.store.repository(current["id"])
        metadata = GitHubClient().repository(canonical)
        resolved_canonical = metadata["full_name"]
        repository_id = stable_id(
            "repo", f"github\0{resolved_canonical.casefold()}"
        )
        with self._registration_guard(
            repository_id, resolved_canonical, metadata["remote_url"]
        ) as locked_repository_id:
            repository = self.store.add_repository(
                repository_id=repository_id,
                provider="github",
                canonical_name=resolved_canonical,
                display_name=metadata["display_name"],
                remote_url=metadata["remote_url"],
                origin_kind="github",
                aliases=tuple(aliases),
            )
            _require_locked_repository(repository, locked_repository_id)
            return repository

    def _ensure_git_repository(
        self,
        remote: str,
        *,
        aliases: Iterable[str],
        allow_file: bool = True,
    ) -> dict[str, Any]:
        clean = validate_git_remote(remote, allow_file=allow_file)
        repository_id, canonical, display_name = _remote_repository_identity(clean)
        with self._registration_guard(
            repository_id, canonical, clean
        ) as locked_repository_id:
            repository = self.store.add_repository(
                repository_id=repository_id,
                provider="git",
                canonical_name=canonical,
                display_name=display_name,
                remote_url=clean,
                origin_kind="git",
                aliases=tuple(aliases),
            )
            _require_locked_repository(repository, locked_repository_id)
            return repository

    @contextlib.contextmanager
    def _registration_guard(
        self,
        candidate_repository_id: str,
        canonical_name: str,
        remote_url: str | None,
    ) -> Iterator[str]:
        """Lock the actual existing identity, or the deterministic insertion identity."""
        for _attempt in range(3):
            existing = self._repository_by_registration_identity(
                canonical_name, remote_url
            )
            lock_id = existing["id"] if existing else candidate_repository_id
            retry = False
            with self.artifacts.repository_guard(lock_id):
                current = self._repository_by_registration_identity(
                    canonical_name, remote_url
                )
                if current is not None and current["id"] != lock_id:
                    retry = True
                elif current is None and lock_id != candidate_repository_id:
                    retry = True
                else:
                    yield lock_id
                    return
            if not retry:
                break
        raise SourceUnavailableError(
            "Repository identity changed repeatedly during guarded registration."
        )

    def _repository_by_registration_identity(
        self, canonical_name: str, remote_url: str | None
    ) -> dict[str, Any] | None:
        matches: dict[str, dict[str, Any]] = {}
        clean_remote = sanitize_remote(remote_url)
        if clean_remote:
            remote_match = self.store.repository_by_remote(clean_remote)
            if remote_match:
                matches[remote_match["id"]] = remote_match
        canonical_match = self.store.repository_by_canonical_name(canonical_name)
        if canonical_match:
            matches[canonical_match["id"]] = canonical_match
        if len(matches) > 1:
            raise ValidationError(
                "Repository canonical name and remote URL resolve to different catalog entries."
            )
        return next(iter(matches.values()), None)

    def _write_stub_manifest(self, repository: dict[str, Any]) -> None:
        repos_root = ensure_within(self.root, self.root / "repos")
        repo_root = ensure_within(repos_root, repos_root / repository["id"])
        repo_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "repository_id": repository["id"],
            "canonical_name": repository["canonical_name"],
            "display_name": repository["display_name"],
            "aliases": repository.get("aliases", []),
            "remote_url": sanitize_remote(repository.get("remote_url")),
            "summary": "Source catalog identity stub. Add manifest.json for enriched agent guidance.",
            "updated_at": utc_now(),
        }
        atomic_write_json(repo_root / "manifest.stub.json", payload)
        self.store.update_manifest_cache(
            repository["id"], status="stub", summary=payload["summary"]
        )


def _generic_repository_identity(remote: str) -> tuple[str, str]:
    github = github_name_from_remote(remote)
    if github:
        return f"git:{github}", github.rsplit("/", 1)[-1]
    parsed = urllib.parse.urlsplit(remote)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname.casefold()
        path = parsed.path.strip("/").removesuffix(".git")
    else:
        match = remote.split(":", 1)
        host = match[0].split("@")[-1].casefold()
        path = match[1].strip("/").removesuffix(".git") if len(match) == 2 else remote
    display = Path(path).name or "source"
    digest = hashlib.sha256(normalized_git_remote_identity(remote).encode("utf-8")).hexdigest()[:8]
    return f"git:{host}/{path}#{digest}", display


def _require_locked_repository(
    repository: dict[str, Any], locked_repository_id: str
) -> None:
    """Fail before filesystem work if registration resolved outside its guard."""
    if repository["id"] != locked_repository_id:
        raise SourceUnavailableError(
            "Repository identity changed during guarded registration."
        )


def _remote_repository_identity(remote: str) -> tuple[str, str, str]:
    """Return an order-independent repository identity for one sanitized remote."""
    github = github_name_from_remote(remote)
    if github:
        return (
            stable_id("repo", f"github\0{github.casefold()}"),
            github,
            github.rsplit("/", 1)[-1],
        )
    canonical, display_name = _generic_repository_identity(remote)
    return (
        stable_id("repo", f"git\0{normalized_git_remote_identity(remote)}"),
        canonical,
        display_name,
    )


def _local_canonical_name(metadata: dict[str, Any]) -> str:
    digest = hashlib.sha256(_path_identity(metadata["canonical_path"]).encode("utf-8")).hexdigest()[:10]
    return f"local:{metadata['display_name']}#{digest}"


def _path_identity(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _path_is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.expanduser().resolve(strict=False).relative_to(
            root.expanduser().resolve(strict=False)
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _classify_preserved_path(path_value: str) -> tuple[Path | None, str | None]:
    """Resolve one persisted external path without accepting links or reparse points."""
    expanded = Path(path_value).expanduser()
    if not expanded.is_absolute():
        return None, "path is not absolute"
    lexical = Path(os.path.abspath(expanded))
    try:
        current = Path(lexical.anchor)
        for part in lexical.parts[1:]:
            current = current / part
            if is_link_or_reparse(current):
                return None, f"path component is a link or reparse point: {current}"
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"path resolution failed: {type(exc).__name__}"
    if resolved != lexical:
        return None, "resolved path differs from its persisted canonical path"
    return lexical, None


def _post_verify_preserved_paths(
    preserved_paths: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Recheck every external path after managed deletion and fail on any loss."""
    verified: list[dict[str, Any]] = []
    failures: list[str] = []
    for item in preserved_paths:
        path = str(item["path"])
        classified, classification_error = _classify_preserved_path(path)
        exists_after = classified is not None and classified.exists()
        result = {**item, "exists_after": exists_after}
        verified.append(result)
        if item.get("exists") and (
            classification_error is not None or not exists_after
        ):
            failures.append(
                f"{path}: {classification_error or 'path no longer exists'}"
            )
    if failures:
        raise ValidationError(
            "Preserved local source post-verification failed.",
            details={"preservation_failures": failures},
        )
    return verified


def _removal_plan_token(plan: dict[str, Any]) -> str:
    """Bind authorization to the stable, user-visible removal projection."""
    payload = _canonicalize_token_value(
        {
            "contract": "repository-removal-plan-v1",
            "plan": plan,
        }
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _removal_deletion_set_digest(
    repository_detail: dict[str, Any], tags: list[dict[str, Any]]
) -> str:
    """Fingerprint stable identities and ownership paths deleted with a repository."""
    projection = {
        "contract": "repository-removal-deletion-set-v1",
        "repository": {
            key: repository_detail.get(key)
            for key in (
                "id",
                "canonical_name",
                "display_name",
                "provider",
                "remote_url",
                "origin_kind",
            )
        },
        "aliases": repository_detail.get("aliases", []),
        "artifacts": [
            {
                key: artifact.get(key)
                for key in (
                    "id",
                    "kind",
                    "ref",
                    "source_path",
                    "archive_path",
                    "external",
                )
            }
            for artifact in repository_detail.get("artifacts", [])
        ],
        "local_sources": [
            {
                key: source.get(key)
                for key in ("id", "path", "canonical_path")
            }
            for source in repository_detail.get("local_sources", [])
        ],
        "package_bindings": [
            {
                key: package.get(key)
                for key in (
                    "id",
                    "ecosystem",
                    "package_id",
                    "version",
                    "repository_id",
                    "artifact_id",
                    "requested_ref",
                    "resolved_ref",
                    "resolution_kind",
                    "expected_commit",
                )
            }
            for package in repository_detail.get("packages", [])
        ],
        "tags": [
            {"name": tag.get("name"), "commit_sha": tag.get("commit_sha")}
            for tag in tags
        ],
    }
    encoded = json.dumps(
        _canonicalize_token_value(projection),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonicalize_token_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonicalize_token_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        canonical = [_canonicalize_token_value(item) for item in value]
        return sorted(
            canonical,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    return value
