---
name: inspect-dependency-source
description: Manage and reuse exact third-party dependency source trees from one user-level catalog shared across projects and coding agents. Use when debugging behavior across a library or SDK boundary, verifying version-specific implementation details, resolving a NuGet package to its repository commit or tag, caching GitHub or generic Git source, registering local source trees, avoiding repeated dependency downloads, inspecting source provenance, or checking the global catalog and dashboard.
---

# Inspect Dependency Source

Ground dependency analysis in the exact source being used. Reuse the global catalog before downloading another copy or reasoning from documentation alone.

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
6. Keep managed artifacts read-only. They are shared by other projects and agents.

Read [references/workflows.md](references/workflows.md) for provider-specific procedures and recovery rules. Read [references/cli.md](references/cli.md) for exact commands. Read [references/schema.md](references/schema.md) before consuming JSON or operation APIs, or authoring an enriched manifest.

## Operate the global catalog

- Run `init` once to create the user-level catalog and start or reuse the local dashboard. Add `--no-dashboard` for headless use.
- Run `doctor` to inspect local Git, authentication, and runtime prerequisites before network-backed work; it does not perform a network probe. Keep local resolution, status, and dashboard inspection usable offline.
- Run `verify` when source integrity or availability is uncertain.
- Run `status` for a compact catalog snapshot.
- Run `dashboard status` to discover the active localhost URL.
- Use `INSPECT_DEPENDENCY_SOURCE_HOME` or `--catalog-root` only for an intentional catalog override.

Read [references/architecture.md](references/architecture.md) for root precedence, storage ownership, dashboard APIs, security, privacy, and outbound traffic.

## Report evidence

Include the selected repository, requested version or ref, resolved commit, `resolution_kind`, verification state, and source path in dependency-analysis findings. Distinguish exact evidence from `heuristic_tag` or `unresolved` results. Never imply that a heuristic match proves the package contents.
