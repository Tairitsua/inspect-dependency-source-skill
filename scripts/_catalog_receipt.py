"""Privacy-conscious Markdown receipts for resolved dependency source evidence."""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


_MARKDOWN_DELIMITER_ENTITIES = str.maketrans(
    {
        "\\": "&#92;",
        "`": "&#96;",
        "*": "&#42;",
        "_": "&#95;",
        "[": "&#91;",
        "]": "&#93;",
        "|": "&#124;",
        "~": "&#126;",
    }
)
_OPAQUE_REPOSITORY_SUFFIX = re.compile(r"#[0-9a-f]{8,64}$", re.IGNORECASE)
_LOCAL_REF_SUFFIX = re.compile(r"@local-[0-9a-f]{8,64}$", re.IGNORECASE)
_FULL_GIT_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.IGNORECASE)


@dataclass(frozen=True)
class ResolveRequest:
    """The user-visible lookup that produced a resolution contract."""

    query: str
    ref: str | None = None


@dataclass(frozen=True)
class SourceReceipt:
    """A share-conscious projection of the stable resolution contract."""

    verdict: str
    verdict_label: str
    summary: str
    decision_label: str
    decision: str
    request: str
    repository: str
    provider: str
    selected_ref: str | None
    expected_commit: str | None
    observed_commit: str | None
    resolution_kind: str
    verification_state: str
    verified_at: str | None


def build_source_receipt(
    contract: Mapping[str, Any], request: ResolveRequest
) -> SourceReceipt:
    """Derive conservative receipt semantics without changing the JSON contract."""
    if _text(contract.get("status")) != "ok":
        raise ValueError("A source receipt requires a successful resolution contract.")

    repository = _mapping(contract.get("repository"))
    artifact = _mapping(contract.get("artifact"))
    package = _mapping(contract.get("package"))
    package_query_matches = _is_package_query(package, request)
    package_request_problem: str | None = None

    provider = _text(repository.get("provider")) or "not registered"
    repository_name = _public_repository_name(repository, provider)
    artifact_kind = _text(artifact.get("kind"))
    verification_state = (
        _text(artifact.get("verification_state"))
        or _text(contract.get("verification_state"))
        or "unknown"
    )

    if package_query_matches:
        ecosystem = _ecosystem_label(_text(package.get("ecosystem")))
        package_id = _text(package.get("id")) or request.query
        package_version = _text(package.get("version"))
        if request.ref is None:
            request_label = f"{ecosystem} · {package_id} (version not specified)"
            package_request_problem = "package_version_missing"
        else:
            request_label = f"{ecosystem} · {package_id} {request.ref}"
            if not package_version or package_version.casefold() != request.ref.casefold():
                package_request_problem = "package_version_mismatch"
        resolution_kind = _text(package.get("resolution_kind")) or "unknown"
        selected_ref = _public_artifact_ref(
            _text(package.get("resolved_ref")) or _text(artifact.get("ref")),
            artifact_kind,
        )
        expected_commit = _text(package.get("expected_commit")) or _text(
            artifact.get("expected_commit")
        )
    else:
        request_label = f"Repository · {repository_name}"
        request_ref = _public_artifact_ref(request.ref, artifact_kind)
        if request_ref:
            request_label += f" @ {request_ref}"
        resolution_kind = (
            _text(artifact.get("resolution_kind"))
            or _text(contract.get("resolution_kind"))
            or "unknown"
        )
        selected_ref = _public_artifact_ref(_text(artifact.get("ref")), artifact_kind)
        expected_commit = _text(artifact.get("expected_commit"))

    observed_commit = _text(artifact.get("actual_commit"))
    verdict, verdict_label, summary, decision_label, decision = _classify(
        resolution_kind=resolution_kind,
        verification_state=verification_state,
        expected_commit=expected_commit,
        observed_commit=observed_commit,
        request_problem=package_request_problem,
    )

    return SourceReceipt(
        verdict=verdict,
        verdict_label=verdict_label,
        summary=summary,
        decision_label=decision_label,
        decision=decision,
        request=request_label,
        repository=repository_name,
        provider=provider,
        selected_ref=selected_ref,
        expected_commit=expected_commit,
        observed_commit=observed_commit,
        resolution_kind=resolution_kind,
        verification_state=verification_state,
        verified_at=_text(artifact.get("verified_at")),
    )


def render_source_receipt(receipt: SourceReceipt) -> str:
    """Render deterministic Markdown without local paths or remote URLs."""
    short_commit = (
        receipt.observed_commit[:12] if receipt.observed_commit else "not available"
    )
    chain = " → ".join(
        (
            _code(receipt.request),
            _code(receipt.repository),
            _code(short_commit),
        )
    )
    rows = (
        ("Request", receipt.request),
        ("Repository", f"{receipt.repository} ({receipt.provider})"),
        ("Resolved ref", receipt.selected_ref),
        ("Expected commit", receipt.expected_commit),
        ("Observed commit", receipt.observed_commit),
        ("Provenance", receipt.resolution_kind),
        ("Integrity", receipt.verification_state),
        ("Verified at", receipt.verified_at),
        ("Local source", "available — absolute path withheld"),
    )
    table = "\n".join(f"| {label} | {_code(value)} |" for label, value in rows)
    return (
        f"## Dependency Source Receipt — {_code(receipt.request)}\n\n"
        f"> **{receipt.verdict} — {receipt.verdict_label}.** {receipt.summary}\n\n"
        f"{chain}\n\n"
        "| Evidence | Value |\n"
        "| --- | --- |\n"
        f"{table}\n\n"
        f"**{receipt.decision_label}:** {receipt.decision}\n\n"
        "_Privacy: absolute paths, remote URLs, aliases, catalog IDs, and path-derived "
        "identity suffixes are withheld. Review dependency and repository names before "
        "sharing._"
    )


def _classify(
    *,
    resolution_kind: str,
    verification_state: str,
    expected_commit: str | None,
    observed_commit: str | None,
    request_problem: str | None = None,
) -> tuple[str, str, str, str, str]:
    if request_problem == "package_version_missing":
        return (
            "BLOCKED",
            "package version not specified",
            "The package name matched cached evidence, but the request did not identify an "
            "exact package version.",
            "Decision",
            "Resolve the package again with --ref <exact-version> before making a "
            "version-specific claim.",
        )
    if request_problem == "package_version_mismatch":
        return (
            "BLOCKED",
            "package version mismatch",
            "The returned package binding does not match the exact version that was requested.",
            "Decision",
            "Treat the resolution as inconsistent and resolve the exact package version again.",
        )
    commits_agree = bool(
        expected_commit
        and observed_commit
        and _FULL_GIT_COMMIT.fullmatch(expected_commit)
        and _FULL_GIT_COMMIT.fullmatch(observed_commit)
        and expected_commit.casefold() == observed_commit.casefold()
    )
    verified = verification_state == "verified"

    if resolution_kind == "exact_commit" and verified and commits_agree:
        return (
            "PROVEN",
            "exact commit",
            "The request resolved to an immutable commit, and the cached source passed "
            "integrity verification at that commit.",
            "Claim boundary",
            "Confirm the project actually resolves this package or ref before using the "
            "receipt for version-specific findings.",
        )
    if resolution_kind == "exact_tag" and verified and commits_agree:
        return (
            "PROVEN",
            "exact tag",
            "The exact tag resolved to the observed commit, and the cached source passed "
            "integrity verification.",
            "Claim boundary",
            "Cite the observed commit as the durable identity because upstream tags can move.",
        )
    if resolution_kind == "heuristic_tag":
        integrity = (
            "The cached source passed integrity verification, but"
            if verified
            else "The cached source is not fully verified, and"
        )
        return (
            "CANDIDATE",
            "heuristic tag",
            f"{integrity} the package-to-tag mapping was inferred rather than proven.",
            "Decision",
            "Treat this source only as a lead; independently verify the mapping before "
            "making exact-version claims.",
        )
    if resolution_kind == "unresolved":
        return (
            "BLOCKED",
            "provenance unresolved",
            "A usable source tree exists, but no exact package/ref-to-commit binding is proven.",
            "Decision",
            "Block exact-version claims; exploratory use must be labeled unproven.",
        )
    if resolution_kind in {"exact_commit", "exact_tag"}:
        return (
            "BLOCKED",
            "verification incomplete",
            "Exact provenance was recorded, but source integrity or commit agreement is not "
            "verified.",
            "Decision",
            "Re-verify the source and resolve any commit mismatch before using it as evidence.",
        )
    return (
        "BLOCKED",
        "evidence kind unknown",
        "The evidence kind is unknown and cannot support an exact-source claim.",
        "Decision",
        "Upgrade the tool or verify the source relationship independently before relying on it.",
    )


def _is_package_query(package: Mapping[str, Any], request: ResolveRequest) -> bool:
    package_id = _text(package.get("id"))
    return bool(package_id and package_id.casefold() == request.query.casefold())


def _ecosystem_label(value: str | None) -> str:
    if value == "nuget":
        return "NuGet"
    return value or "Package"


def _public_repository_name(
    repository: Mapping[str, Any], provider: str
) -> str:
    canonical_name = _text(repository.get("canonical_name"))
    display_name = _text(repository.get("display_name"))
    remote_url = _text(repository.get("remote_url"))
    if (
        remote_url
        and remote_url.casefold().startswith("file:")
    ) or (
        canonical_name
        and canonical_name.casefold().startswith("git:file/")
    ):
        return "local Git source"
    if canonical_name and canonical_name.startswith("local:"):
        return display_name or "local source"
    if canonical_name:
        return _OPAQUE_REPOSITORY_SUFFIX.sub("", canonical_name)
    if display_name:
        return display_name
    return "local source" if provider == "local" else "not available"


def _public_artifact_ref(value: str | None, artifact_kind: str | None) -> str | None:
    if not value or artifact_kind != "local":
        return value
    return _LOCAL_REF_SUFFIX.sub("", value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _code(value: Any) -> str:
    text = _text(value) or "not available"
    single_line = _single_line_visible_text(text)
    escaped = html.escape(single_line, quote=True).translate(
        _MARKDOWN_DELIMITER_ENTITIES
    )
    return f"<code>{escaped}</code>"


def _single_line_visible_text(value: str) -> str:
    visible: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if character.isspace():
            visible.append(" ")
        elif category in {"Cc", "Cf", "Cs"}:
            codepoint = ord(character)
            escape = (
                f"\\u{codepoint:04X}"
                if codepoint <= 0xFFFF
                else f"\\U{codepoint:08X}"
            )
            visible.append(escape)
        else:
            visible.append(character)
    return " ".join("".join(visible).split()) or "not available"
