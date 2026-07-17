---
name: inspect-dependency-source
description: >-
  Manage and reuse exact third-party dependency source trees from one user-level
  catalog shared across projects and coding agents. Use when debugging behavior
  across a library or SDK boundary, verifying version-specific implementation
  details, resolving a NuGet package to its repository commit or tag, caching
  GitHub or generic Git source, registering local source trees, avoiding repeated
  dependency downloads, inspecting source provenance, producing a share-conscious
  Source Receipt, or checking the global catalog and dashboard. Do not use for
  first-party code already present in the active workspace, documentation-only API
  questions, or arbitrary file deletion outside the catalog.
---

# Inspect Dependency Source

Ground dependency analysis in the exact source being used, then preserve the evidence in a Source Receipt. Reuse the global catalog before downloading another copy or reasoning from documentation alone.

## Scope boundaries

- Use this skill for third-party implementation source. Inspect first-party code already in the active workspace directly.
- Prefer authoritative documentation for usage-only questions that do not require implementation details.
- Never use catalog removal commands as a general filesystem cleanup mechanism.
- Never edit a managed source artifact in place. It is shared evidence for other projects and agents.

Resolve `SKILL_DIR` to the directory containing this `SKILL.md`; do not assume the current project contains the skill. Invoke the CLI by absolute path:

```bash
python3 "$SKILL_DIR/scripts/inspect_dependency_source.py" <command>
```

## Resolve and inspect source

1. Run `resolve <query> --json` before fetching anything. When an exact package version is known, always run `resolve <package-id> --ref <version> --json` so another cached version cannot be selected. Treat this as the stable integration interface; do not read the catalog database directly.
2. Prefer an exact package commit or exact tag. If the caller specifies a ref, fail closed when it is unavailable; never replace it with the default branch.
3. Register the dependency only when resolution reports that it is absent:
   - Use `package fetch-nuget` for an exact NuGet package version.
   - Use `repo add-github` for a GitHub repository.
   - Use `repo add-git` for another Git remote.
   - Use `repo add-local` for an existing source tree.
4. Fetch an explicit ref with `repo fetch --ref`. Use `--default-branch` only when the user explicitly wants the current default branch.
5. Resolve again, verify the returned provenance and commit, then inspect `source_path`.
6. When a finding relies on resolved dependency source, run `resolve <query> --ref <ref> --receipt` and include the resulting Source Receipt with the finding.
7. Keep managed artifacts read-only. They are shared by other projects and agents.

Read [references/workflows.md](references/workflows.md) for provider-specific procedures and recovery rules. Read [references/cli.md](references/cli.md) for exact commands. Read [references/schema.md](references/schema.md) before consuming JSON or operation APIs, or authoring an enriched manifest.

## Operate the global catalog

- Run `init` once to create the user-level catalog and start or reuse the local dashboard. Add `--no-dashboard` for headless use.
- Run `doctor` to inspect local Git, authentication, and runtime prerequisites before network-backed work; it does not perform a network probe. Keep local resolution, status, and dashboard inspection usable offline.
- Run `verify` when source integrity or availability is uncertain.
- Run `status` for a compact catalog snapshot.
- Run `dashboard status` to discover the active localhost URL.
- Use `INSPECT_DEPENDENCY_SOURCE_HOME` or `--catalog-root` only for an intentional catalog override.

Read [references/architecture.md](references/architecture.md) for root precedence, storage ownership, dashboard APIs, security, privacy, and outbound traffic.

## Destructive removal gate

Treat every `repo remove` as destructive, and treat `--purge-managed-cache` as a separate higher-risk action. Never infer purge authorization from requests such as "clean up," "remove the dependency," or "free some space."

Before running either removal form:

1. Run `repo remove <query> --plan --json`. If the query is absent or ambiguous, stop.
2. Read and present the exact `repository.id`, `repository.canonical_name`, `metadata_removal.deletion_set_digest`, `purge.managed_cache_path`, every `purge.managed_artifacts` entry, and every `purge.preserved_local_sources` entry. Retain the returned `plan_token` only for the authorized execution.
3. Continue only when both `purge.local_sources_excluded` and `purge.safe_to_purge` are `true`. State the effect precisely: metadata-only removal keeps managed files; purge deletes the listed managed cache; every listed local source is preserved.
4. Ask for explicit authorization naming the canonical repository and whether managed cache will be purged. Do not continue in the same response while waiting for that answer.
5. After authorization, bind execution to that exact snapshot: `repo remove <repository.id> --plan-token <plan_token>` for metadata only, or add both `--purge-managed-cache --yes` only when purge was explicitly authorized. Never execute against the original fuzzy query or canonical name.
6. Confirm the result reports `exists_after: true` for every preserved path whose preview reported `exists: true`. Stop and report a preservation failure if it does not.

Stop without removal if the preview cannot prove the target, any artifact lacks an `external` classification, or a supposedly managed path is outside the selected catalog root. If execution says the plan changed, generate a fresh preview, present it, and request authorization again; never reuse or substitute a token. Never add `--yes` merely to make a command non-interactive.

## Report evidence

Continue using `resolve <query> --ref <ref> --json` for local navigation and source inspection. When a finding relies on resolved dependency source, also run `resolve <query> --ref <ref> --receipt` and include the resulting Source Receipt.

Only `exact_commit` or `exact_tag` combined with `verified` integrity and matching full 40- or 64-hex expected and observed commit identities supports a `PROVEN` receipt. A package receipt also requires `--ref <exact-version>`; an omitted or mismatched package version is `BLOCKED` even when a cached binding exists. A `heuristic_tag` receipt is a `CANDIDATE` requiring independent verification. An `unresolved`, unverified, mismatched, malformed, or unknown relationship is `BLOCKED` for exact-version claims.

Receipts intentionally withhold absolute paths, remote URLs, aliases, catalog IDs, and path-derived identity suffixes. JSON remains local evidence and may contain sensitive paths or repository context; do not publish it by default.
