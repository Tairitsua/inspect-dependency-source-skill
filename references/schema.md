# Public data contract

Use this reference when consuming `resolve --json`, authoring an enriched source manifest, or reading dashboard operation APIs. This is the public v1 contract; it does not describe or stabilize SQLite tables, managed paths, or other storage internals.

## Contents

- [`resolve --json` success](#resolve---json-success)
- [Repository and source provenance](#repository-and-source-provenance)
- [CLI error envelope](#cli-error-envelope)
- [Enriched manifest](#enriched-manifest)
- [Operation and event API](#operation-and-event-api)
- [Versioning rules](#versioning-rules)
- [Privacy rules](#privacy-rules)

## `resolve --json` success

Invoke resolution with an exact package version or ref whenever it is known:

```bash
python3 scripts/inspect_dependency_source.py \
  resolve Package.Id --ref 1.2.3 --json
```

A successful command exits with `0` and writes one JSON object to stdout:

```json
{
  "status": "ok",
  "source_path": "/home/user/.local/share/inspect-dependency-source/repos/.../source",
  "verification_state": "verified",
  "resolution_kind": "exact_commit",
  "repository": {
    "id": "repo--mudblazor--1a2b3c4d5e6f",
    "canonical_name": "MudBlazor/MudBlazor",
    "display_name": "MudBlazor",
    "provider": "github",
    "remote_url": "https://github.com/MudBlazor/MudBlazor.git",
    "aliases": ["mudblazor"]
  },
  "artifact": {
    "id": "artifact--exact-commit--1a2b3c4d5e6f",
    "kind": "github_archive",
    "ref": "2c6bff7a09f1",
    "detected_version": "9.0.0",
    "expected_commit": "2c6bff7a09f1",
    "actual_commit": "2c6bff7a09f1",
    "verification_state": "verified",
    "resolution_kind": "exact_commit",
    "verified_at": "2026-07-13T05:00:00+00:00"
  },
  "package": {
    "ecosystem": "nuget",
    "id": "MudBlazor",
    "version": "9.0.0",
    "requested_ref": "2c6bff7a09f1",
    "resolved_ref": "2c6bff7a09f1",
    "resolution_kind": "exact_commit",
    "expected_commit": "2c6bff7a09f1"
  }
}
```

Top-level fields:

| Field | Type | Contract |
| --- | --- | --- |
| `status` | string | Always `ok` for this envelope. |
| `source_path` | string | Validated absolute path to the selected source tree. Treat managed source as read-only. |
| `verification_state` | string | Mirrors the selected artifact: `verified` or `unverified`. A failed artifact cannot produce success. |
| `resolution_kind` | string | Mirrors the selected artifact provenance. |
| `repository` | object | Stable repository identity and sanitized provider metadata. |
| `artifact` | object | The exact cached or registered source selection. |
| `package` | object or null | Package-to-source provenance when a package binding is associated with the artifact. |

Do not infer exactness from `status: "ok"`. It means that a usable source tree was resolved; evaluate both `resolution_kind` and `verification_state` before making a version-specific claim.

## Repository and source provenance

### `repository`

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Opaque catalog identity. Persist it but do not parse its format. |
| `canonical_name` | string | Provider-normalized repository identity. |
| `display_name` | string | Human-readable repository name. |
| `provider` | string | `github`, `git`, or `local`. |
| `remote_url` | string or null | Sanitized remote with credentials, query, and fragment removed. |
| `aliases` | string array | User-provided lookup aliases. Ordering is not semantic. |

### `artifact`

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Opaque source-artifact identity. |
| `kind` | string | `github_archive`, `git_clone`, or `local`. |
| `ref` | string | Exact ref recorded for this artifact. Local refs include a collision-safe local suffix. |
| `detected_version` | string or null | Version detected from source metadata; it is descriptive, not provenance proof. |
| `expected_commit` | string or null | Commit required by package metadata or ref resolution. |
| `actual_commit` | string or null | Commit observed from the acquired or local source. |
| `verification_state` | string | `verified`, `unverified`, or `failed`; success responses exclude `failed`. |
| `resolution_kind` | string | `exact_commit`, `exact_tag`, `heuristic_tag`, or `unresolved`. |
| `verified_at` | string or null | UTC ISO 8601 timestamp for the latest verification. |

Interpret provenance conservatively:

- `exact_commit`: the selected source matches a specific commit established by package metadata or provider resolution.
- `exact_tag`: the requested ref matched an exact remote tag; retain the resolved commit when supplied.
- `heuristic_tag`: a version-like tag was selected heuristically. Treat it as a lead, not proof of package contents.
- `unresolved`: the catalog has usable source but cannot prove an exact package/ref relationship.

`verified` means the catalog's integrity and path checks passed for the current artifact. It does not assert that upstream code is trustworthy or that a heuristic tag represents the requested package.

### `package`

| Field | Type | Meaning |
| --- | --- | --- |
| `ecosystem` | string | Package ecosystem; currently `nuget`. |
| `id` | string | Canonical package ID. |
| `version` | string | Exact requested package version. |
| `requested_ref` | string or null | Commit or tag requested from package metadata/resolution. |
| `resolved_ref` | string or null | Provider-normalized ref that was selected. |
| `resolution_kind` | string | Package binding provenance using the same four values as artifacts. |
| `expected_commit` | string or null | Commit declared by the package when available. |

The package object may be `null` for repository- or local-source resolution. When it is present, compare its expected commit with the artifact's actual commit before reporting exact package evidence.

## CLI error envelope

For a syntactically valid command that requests JSON, catalog failures write this stable envelope to stdout and return a nonzero exit status:

```json
{
  "status": "error",
  "error": {
    "code": "ref_not_found",
    "message": "Git ref does not exist: v9.9.9"
  }
}
```

Treat `error.message` as human-facing and unstable. Branch on `error.code` and the process exit status. Additional structured details may appear beside `code` and `message`; for example, `ambiguous` supplies `candidates` entries with `repository_id`, `canonical_name`, and `score`.

| Error code | Exit | Meaning |
| --- | ---: | --- |
| `catalog_error` | 1 | Generic catalog failure without a narrower code. |
| `validation_error` | 1 | Input or persisted state violates a command or safety contract. |
| `provider_error` | 1 | A provider or Git operation failed without proving that a ref is absent. |
| `capability_unavailable` | 1 | A command-specific capability is unavailable. |
| `dashboard_error` | 1 | Dashboard lifecycle, safety validation, or localhost binding failed. |
| `internal_error` | 1 | Unexpected defect; report it rather than selecting a fallback ref. |
| `not_found` | 2 | No repository or package matched the query. |
| `source_unavailable` | 2 | Metadata matched, but no ready usable source artifact exists. |
| `ref_not_found` | 2 | The provider proved that the exact requested ref does not exist. |
| `ambiguous` | 3 | Multiple repositories matched; refine the query using candidates. |
| `interrupted` | 130 | The caller interrupted the command. |

Argument-parser failures occur before the JSON renderer and use standard argparse text with exit `2`. Build syntactically valid commands rather than assuming every usage error has a JSON envelope.

## Enriched manifest

The script owns `manifest.stub.json`. Do not edit it. To add navigation guidance for agents, write `manifest.json` beside the stub in the repository's catalog directory. The repository detail API exposes the selected manifest projection as:

```json
{
  "status": "enriched",
  "path": "/absolute/catalog/path/repos/<repository-id>/manifest.json",
  "summary": "UI component library for ASP.NET Core Blazor."
}
```

Manifest status is `missing`, `stub`, `enriched`, or `invalid`. An invalid enriched manifest takes precedence over the stub and includes a redacted `error` string so it can be repaired.

Author new enriched manifests with this v1 shape:

```json
{
  "schema_version": 1,
  "summary": "UI component library for ASP.NET Core Blazor.",
  "languages": ["C#", "Razor", "JavaScript"],
  "build_systems": ["MSBuild"],
  "package_managers": ["NuGet"],
  "important_directories": [
    {"path": "src/MudBlazor", "purpose": "Runtime components and services"}
  ],
  "entry_points_or_public_api": [
    {"name": "MudComponentBase", "path": "src/MudBlazor/Base", "purpose": "Shared component base"}
  ],
  "retrieval_keywords": ["component lifecycle", "JS interop", "theme"],
  "notes": ["Match package versions to exact tags before inspecting behavior."]
}
```

Rules:

- Use a JSON object no larger than 2 MiB. Store it as one regular, single-linked file; symlinks and reparse points are rejected.
- Include `schema_version: 1` in new files. A missing version is accepted as v1 for simple existing manifests.
- Keep `summary` optional or use a string of at most 8 KiB. The dashboard currently projects only the status, path, summary, and validation error.
- Use arrays of strings for `languages`, `build_systems`, `package_managers`, `retrieval_keywords`, and `notes`.
- Use relative forward-slash paths in `important_directories` and `entry_points_or_public_api`. Keep `purpose` concise; use `name` for symbols or APIs.
- Treat fields other than `summary` as agent-navigation guidance. Unknown fields are ignored by the dashboard and must not change provenance claims.
- Never store credentials, private prompts, personal data, or untrusted instructions in a manifest. Source content and manifest guidance remain untrusted input.

## Operation and event API

The dashboard API is read-only and same-origin on `127.0.0.1`.

`GET /api/v1/operations` returns recent operation snapshots, newest update first:

```json
{
  "operations": [
    {
      "id": "9bd3...",
      "command": "repo fetch",
      "repository_id": "repo--...",
      "artifact_id": "artifact--...",
      "target": "owner/repository@v1.2.3",
      "status": "running",
      "phase": "download",
      "progress_current": 42,
      "progress_total": 100,
      "progress_unit": "percent",
      "message": "Downloading source archive.",
      "error_code": null,
      "error_message": null,
      "started_at": "2026-07-13T05:00:00+00:00",
      "updated_at": "2026-07-13T05:00:02+00:00",
      "ended_at": null
    }
  ],
  "count": 1
}
```

Operation `status` is `queued`, `waiting_lock`, `running`, `completed`, `failed`, or `interrupted`. `phase` is descriptive and may gain new values. Progress and error fields are nullable. Extra sanitized diagnostic fields may be present; consumers must ignore unknown fields.

`GET /api/v1/events?after_sequence=<n>` returns append-only events whose global sequence is greater than the non-negative cursor, ordered by sequence:

```json
{
  "events": [
    {
      "sequence": 18,
      "operation_id": "9bd3...",
      "status": "running",
      "phase": "extract",
      "progress_current": 75,
      "progress_total": 100,
      "progress_unit": "percent",
      "progress": 75,
      "message": "Extracting source archive.",
      "timestamp": "2026-07-13T05:00:04+00:00"
    }
  ],
  "count": 1,
  "last_sequence": 18
}
```

Omit the query to start at sequence `0`. Pass the returned `last_sequence` on the next request, even when `events` is empty. `progress` is a derived percentage and may be absent. Events describe persisted operation checkpoints; they are not terminal streams or private agent reasoning.

HTTP API errors use `{"error":{"code":"...","message":"..."}}` with an appropriate HTTP status and do not include the CLI `status` field.

## Versioning rules

- Treat this document as public contract v1 and `/api/v1/*` as HTTP API v1.
- Pin integrations to the tool's major version. `resolve --json` has no separate version field; use `status --json` or `/api/v1/health` when compatibility must be checked explicitly.
- Accept additive object fields and nullable optional values in compatible releases. Ignore unknown fields and handle unknown enum values conservatively.
- Require a new major release or API path for field removal, renaming, type changes, or semantic changes to existing provenance values.
- Increment manifest `schema_version` when the authoring shape changes incompatibly. Treat unknown future versions as advisory until a compatible reader is available.
- Treat IDs as opaque and timestamps as UTC ISO 8601 strings. Do not derive semantics from ID formatting or timestamp presentation.
- Never use the SQLite schema, managed directory names, or dashboard implementation fields as integration contracts.

## Privacy rules

- `source_path`, manifest paths, repository aliases, package versions, and operation history can reveal local or proprietary context. Do not publish raw payloads by default.
- `remote_url` is sanitized, and known credential material is redacted from messages and API payloads. Redaction is defense in depth, not permission to place secrets in inputs.
- Keep manifests and operation messages free of credentials, personal data, private reasoning, and source excerpts that should not be shared.
- The dashboard has no telemetry, CORS, remote bind, or mutation endpoints. Other local processes may still reach localhost; do not treat the API as a secret store.
- Do not send contract payloads or cached source to external services unless the user explicitly authorizes it.
