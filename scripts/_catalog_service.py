"""Use-case orchestration for the global dependency source catalog."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

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
        """Record that another process currently owns this artifact lock."""
        self.store.update_operation(
            self.operation_id,
            status=OperationStatus.WAITING_LOCK,
            phase="artifact_lock",
            message="Waiting for the same source artifact in another process.",
        )

    def acquired_lock(self) -> None:
        """Return a waiting operation to running after it obtains the artifact lock."""
        self.store.update_operation(
            self.operation_id,
            status=OperationStatus.RUNNING,
            phase="source_acquisition",
            message="Artifact lock acquired; source acquisition resumed.",
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

    def __init__(self, root: str | Path) -> None:
        self.store = CatalogStore(Path(root).expanduser())
        self.root = self.store.root
        self.store.initialize()
        self.recovered_operations = self.store.recover_stale_operations()
        self.artifacts = ArtifactManager(self.root)
        self.staging_cleanup = cleanup_stale_staging(self.root)

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
            repository = self.store.add_repository(
                repository_id=stable_id("repo", f"github\0{canonical.casefold()}"),
                provider="github",
                canonical_name=canonical,
                display_name=metadata["display_name"],
                remote_url=metadata["remote_url"],
                origin_kind="github",
                aliases=tuple(aliases),
            )
            self._write_stub_manifest(repository)
            operation.complete(f"Registered GitHub repository {canonical}.")
            return self.store.repository(repository["id"])

    def add_git(self, remote_url: str, *, aliases: Iterable[str] = ()) -> dict[str, Any]:
        """Register a generic Git remote without requiring GitHub-specific tooling."""
        remote = validate_git_remote(remote_url)
        repository_id, canonical, display_name = _remote_repository_identity(remote)
        with OperationTracker(self.store, "repo add-git", target=remote) as operation:
            repository = self.store.add_repository(
                repository_id=repository_id,
                provider="git",
                canonical_name=canonical,
                display_name=display_name,
                remote_url=remote,
                origin_kind="git",
                aliases=tuple(aliases),
            )
            self._write_stub_manifest(repository)
            operation.complete(f"Registered Git repository {canonical}.")
            return self.store.repository(repository["id"])

    def add_local(self, source_path: str | Path, *, aliases: Iterable[str] = ()) -> dict[str, Any]:
        """Register and verify one external user-owned source tree."""
        metadata = inspect_local_source(source_path)
        with OperationTracker(
            self.store, "repo add-local", target=metadata["canonical_path"]
        ) as operation:
            existing = None
            remote = metadata.get("remote_url")
            if remote:
                existing = self.store.repository_by_remote(remote)
            if existing:
                repository = existing
                self.store.add_aliases(repository["id"], tuple(aliases))
            else:
                if remote:
                    repository_id, canonical, display_name = _remote_repository_identity(remote)
                else:
                    repository_id = stable_id(
                        "repo", f"local\0{_path_identity(metadata['canonical_path'])}"
                    )
                    canonical = _local_canonical_name(metadata)
                    display_name = metadata["display_name"]
                repository = self.store.add_repository(
                    repository_id=repository_id,
                    provider="local",
                    canonical_name=canonical,
                    display_name=display_name,
                    remote_url=remote,
                    origin_kind="local",
                    aliases=tuple(aliases),
                )
            local_identity = stable_id("local", metadata["canonical_path"])
            source_record = {
                "id": local_identity,
                **metadata,
                "exists": True,
                "added_at": utc_now(),
            }
            clean_commit = metadata.get("commit_sha") if metadata.get("dirty") is False else None
            resolution_kind = ResolutionKind.EXACT_COMMIT if clean_commit else ResolutionKind.UNRESOLVED
            path_suffix = hashlib.sha256(metadata["canonical_path"].encode("utf-8")).hexdigest()[:10]
            source_ref = f"{clean_commit or metadata.get('detected_version') or 'working-tree'}@local-{path_suffix}"
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
                    VerificationState.VERIFIED if clean_commit else VerificationState.UNVERIFIED
                ),
                "acquired_at": utc_now(),
                "verified_at": utc_now(),
                "last_accessed_at": utc_now(),
                "source_bytes": None,
                "archive_bytes": 0,
                "file_count": None,
                "external": True,
            }
            self.store.upsert_local_source_artifact(repository["id"], source_record, artifact)
            repository = self.store.repository(repository["id"])
            self._write_stub_manifest(repository)
            operation.complete(
                f"Registered local source {metadata['canonical_path']}.", artifact_identity=identity
            )
            return self.store.repository(repository["id"])

    def scan_local(
        self,
        base_path: str | Path,
        *,
        max_depth: int,
        update_existing: bool,
    ) -> dict[str, Any]:
        """Discover and register source roots beneath one explicit absolute directory."""
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
            artifact = self._acquire(
                repository,
                resolved,
                force=force,
                nested_operation=False,
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
            operation.complete(
                f"Resolved {metadata.package_id} {metadata.version} to local source with "
                f"{resolved.resolution_kind.value} provenance.",
                artifact_identity=artifact["id"],
            )
        return self.resolve(package_id, ref=metadata.version)

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

    def remove(self, query: str, *, purge_managed_cache: bool, yes: bool) -> dict[str, Any]:
        """Remove catalog metadata and optionally its guarded managed cache."""
        repository = self.store.resolve_repository(query)
        if purge_managed_cache and not yes:
            raise ValidationError("--purge-managed-cache requires --yes.")
        with OperationTracker(
            self.store,
            "repo remove",
            repository_id=repository["id"],
            target=repository["canonical_name"],
        ) as operation:
            if purge_managed_cache:
                self.artifacts.purge_repository(repository["id"])
            self.store.delete_repository(repository["id"])
            operation.complete(f"Removed repository {repository['canonical_name']} from the catalog.")
        return {
            "removed": repository["canonical_name"],
            "purged_managed_cache": purge_managed_cache,
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
        wait_callback=None,
        resume_callback=None,
    ) -> dict[str, Any]:
        remote = repository.get("remote_url")
        if not remote:
            raise CapabilityError("Repository has no remote source provider.")
        expected_identity = artifact_id(
            repository["id"],
            "github_archive" if github_name_from_remote(remote) and repository["provider"] != "git" else "git_clone",
            resolved.resolved_ref,
        )

        def acquire(operation: OperationTracker | None) -> dict[str, Any]:
            on_wait = wait_callback or (operation.waiting_for_lock if operation else None)
            on_acquired = resume_callback or (operation.acquired_lock if operation else None)
            persisted: dict[str, Any] | None = None

            def persist_validated(artifact: dict[str, Any]) -> None:
                nonlocal persisted
                artifact["detected_version"] = detect_version(Path(artifact["source_path"]))
                self.store.upsert_artifact(repository["id"], artifact)
                persisted = artifact

            github_name = github_name_from_remote(remote) if repository["provider"] != "git" else None
            if github_name:
                artifact = self.artifacts.acquire_github(
                    repository_id=repository["id"],
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
                    repository_id=repository["id"],
                    remote_url=remote,
                    resolved=resolved,
                    force=force,
                    on_wait=on_wait,
                    on_acquired=on_acquired,
                    persist_validated=persist_validated,
                    load_trusted=lambda: self.store.artifact(expected_identity),
                )
            if persisted is None:
                raise ProviderError("Validated artifact was not persisted transactionally.")
            self._write_stub_manifest(self.store.repository(repository["id"]))
            return artifact

        if not nested_operation:
            return acquire(None)
        with OperationTracker(
            self.store,
            "repo fetch",
            repository_id=repository["id"],
            artifact_identity=expected_identity,
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
            self.store.add_aliases(existing["id"], tuple(aliases))
            return self.store.repository(existing["id"])
        metadata = GitHubClient().repository(canonical)
        return self.store.add_repository(
            repository_id=stable_id("repo", f"github\0{canonical.casefold()}"),
            provider="github",
            canonical_name=metadata["full_name"],
            display_name=metadata["display_name"],
            remote_url=metadata["remote_url"],
            origin_kind="github",
            aliases=tuple(aliases),
        )

    def _ensure_git_repository(
        self,
        remote: str,
        *,
        aliases: Iterable[str],
        allow_file: bool = True,
    ) -> dict[str, Any]:
        clean = validate_git_remote(remote, allow_file=allow_file)
        existing = self.store.repository_by_remote(clean)
        if existing:
            self.store.add_aliases(existing["id"], tuple(aliases))
            return self.store.repository(existing["id"])
        canonical, display_name = _generic_repository_identity(clean)
        return self.store.add_repository(
            repository_id=stable_id("repo", f"git\0{normalized_git_remote_identity(clean)}"),
            provider="git",
            canonical_name=canonical,
            display_name=display_name,
            remote_url=clean,
            origin_kind="git",
            aliases=tuple(aliases),
        )

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
