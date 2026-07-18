# Inspect Dependency Source

[简体中文](README.zh-CN.md)

[![Release readiness](https://github.com/Tairitsua/inspect-dependency-source-skill/actions/workflows/release-readiness.yml/badge.svg)](https://github.com/Tairitsua/inspect-dependency-source-skill/actions/workflows/release-readiness.yml)
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-compatible-5b45ff)](SKILL.md)
[![skills.sh](https://skills.sh/b/tairitsua/inspect-dependency-source-skill)](https://skills.sh/tairitsua/inspect-dependency-source-skill)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Give coding agents the exact third-party source they need, only when they need it.

Coding agents are usually accurate inside your repository and much less reliable at a dependency boundary. They may reason from documentation, inspect the latest upstream branch instead of the version your project uses, or download the same large repository in several workspaces.

Inspect Dependency Source is a user-level Agent Skill that retrieves exact dependency source into one reusable local catalog. It resolves packages and refs to commits, verifies cached trees before use, and fails closed instead of silently substituting a different branch. Agents receive a stable local `source_path` and the version, commit, provenance, and verification metadata needed to inspect that tree confidently.

Runtime code uses only the Python standard library. Public Git and GitHub repositories do not require the GitHub CLI.

## Install once at user level

The recommended installation makes the Skill available to every project for the current OS user:

```bash
npx skills add Tairitsua/inspect-dependency-source-skill --global
```

The package exposes one Skill and is listed on [skills.sh](https://skills.sh/tairitsua/inspect-dependency-source-skill). Remove `--global` only when you intentionally want a project-local installation.

For Claude Code's plugin marketplace:

```bash
claude plugin marketplace add Tairitsua/inspect-dependency-source-skill
claude plugin install inspect-dependency-source@inspect-dependency-source
```

If `npx` is unavailable, keep one canonical checkout in the Codex user-skill directory and link Claude to the same files:

```bash
mkdir -p "$HOME/.agents/skills" "$HOME/.claude/skills"
git clone https://github.com/Tairitsua/inspect-dependency-source-skill.git \
  "$HOME/.agents/skills/inspect-dependency-source"
ln -s "$HOME/.agents/skills/inspect-dependency-source" \
  "$HOME/.claude/skills/inspect-dependency-source"
```

- Codex user skills: `$HOME/.agents/skills/inspect-dependency-source`
- Claude user skills: `~/.claude/skills/inspect-dependency-source`

If symbolic links are unavailable, clone or copy the repository into each location. All installations still reuse one catalog because the runtime root is independent of the Skill directory and current project.

Requirements:

- Python 3.11 or newer with its standard `sqlite3` module.
- Git for Git-backed registration and fetches.
- Network access only for remote refresh, download, or NuGet resolution.
- Optional `gh`, `GH_TOKEN`, or `GITHUB_TOKEN` for private GitHub repositories or higher API limits.

## Enable automatic use in each project

Add this small routing rule to the project's `AGENTS.md`:

```md
## Dependency source inspection

When analysis or debugging depends on third-party library or SDK implementation, automatically use the user-level `inspect-dependency-source` skill, even if it was not explicitly named. Resolve the exact version or ref used by this project, reuse a matching catalog source before fetching, inspect the returned `source_path` read-only, and never substitute the upstream default branch.
```

The user-level installation makes the Skill available. The `AGENTS.md` rule tells the project's agents to invoke it proactively when a task crosses a dependency boundary, without requiring the user to name the Skill in every prompt.

[Claude Code reads `CLAUDE.md` rather than `AGENTS.md`](https://code.claude.com/docs/en/memory#agentsmd). If the project also uses Claude Code, add a root `CLAUDE.md` that imports the shared rule instead of duplicating it:

```md
@AGENTS.md
```

Start a new agent session after adding or changing either instruction file so the updated project guidance is loaded.

## Ask an ordinary dependency question

Once the routing rule is present, ask the question you actually care about:

```text
Why does Newtonsoft.Json 13.0.3 serialize this value this way?
```

The agent should recognize that the answer depends on third-party implementation, obtain the exact source used by the project, inspect it read-only, and report the package version and resolved commit with its finding. You should not have to prescribe catalog commands in the prompt.

## What happens automatically

1. Read the project's resolved dependency data and determine the exact package version or repository ref.
2. Run `resolve --json` against the user-level catalog before downloading anything.
3. If no matching usable tree exists, use the appropriate NuGet, GitHub, Git, or local-source command to acquire it.
4. Resolve again and confirm the expected commit, observed commit, provenance, and integrity state.
5. Inspect the returned `source_path` without modifying catalog-managed files.
6. Answer the original question and identify the dependency version and commit that were inspected.

The catalog is reused by projects and agents on the same machine, so a dependency already acquired for one workspace does not need to be downloaded again. An explicit missing ref is a blocker: the Skill never replaces it with the upstream default branch.

The stable machine-readable contract is:

```bash
python3 scripts/inspect_dependency_source.py \
  resolve Package.Id --ref 1.2.3 --json
```

A successful result includes `source_path`, `verification_state`, and `resolution_kind`, plus repository, artifact, and optional package provenance. Consumers must check the exit code and result status before reading the path. See the [stable local data contract](references/schema.md) for the complete schema and deterministic error envelope.

The [`Newtonsoft.Json 13.0.3` replay](examples/newtonsoft-json-13.0.3/README.md) demonstrates exact NuGet acquisition and resolution to commit `0a2e291c0d9c0c7675d445703e51750363a549ef` without checking a machine-specific path into the repository.

## Dashboard and observability

![Inspect Dependency Source dashboard showing catalog health, verified source inventory, and repository provenance](docs/images/dashboard-overview.png)

The responsive, read-only localhost dashboard keeps the user-level catalog observable. It shows:

- Repository, artifact, package-binding, and verification counts.
- Searchable source inventory with aliases and sanitized remotes.
- Exact ref/commit provenance and preferred source paths.
- Cached tags, local Git snapshots, manifests, and freshness.
- Active and failed operations with expandable event timelines.
- Integrity warnings, cache disk use, and free space.

`init` starts or reuses the dashboard unless `--no-dashboard` is supplied. It can also be managed explicitly:

```bash
python3 scripts/inspect_dependency_source.py dashboard start
python3 scripts/inspect_dependency_source.py dashboard status
python3 scripts/inspect_dependency_source.py dashboard stop
```

The UI supports English and Simplified Chinese, system light/dark themes, reduced motion, keyboard navigation, and 360/768/1440-pixel layouts. Language, theme, filters, selection, and expanded timelines persist across its two-second refresh cycle.

The server binds only to `127.0.0.1`, serves no CDN assets, disables CORS, exposes GET/HEAD only, and provides no fetch, remove, or other mutation controls.

## Supported sources

- GitHub repositories.
- Generic Git remotes.
- Existing local source trees.
- Exact NuGet package versions, including repository-commit provenance when the package provides it.

npm, PyPI, Maven, and Cargo package resolution are not part of the first release. Their repositories can still be registered through Git or as local source.

[opensrc](https://github.com/vercel-labs/opensrc) is useful for broad package-source retrieval, while [Context7](https://context7.com/docs/overview) supplies version-aware documentation and examples. This Skill focuses on exact, reusable source trees and fail-closed ref resolution.

## Advanced CLI and catalog administration

Run commands from the installed Skill directory:

```bash
cd "$HOME/.agents/skills/inspect-dependency-source"

# Initialize the catalog and dashboard, then check local prerequisites.
python3 scripts/inspect_dependency_source.py init
python3 scripts/inspect_dependency_source.py doctor

# Acquire and resolve an exact NuGet dependency.
python3 scripts/inspect_dependency_source.py package fetch-nuget Package.Id 1.2.3
python3 scripts/inspect_dependency_source.py resolve Package.Id --ref 1.2.3 --json

# Acquire and resolve an exact GitHub ref.
python3 scripts/inspect_dependency_source.py repo add-github owner/repository --alias example
python3 scripts/inspect_dependency_source.py repo fetch example --ref v1.2.3
python3 scripts/inspect_dependency_source.py resolve example --ref v1.2.3 --json

# Register source that already exists locally.
python3 scripts/inspect_dependency_source.py repo add-local /absolute/path/to/source --alias example
python3 scripts/inspect_dependency_source.py resolve example --json

# Reconcile cached source and inspect catalog health.
python3 scripts/inspect_dependency_source.py verify --all --json
python3 scripts/inspect_dependency_source.py status
```

See the [CLI reference](references/cli.md) for all commands and [source inspection workflows](references/workflows.md) for exact-ref, NuGet, local, offline, recovery, and removal procedures. Repository removal requires a preview, an exact repository ID, a matching plan token, and explicit authorization; managed-cache purging has an additional confirmation gate.

### Catalog location

The active catalog root is selected in this order:

1. `--catalog-root <absolute-path>` for one command.
2. `INSPECT_DEPENDENCY_SOURCE_HOME`.
3. The root persisted by `config set-root`.
4. The platform-standard user data directory.

The defaults are `${XDG_DATA_HOME:-$HOME/.local/share}/inspect-dependency-source` on Linux, `~/Library/Application Support/Inspect Dependency Source` on macOS, and `%LOCALAPPDATA%\Inspect Dependency Source` on Windows.

```bash
python3 scripts/inspect_dependency_source.py config show
python3 scripts/inspect_dependency_source.py config set-root /absolute/path/to/catalog
```

Changing the setting does not move existing data. The runtime root is never inferred from the Skill folder or current repository. Filesystem volume roots and directories containing unrelated entries are rejected; on POSIX, the selected directory becomes app-owned with mode `0700`.

Metadata is stored in SQLite using WAL mode. Managed archives, staged downloads, promoted source trees, operation locks, dashboard process metadata, and cached reconciliation metrics live under the same root. Consumers must use the CLI or read-only dashboard API instead of depending on the internal schema. See [architecture and safety](references/architecture.md) for the storage model, HTTP endpoints, transactional artifact behavior, and safety boundaries.

## Security boundary

- No telemetry is collected; catalog metadata and source trees stay on the user's machine.
- On POSIX, catalog directories are owner-only (`0700`) and catalog state files use `0600`.
- Outbound traffic occurs only for explicit remote work such as GitHub registration, metadata refresh, source fetch, or package resolution.
- Credentials are removed from persisted and displayed remotes and redacted from errors, events, JSON, and APIs.
- Archive extraction rejects traversal, symlink escape, excessive member counts, and excessive size.
- Downloads are staged and validated before atomic promotion; a failed replacement keeps the last verified artifact.
- Managed deletion is containment-checked and never removes a registered user-owned local tree.
- The dashboard never exposes arbitrary source files, environment variables, secrets, or mutation endpoints.
- Agents must preview the exact target and pause for explicit authorization before any `repo remove` operation. Purging managed cache requires a separate explicit approval.

The dashboard and CLI may display repository names, local paths, package versions, and operation history. Treat them as local development data and review logs or screenshots before sending them outside the machine.

## Repository map

| Path | Purpose |
| --- | --- |
| `SKILL.md` | Agent-facing triggers, exact-source workflow, failure rules, and destructive-operation pause. |
| `scripts/` | Standard-library CLI, catalog, providers, integrity checks, and dashboard. |
| `references/` | CLI, stable local schema, architecture, and provider-specific workflow contracts. |
| `examples/` | Reproducible exact dependency-source replays. |
| `assets/dashboard/` | Offline dashboard HTML, CSS, and JavaScript. |
| `tests/` | Unit, integration, safety, packaging, and real-browser validation. |

## Development

Runtime code must remain Python-standard-library-only. Development and browser validation may use optional tooling.

```bash
# Standard-library unit and integration tests.
python3 -m unittest discover -s tests -p 'test_*.py' -v

# Validate the Skill package metadata and structure.
python3 /path/to/skill-creator/scripts/quick_validate.py .

# Validate release metadata, documentation links, leakage rules, CI, and assets.
python3 scripts/validate_release.py

# Optional browser validation (after `pip install playwright` and
# `playwright install chromium`).
python3 tests/browser_validation.py
```

Browser tests cover bilingual persistence, repository switching, incremental operation timelines, responsive layouts, system themes, accessibility, security headers, and console/network errors.

Before a release, confirm that the full test suite and Skill validation pass, the working tree is clean, and installed Codex and Claude copies pass the same Skill validation.

See [CHANGELOG.md](CHANGELOG.md) for release history and the [draft v1.0.0 notes](docs/release-notes/v1.0.0.md) for the release narrative.

## Acknowledgements

- The cross-agent package follows the [Agent Skills specification](https://agentskills.io/specification).
- [opensrc](https://github.com/vercel-labs/opensrc) is a useful reference for broad package-source retrieval.
- [Context7](https://context7.com/docs/overview) complements source inspection with version-aware documentation and examples.
- The Claude marketplace layout follows [Anthropic's public Agent Skills marketplace](https://github.com/anthropics/skills/blob/main/.claude-plugin/marketplace.json).

## License

[MIT](LICENSE) © 2026 Momean.
