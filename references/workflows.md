# Source inspection workflows

Use these procedures to obtain evidence without confusing a likely version with the exact dependency under analysis.

## Reuse an existing source

1. Run `resolve <repository-or-package> --json`.
2. Confirm that `source_path` is available and verification is healthy.
3. Compare the resolved commit or ref with the dependency version in the user's project.
4. Inspect the returned path without modifying managed files.
5. Report the ref, commit, and provenance with the finding.

Do not download a second copy when a verified matching artifact already exists.

## Inspect an exact GitHub or Git ref

1. Register the repository with `repo add-github` or `repo add-git` when absent.
2. Refresh tag metadata only when the remote state is required.
3. Fetch the exact ref with `repo fetch <query> --ref <ref>`.
4. Stop if the ref is missing. Do not use the default branch as a substitute.
5. Resolve again and confirm the exact commit before inspecting the source.

Use `--default-branch` only for questions explicitly about current upstream behavior.

## Inspect a NuGet dependency

1. Read the exact package ID and version from the project's resolved dependency data rather than guessing from a project declaration.
2. Run `package fetch-nuget <package-id> <version>`.
3. Prefer the repository commit embedded in NuGet metadata when present.
4. Otherwise, evaluate exact tag forms before accepting a heuristic match.
5. Treat `heuristic_tag` as a lead, not proof. Compare assembly/package metadata or source contents before drawing a version-specific conclusion.
6. Resolve the exact package with `resolve <package-id> --ref <version> --json`; never omit `--ref` when the dependency version is known. Retain its package-to-artifact provenance in the final analysis.

## Register local source

Use `repo add-local <absolute-path>` for one known source tree. Use `repo scan-local` only when the user intentionally wants discovery under a parent directory.

- Preserve the canonical absolute path.
- Infer repository identity from a sanitized Git remote when possible.
- Treat a local tree as user-owned; never purge or rewrite it.
- Record current commit and dirty state when available because a local checkout may differ from its remote.

## Work offline

Use `resolve`, `verify`, `status`, and the dashboard against cached or local source without GitHub authentication. Avoid refresh or fetch operations unless network access becomes available. Explain when evidence may be stale rather than silently switching to a different source.

## Handle ambiguity and failure

- Refine an ambiguous query with the canonical repository name, owner, alias, package ID, or ref.
- Re-run `verify` when an artifact exists but cannot be read.
- Preserve the last verified artifact when a forced replacement fails.
- Inspect operation events for a failed download; do not infer success from the presence of a staging directory.
- Treat `unresolved` provenance, unavailable source, a missing exact ref, and an integrity failure as explicit blockers to exact source claims.

## Share evidence across agents

Use the user-level catalog as the shared source of truth. Pass `resolve --json` output or its repository/ref/commit/path fields to another agent instead of asking it to rediscover the dependency. Never pass secrets, raw environment variables, SQLite files, or internal staging paths.
