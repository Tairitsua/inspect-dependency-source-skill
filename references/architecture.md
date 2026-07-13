# Catalog architecture and safety

Use this reference when configuring storage, integrating the dashboard, or evaluating privacy and failure behavior.

## Catalog root

Resolve the active user-level catalog root in this order:

1. Command-level `--catalog-root`.
2. `INSPECT_DEPENDENCY_SOURCE_HOME`.
3. The root saved by `config set-root` in user configuration.
4. The platform-standard user data directory.

Use `${XDG_DATA_HOME:-$HOME/.local/share}/inspect-dependency-source` on Linux, `~/Library/Application Support/Inspect Dependency Source` on macOS, and `%LOCALAPPDATA%\Inspect Dependency Source` on Windows as the platform defaults.

Never derive runtime ownership from the skill installation path, the current working directory, or a project repository. Changing the configured root affects future commands and does not move an existing catalog.

## Storage model

Use SQLite in WAL mode for metadata. Keep transactions short so CLI mutations and dashboard reads can proceed concurrently. Model these concepts separately:

- Repositories and aliases.
- User-owned local source registrations.
- Managed source artifacts, exact refs, commits, and verification state.
- Package-version bindings and their resolution provenance.
- Cached tags and refresh timestamps.
- Operations and append-only operation events.

Keep downloaded archives, staging areas, promoted source trees, locks, dashboard process metadata, and cached reconciliation metrics outside the database under the selected catalog root. Treat internal paths and tables as implementation details; integrate through the CLI or read-only HTTP API. Read [schema.md](schema.md) for the stable public JSON, manifest, and operation-event contracts.

## Artifact integrity

- Create collision-safe artifact IDs with a normalized label and hash suffix.
- Download and extract into a staging directory under the catalog root.
- Reject absolute archive paths, traversal, symlink escape, excessive members, and excessive extracted size.
- Verify staged content before atomically promoting it.
- Preserve the previous verified artifact until a replacement succeeds.
- Lock by artifact so identical fetches serialize while unrelated fetches can proceed.
- Recover interrupted operations without presenting incomplete staging data as a valid artifact.
- Reclaim generated staging directories older than 24 hours only after acquiring the
  matching artifact lock; preserve unknown entries and stages owned by active work.
- Guard deletion with containment checks and never delete a registered user-owned local tree.

## Dashboard and APIs

Run one read-only dashboard per user catalog on `127.0.0.1`. Expose only GET and HEAD routes:

- `GET /api/v1/health`
- `GET /api/v1/summary`
- `GET /api/v1/repositories`
- `GET /api/v1/repositories/{id}`
- `GET /api/v1/repositories/{id}/tags`
- `GET /api/v1/operations`
- `GET /api/v1/events?after_sequence=<n>`

Return repository metadata, provenance, verification, cached metrics, and operation history. Never serve arbitrary source files, environment variables, credentials, mutation controls, or internal database access.

Apply a restrictive Content Security Policy and other security headers. Do not enable CORS or load CDN resources. Persist language, theme, filters, the selected repository, and expanded timelines locally in the browser. Refresh data every two seconds without discarding those choices.

Calculate directory sizes, integrity scans, and free-space reconciliation asynchronously. Serve cached metrics from polling endpoints so a large catalog cannot block the UI. Refresh lightweight manifest and local-source snapshots on each background cycle, while throttling full tree hashing and recursive disk measurement to at most once every six hours unless the user runs `verify` explicitly.

## Privacy and network behavior

On POSIX systems, the catalog root and managed directories use mode `0700`; state, lock, and lifecycle files are created with owner-only `0600` permissions. Platform ACLs remain the user's responsibility on Windows.

Keep catalog metadata, source trees, dashboard state, and operation history on the user's machine. Do not collect telemetry or send catalog contents to a hosted service.

Outbound traffic occurs only for explicit remote work such as registering a GitHub repository, refreshing metadata, downloading Git/GitHub source, or resolving NuGet package metadata. Public Git and GitHub operations must not require `gh`. Use `GH_TOKEN`, `GITHUB_TOKEN`, or authenticated `gh` only when available for private access or higher API limits.

Sanitize credentials in remotes before persistence or display. Redact secrets from errors, operation events, CLI JSON, and dashboard APIs. Binding to localhost limits exposure but does not make source metadata safe to share; users should review catalog contents before screen sharing or publishing logs.

## Compatibility

Do not support the legacy project-local JSON catalog, `source_catalog.py`, its cache layout, or an unsupported SQLite schema version. This clean-break release fails closed on a schema-version mismatch instead of migrating old state. Start the global catalog empty unless the user explicitly registers existing source as local. Never import or delete a legacy cache implicitly.
