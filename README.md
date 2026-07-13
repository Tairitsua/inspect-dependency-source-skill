# Inspect Dependency Source

[简体中文](README.zh-CN.md)

> Stop guessing at dependency behavior. Ground coding agents in the exact source they are debugging.

Coding agents are often accurate inside your repository and surprisingly uncertain at a dependency boundary. They may inspect the latest upstream branch instead of the version you use, repeat the same large download in several workspaces, lose the link between a package and its source commit, or rely on documentation that cannot explain implementation behavior.

Inspect Dependency Source adds a user-level source catalog shared by every project and coding agent on the same machine. It resolves dependencies to reusable local source trees, records how each version was selected, fails closed when an exact ref is unavailable, and provides a polished localhost dashboard for people who want to see what is cached and whether it is trustworthy.

The result is not a smarter model. It is a more reliable source-analysis workflow with better evidence.

## What it solves

- **Exact-version debugging:** bind a package or requested ref to a tag or commit instead of silently substituting the default branch.
- **Reusable source grounding:** download once, then resolve the same verified source from Codex, Claude, and different repositories.
- **Visible provenance:** distinguish `exact_commit`, `exact_tag`, `heuristic_tag`, and `unresolved` evidence.
- **Safer caching:** stage, validate, and atomically promote managed artifacts while retaining the last known-good copy.
- **Local observability:** inspect repositories, artifacts, package bindings, freshness, integrity, disk use, and operation history in an offline-capable dashboard.
- **Automation without internals:** integrate through a stable `resolve --json` contract rather than reading storage files directly.

Runtime code uses only the Python standard library. Public Git and GitHub repositories do not require the GitHub CLI.

## A local, source-first Context7 alternative for implementation-level debugging

[Context7](https://context7.com/docs/overview) retrieves version-specific documentation and code examples through integrations such as MCP, its CLI, and Skills. That is useful when an agent needs authoritative usage guidance without leaving its workflow.

Inspect Dependency Source addresses a related but different grounding problem: it manages complete source trees on the user's machine, pins them to exact refs or commits, records package-to-source provenance, and reuses the source offline for implementation-level debugging.

| Need | Context7 | Inspect Dependency Source |
| --- | --- | --- |
| Primary grounding material | Version-specific documentation and examples | Complete local source trees |
| Typical question | “How should I use this API?” | “What does this exact dependency version do internally?” |
| Delivery | MCP, CLI, and Skills | User-level Skill, CLI, and localhost dashboard |
| Offline reuse | Depends on the integration and retrieved context | Cached source remains locally reusable |
| Provenance focus | Library documentation version | Package/ref to artifact and commit |

This project can be used as a local, source-first Context7 alternative for source-grounding workflows. It is not API-compatible with Context7, does not impersonate its MCP service, and does not replace documentation-focused retrieval. The two approaches can also be used together. See the [Context7 repository](https://github.com/upstash/context7) for its current capabilities.

## Supported sources

- GitHub repositories.
- Generic Git remotes.
- Existing local source trees.
- Exact NuGet package versions, including repository-commit provenance when the package provides it.

npm, PyPI, Maven, and Cargo package resolution are not part of the first release. Their repositories can still be registered through Git or as local source.

## User-level installation

Install the skill once per OS user, not inside a project. The example keeps the canonical checkout in the Codex user-skill directory and links Claude to the same checkout:

```bash
mkdir -p "$HOME/.agents/skills" "$HOME/.claude/skills"
git clone https://github.com/Tairitsua/inspect-dependency-source.git \
  "$HOME/.agents/skills/inspect-dependency-source"
ln -s "$HOME/.agents/skills/inspect-dependency-source" \
  "$HOME/.claude/skills/inspect-dependency-source"
```

- Codex user skills: `$HOME/.agents/skills/inspect-dependency-source`
- Claude user skills: `~/.claude/skills/inspect-dependency-source`

If symbolic links are unavailable, clone or copy the repository into each location. The catalog remains shared because its runtime root is independent of the installation directory and current project.

Requirements:

- Python 3.11 or newer with its standard `sqlite3` module.
- Git for Git-backed registration and fetches.
- Network access only for remote refresh, download, or NuGet resolution.
- Optional `gh`, `GH_TOKEN`, or `GITHUB_TOKEN` for private GitHub repositories or higher API limits.

## Quick start

Run commands from the installed skill directory:

```bash
cd "$HOME/.agents/skills/inspect-dependency-source"

# Create the global catalog and start or reuse the dashboard.
python3 scripts/inspect_dependency_source.py init

# Check local Git, GitHub-auth, and runtime prerequisites (without a network probe).
python3 scripts/inspect_dependency_source.py doctor

# Cache one exact source ref.
python3 scripts/inspect_dependency_source.py repo add-github owner/repository --alias example
python3 scripts/inspect_dependency_source.py repo fetch example --ref v1.2.3

# Obtain the stable machine-readable evidence used by agents.
python3 scripts/inspect_dependency_source.py resolve example --ref v1.2.3 --json

# Print the active localhost dashboard URL.
python3 scripts/inspect_dependency_source.py dashboard status
```

For an exact NuGet dependency:

```bash
python3 scripts/inspect_dependency_source.py package fetch-nuget Package.Id 1.2.3
python3 scripts/inspect_dependency_source.py resolve Package.Id --ref 1.2.3 --json
```

For source that already exists locally:

```bash
python3 scripts/inspect_dependency_source.py repo add-local /absolute/path/to/source --alias example
python3 scripts/inspect_dependency_source.py resolve example --json
```

See [the CLI reference](references/cli.md) for every command and [source inspection workflows](references/workflows.md) for exact-ref, NuGet, local, offline, and recovery procedures.

## Dashboard

`init` starts or reuses one dashboard for the selected global catalog unless `--no-dashboard` is supplied. You can also manage it explicitly:

```bash
python3 scripts/inspect_dependency_source.py dashboard start
python3 scripts/inspect_dependency_source.py dashboard status
python3 scripts/inspect_dependency_source.py dashboard stop
```

The responsive, read-only UI shows:

- Repository, artifact, package-binding, and verification counts.
- Searchable source inventory with aliases and sanitized remotes.
- Exact ref/commit provenance and preferred source paths.
- Cached tags, local Git snapshots, manifests, and freshness.
- Active and failed operations with expandable event timelines.
- Integrity warnings, cache disk use, and free space.

It supports English and Simplified Chinese, system light/dark themes, reduced motion, keyboard navigation, and 360/768/1440-pixel layouts. Language, theme, filters, selection, and expanded timelines persist across the two-second refresh cycle.

The server binds only to `127.0.0.1`, serves no CDN assets, disables CORS, exposes GET/HEAD only, and provides no fetch, remove, or other mutation controls.

## How agents use it

The included `SKILL.md` instructs an agent to resolve before downloading and to treat `resolve --json` as the stable integration contract. A result identifies the repository, requested version or ref, selected artifact, resolved commit, verification state, source path, package provenance, and deterministic failure information. See the [public data contract](references/schema.md) for exact fields, errors, enriched manifests, and operation events.

The source catalog is evidence, not authority by itself. Agents should:

1. Match the project's resolved dependency version.
2. Prefer `exact_commit` or `exact_tag` provenance.
3. Treat `heuristic_tag` as a lead requiring further verification.
4. Stop when an exact requested ref is unavailable instead of analyzing another branch.
5. Cite the ref, commit, provenance, and path in findings.
6. Keep managed source artifacts read-only because other projects and agents share them.

## Global catalog location

The active catalog root is selected in this order:

1. `--catalog-root <absolute-path>` for one command.
2. `INSPECT_DEPENDENCY_SOURCE_HOME`.
3. The root persisted by `config set-root`.
4. The platform-standard user data directory.

The default data directory is `${XDG_DATA_HOME:-$HOME/.local/share}/inspect-dependency-source` on Linux, `~/Library/Application Support/Inspect Dependency Source` on macOS, and `%LOCALAPPDATA%\Inspect Dependency Source` on Windows.

```bash
python3 scripts/inspect_dependency_source.py config show
python3 scripts/inspect_dependency_source.py config set-root /absolute/path/to/catalog
```

Changing the setting does not move existing data. The runtime root is never inferred from the skill folder or current repository.

Choose a dedicated catalog directory. Filesystem volume roots and directories containing unrelated entries are rejected; on POSIX, the selected directory becomes app-owned with mode `0700`.

Metadata is stored in SQLite using WAL mode. Managed archives, staged downloads, promoted source trees, operation locks, dashboard process metadata, and cached reconciliation metrics live under the same catalog root. Repository, local-source, artifact, package-binding, tag, operation, and event records are modeled separately. Consumers must use the CLI or read-only dashboard API instead of depending on the internal schema.

See [architecture and safety](references/architecture.md) for the storage model, HTTP endpoints, transactional artifact behavior, and safety boundaries.

## Privacy and security

- No telemetry is collected.
- Catalog metadata and source trees stay on the user's machine.
- On POSIX systems, catalog directories are restricted to the owning user (`0700`) and catalog state files use owner-only permissions (`0600`).
- Outbound traffic occurs only for explicit remote work such as GitHub registration,
  metadata refresh, source fetch, or package resolution.
- Credentials are removed from persisted and displayed remotes and redacted from errors, events, JSON, and APIs.
- Archive extraction rejects traversal, symlink escape, excessive member counts, and excessive size.
- Managed deletion is containment-checked and never removes a registered user-owned local tree.
- The dashboard never exposes arbitrary source files, environment variables, secrets, or mutation endpoints.

Localhost-only does not mean share-safe. Review repository names, local paths, package versions, and operation history before publishing logs or sharing the dashboard screen.

## Migration and compatibility

This is a breaking redesign of `third-party-source-catalog`. It intentionally does not support the old `source_catalog.py` commands, project-local JSON catalog, or legacy cache layout.

The new user-level catalog starts empty. Keep an old cache untouched, then explicitly register any source you still need with `repo add-local`. Nothing is imported, moved, or deleted automatically.

## Development

Runtime code must remain Python-standard-library-only. Development and browser validation may use optional tooling.

```bash
# Standard-library unit and integration tests.
python3 -m unittest discover -s tests -p 'test_*.py' -v

# Validate the Skill package metadata and structure.
python3 /path/to/skill-creator/scripts/quick_validate.py .

# Optional browser validation (after `pip install playwright` and
# `playwright install chromium`).
python3 tests/browser_validation.py
```

Browser tests should cover bilingual persistence, repository switching, incremental operation timelines, responsive layouts, system themes, accessibility, security headers, and console/network errors.

Before publishing, confirm that the repository contains only the baseline import and breaking redesign commits, that the working tree is clean, and that installed Codex and Claude copies pass the same Skill validation.

## License

[MIT](LICENSE) © 2026 Momean.
