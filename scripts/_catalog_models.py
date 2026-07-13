"""Domain types and errors for the dependency source catalog."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = 1
APP_VERSION = "1.0.0"


class CatalogError(RuntimeError):
    """Base error with a stable machine-readable code and exit status."""

    code = "catalog_error"
    exit_code = 1

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details:
            payload.update(self.details)
        return payload


class ValidationError(CatalogError):
    """Raised when input or persisted state violates the catalog contract."""

    code = "validation_error"


class NotFoundError(CatalogError):
    """Raised when a repository or source cannot be resolved."""

    code = "not_found"
    exit_code = 2


class SourceUnavailableError(CatalogError):
    """Raised when metadata exists but no usable source tree is available."""

    code = "source_unavailable"
    exit_code = 2


class AmbiguousError(CatalogError):
    """Raised when a fuzzy query has no unique best match."""

    code = "ambiguous"
    exit_code = 3


class ProviderError(CatalogError):
    """Raised when a remote source provider cannot complete an operation."""

    code = "provider_error"


class RefNotFoundError(ProviderError):
    """Raised only when a provider confirms that a requested ref is missing."""

    code = "ref_not_found"
    exit_code = 2


class CapabilityError(CatalogError):
    """Raised when a command-specific external capability is unavailable."""

    code = "capability_unavailable"


class OperationStatus(StrEnum):
    """Authoritative lifecycle states for a persisted catalog operation."""

    QUEUED = "queued"
    WAITING_LOCK = "waiting_lock"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ArtifactStatus(StrEnum):
    """Availability states for one cached or external source artifact."""

    STAGING = "staging"
    READY = "ready"
    MISSING = "missing"
    INVALID = "invalid"


class VerificationState(StrEnum):
    """Verification strength for a source artifact."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"


class ResolutionKind(StrEnum):
    """How a requested version or ref was resolved to source."""

    EXACT_COMMIT = "exact_commit"
    EXACT_TAG = "exact_tag"
    HEURISTIC_TAG = "heuristic_tag"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ResolvedRef:
    """Provider resolution result for one requested source ref."""

    requested_ref: str
    resolved_ref: str
    commit_sha: str | None
    resolution_kind: ResolutionKind


@dataclass(frozen=True)
class NuGetMetadata:
    """Repository provenance declared by an exact NuGet package."""

    package_id: str
    version: str
    repository_url: str | None
    repository_commit: str | None
    project_url: str | None
