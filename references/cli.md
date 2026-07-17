# CLI reference

Use this reference when invoking the catalog manually, integrating another skill, or consuming JSON output. Run every command from the skill directory:

```bash
python3 scripts/inspect_dependency_source.py <command>
```

## Contents

- [Global options and configuration](#global-options-and-configuration)
- [Initialization and diagnostics](#initialization-and-diagnostics)
- [Repository commands](#repository-commands)
- [Package commands](#package-commands)
- [Resolution and verification](#resolution-and-verification)
- [Dashboard commands](#dashboard-commands)
- [Stable resolution contract](#stable-resolution-contract)
- [Automation rules](#automation-rules)

## Global options and configuration

Place `--catalog-root <absolute-path>` before the subcommand to override the catalog for one invocation. Use the reusable user-level catalog by default.

```bash
python3 scripts/inspect_dependency_source.py --catalog-root /srv/source-catalog status
python3 scripts/inspect_dependency_source.py config show
python3 scripts/inspect_dependency_source.py config set-root /srv/source-catalog
```

`config set-root` changes future operations. It does not move an existing catalog.

## Initialization and diagnostics

```bash
python3 scripts/inspect_dependency_source.py init
python3 scripts/inspect_dependency_source.py init --catalog-root /srv/source-catalog
python3 scripts/inspect_dependency_source.py init --no-dashboard
python3 scripts/inspect_dependency_source.py doctor
python3 scripts/inspect_dependency_source.py doctor --json
```

Use `init` to create or validate the selected catalog. By default, reuse or start one user-level localhost dashboard.

Use `doctor` to inspect local capabilities needed for network-backed work. It does not make a network request. Missing GitHub authentication must not prevent local repositories, cached source, status, or the dashboard from working.

## Repository commands

Register sources:

```bash
python3 scripts/inspect_dependency_source.py repo add-github owner/repository --alias short-name
python3 scripts/inspect_dependency_source.py repo add-git https://git.example.com/team/repository.git --alias short-name
python3 scripts/inspect_dependency_source.py repo add-local /absolute/path/to/source --alias short-name
python3 scripts/inspect_dependency_source.py repo scan-local /absolute/path/to/parent --max-depth 3 --update-existing
```

Refresh and inspect metadata:

```bash
python3 scripts/inspect_dependency_source.py repo refresh short-name
python3 scripts/inspect_dependency_source.py repo refresh --all
python3 scripts/inspect_dependency_source.py repo tags short-name --match 1.2.3 --limit 50
python3 scripts/inspect_dependency_source.py repo tags short-name --refresh --json
python3 scripts/inspect_dependency_source.py repo list
python3 scripts/inspect_dependency_source.py repo list short-name --json
```

Fetch source:

```bash
python3 scripts/inspect_dependency_source.py repo fetch short-name --ref v1.2.3
python3 scripts/inspect_dependency_source.py repo fetch short-name --default-branch
python3 scripts/inspect_dependency_source.py repo fetch short-name --ref v1.2.3 --force
```

An explicit ref is strict. A missing ref must fail rather than fetch the default branch. `--force` must retain the previous verified artifact until the replacement is completely downloaded, validated, and promoted.

Remove a catalog record:

```bash
python3 scripts/inspect_dependency_source.py repo remove <query> --plan --json
python3 scripts/inspect_dependency_source.py repo remove <repository.id> --plan-token <plan_token>
python3 scripts/inspect_dependency_source.py repo remove <repository.id> --plan-token <plan_token> --purge-managed-cache --yes
```

Omitting `--purge-managed-cache` keeps managed files. Purging requires both explicit flags and must never delete local source trees registered outside the catalog.

### Safe removal protocol

Agents must preview and pause before either removal form:

```bash
python3 scripts/inspect_dependency_source.py repo remove <query> --plan --json
```

Present `repository.id`, `repository.canonical_name`, `metadata_removal.deletion_set_digest`, `purge.managed_cache_path`, all `purge.managed_artifacts`, and every `purge.preserved_local_sources` entry. Continue only when every preserved path has `path_classified: true` and both `purge.local_sources_excluded` and `purge.safe_to_purge` are `true`. Explain whether the proposed action removes metadata only or also purges the listed catalog-managed path; every listed local source must remain untouched. Retain the returned `plan_token` for that authorized execution only.

Ask for explicit authorization that names the canonical repository and the purge effect. Execute only against the exact `repository.id` and `plan_token` returned by that preview, never the original fuzzy query. If repository state changes, execution rejects the stale token; preview and ask again instead of retrying automatically. A general cleanup request is not authorization, and `--yes` must never be added merely to avoid interaction. Stop if the target is ambiguous, an artifact lacks its `external` classification, or a managed path cannot be proven to live under the selected catalog root. After removal, require `exists_after: true` for every preserved path whose preview had `exists: true`. The preview JSON contains sensitive local paths and must remain local.

## Package commands

Resolve and fetch a NuGet package source:

```bash
python3 scripts/inspect_dependency_source.py package fetch-nuget Package.Id 1.2.3
python3 scripts/inspect_dependency_source.py package fetch-nuget Package.Id 1.2.3 --alias short-name --force
```

Prefer a repository commit recorded in the package metadata over tag matching. When no exact commit exists, record whether tag selection was exact, heuristic, or unresolved.

## Resolution and verification

```bash
python3 scripts/inspect_dependency_source.py resolve short-name
python3 scripts/inspect_dependency_source.py resolve Package.Id --ref 1.2.3 --json
python3 scripts/inspect_dependency_source.py verify short-name --json
python3 scripts/inspect_dependency_source.py verify --all --json
python3 scripts/inspect_dependency_source.py status
python3 scripts/inspect_dependency_source.py status --json
```

Use `resolve --json` in other skills and automation. It remains the stable machine contract and includes the validated local source path.

## Dashboard commands

```bash
python3 scripts/inspect_dependency_source.py dashboard start
python3 scripts/inspect_dependency_source.py dashboard start --port 8765
python3 scripts/inspect_dependency_source.py dashboard status
python3 scripts/inspect_dependency_source.py dashboard stop
```

The dashboard binds to `127.0.0.1`. Starting it must reuse a healthy user-level process. Status verifies process metadata against the health endpoint rather than trusting a PID alone.

## Stable resolution contract

Treat `resolve --json` as the stable local machine interface. A success exposes `source_path`, `verification_state`, and `resolution_kind` at the top level, with nested repository, artifact, and optional package provenance. Check `status` and the process exit code before dereferencing fields.

Read [schema.md](schema.md) for the complete success schema, stable error envelope and codes, manifest format, operation/event projections, and versioning rules. Do not couple consumers to SQLite tables or internal directory names.

## Automation rules

- Capture stdout as the requested text or JSON payload. Send diagnostics and progress to stderr.
- Check the process exit code before consuming a result.
- Redact credentials before logging command arguments or output.
- Avoid concurrent repository mutation. A repository-scoped guard serializes acquisition and removal, while the inner per-artifact lock protects promotion and recovery.
- Allow source work for different repositories to proceed concurrently; different refs of the same repository intentionally serialize.
- Retry only resumable network failures. Do not retry validation, traversal, ambiguity, or missing-ref failures as another ref.
