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

Place `--catalog-root <absolute-path>` before the subcommand to override the catalog for one invocation. Use the shared user catalog by default.

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
python3 scripts/inspect_dependency_source.py repo remove short-name
python3 scripts/inspect_dependency_source.py repo remove short-name --purge-managed-cache --yes
```

Omitting `--purge-managed-cache` keeps managed files. Purging requires both explicit flags and must never delete local source trees registered outside the catalog.

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

Use `resolve --json` in other skills and automation. Use human-readable output only for interactive work.

## Dashboard commands

```bash
python3 scripts/inspect_dependency_source.py dashboard start
python3 scripts/inspect_dependency_source.py dashboard start --port 8765
python3 scripts/inspect_dependency_source.py dashboard status
python3 scripts/inspect_dependency_source.py dashboard stop
```

The dashboard binds to `127.0.0.1`. Starting it must reuse a healthy user-level process. Status verifies process metadata against the health endpoint rather than trusting a PID alone.

## Stable resolution contract

Treat `resolve --json` as the public machine interface. A success exposes `source_path`, `verification_state`, and `resolution_kind` at the top level, with nested repository, artifact, and optional package provenance. Check `status` and the process exit code before dereferencing fields.

Read [schema.md](schema.md) for the complete success schema, stable error envelope and codes, manifest format, operation/event projections, and versioning rules. Do not couple consumers to SQLite tables or internal directory names.

## Automation rules

- Capture stdout as the requested text or JSON payload. Send diagnostics and progress to stderr.
- Check the process exit code before consuming a result.
- Redact credentials before logging command arguments or output.
- Avoid concurrent duplicate fetches for the same artifact. Let the per-artifact operation lock serialize them.
- Allow unrelated artifacts to download concurrently.
- Retry only resumable network failures. Do not retry validation, traversal, ambiguity, or missing-ref failures as another ref.
