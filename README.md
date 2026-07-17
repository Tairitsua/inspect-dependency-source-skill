# Inspect Dependency Source

[简体中文](README.zh-CN.md)

[![Release readiness](https://github.com/Tairitsua/inspect-dependency-source-skill/actions/workflows/release-readiness.yml/badge.svg)](https://github.com/Tairitsua/inspect-dependency-source-skill/actions/workflows/release-readiness.yml)
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-compatible-5b45ff)](SKILL.md)
[![skills.sh](https://skills.sh/b/tairitsua/inspect-dependency-source-skill)](https://skills.sh/tairitsua/inspect-dependency-source-skill)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Inspect the exact dependency source you use—and prove which commit it came from.

Coding agents are often accurate inside your repository and surprisingly uncertain at a dependency boundary. They may inspect the latest upstream branch instead of the version you use, repeat the same large download in several workspaces, lose the link between a package and its source commit, or rely on documentation that cannot explain implementation behavior.

Inspect Dependency Source is a user-level Agent Skill with a source catalog shared by every project and coding agent on the same machine. It resolves dependencies to reusable local source trees, records how each version was selected, fails closed when an exact ref is unavailable, and turns the result into a share-conscious Source Receipt.

The result is not a smarter model. It is an inspectable chain from package or ref to repository, commit, verified local source, and a receipt that preserves the claim boundary.

[See the proof](#proof-in-one-receipt) · [Install](#install-in-30-seconds) · [Try the first prompt](#first-prompt) · [Safety boundary](#safety-boundary-and-privacy) · [Validate](#development)

## Proof in one receipt

A real replay of [`Newtonsoft.Json 13.0.3`](examples/newtonsoft-json-13.0.3/README.md) produced:

> **PROVEN — exact commit.** The request resolved to an immutable commit, and the cached source passed integrity verification at that commit.

`NuGet · Newtonsoft.Json 13.0.3` → `JamesNK/Newtonsoft.Json` → `0a2e291c0d9c`

The [full Source Receipt](examples/newtonsoft-json-13.0.3/source-receipt.md) records the expected and observed commits, provenance class, integrity state, and verification time. It withholds absolute paths, remote URLs, aliases, catalog IDs, and path-derived identity suffixes by design.

![Short real-browser demonstration of package lookup, exact provenance, and fail-closed integrity evidence](docs/images/dashboard-demo.gif)

*A reproducible browser replay: package identity → exact commit → integrity failure that stays visibly blocked.*

![Inspect Dependency Source dashboard showing catalog health, verified source inventory, and repository provenance](docs/images/dashboard-overview.png)

*A read-only, localhost dashboard keeps source inventory, exact-version evidence, and operation health visible without exposing mutation controls.*

## What it solves

- **Exact-version debugging:** bind a package or requested ref to a tag or commit instead of silently substituting the default branch.
- **Reusable source grounding:** download once, then resolve the same verified source from Codex, Claude, and different repositories.
- **Source Receipts:** turn package/ref provenance into a copyable evidence card that makes proven, candidate, and blocked conclusions difficult to confuse.
- **Safer caching:** stage, validate, and atomically promote managed artifacts while retaining the last known-good copy.
- **Local observability:** inspect repositories, artifacts, package bindings, freshness, integrity, disk use, and operation history in an offline-capable dashboard.
- **Automation without internals:** integrate through a stable `resolve --json` contract rather than reading storage files directly.

Runtime code uses only the Python standard library. Public Git and GitHub repositories do not require the GitHub CLI.

## Evidence, not just source retrieval

[opensrc](https://github.com/vercel-labs/opensrc) optimizes convenient source retrieval across several package registries. [Context7](https://context7.com/docs/overview) retrieves version-specific documentation and examples. Both are useful, and both can complement this project.

Inspect Dependency Source optimizes a narrower trust question: *Can this finding prove that the inspected tree matches the dependency or ref that was requested?*

| Need | opensrc | Context7 | Inspect Dependency Source |
| --- | --- | --- | --- |
| Primary result | Cached source path | Documentation and examples | Verified source plus Source Receipt |
| Typical question | “Where is this package source?” | “How should I use this API?” | “Which commit proves this dependency behavior?” |
| Missing requested ref | [May fall back to the default branch](https://github.com/vercel-labs/opensrc/blob/f96078ac0a7ce3fb7d058d73ce65ff4b6606d765/packages/opensrc/cli/src/core/git.rs#L95-L127) | Not a source-tree resolver | Fails closed |
| Evidence boundary | Package/version-oriented cache entry | Documentation version | Expected commit, observed commit, provenance, integrity |
| Offline reuse | Cached source | Integration-dependent | Shared local catalog |

Inspect Dependency Source does not replace broad source retrieval or documentation lookup. Its differentiator is conservative proof: heuristic mappings stay candidates, unresolved mappings block exact-version claims, and exact evidence must agree at the commit level.

## Supported sources

- GitHub repositories.
- Generic Git remotes.
- Existing local source trees.
- Exact NuGet package versions, including repository-commit provenance when the package provides it.

npm, PyPI, Maven, and Cargo package resolution are not part of the first release. Their repositories can still be registered through Git or as local source.

## Install in 30 seconds

Install once for the current OS user so every project can invoke the same Skill:

```bash
npx skills add Tairitsua/inspect-dependency-source-skill --global
```

Remove `--global` for a project-local installation. The public package exposes exactly one Skill and is also listed on [skills.sh](https://skills.sh/tairitsua/inspect-dependency-source-skill).

For Claude Code's plugin marketplace:

```bash
claude plugin marketplace add Tairitsua/inspect-dependency-source-skill
claude plugin install inspect-dependency-source@inspect-dependency-source
```

### First prompt

After installation, tell your agent:

```text
Use the inspect-dependency-source skill to resolve Newtonsoft.Json 13.0.3,
inspect the exact source used by that package, and include a Source Receipt.
```

### Manual shared checkout

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

# Render share-conscious Markdown evidence for a finding.
python3 scripts/inspect_dependency_source.py resolve example --ref v1.2.3 --receipt

# Print the active localhost dashboard URL.
python3 scripts/inspect_dependency_source.py dashboard status
```

For an exact NuGet dependency:

```bash
python3 scripts/inspect_dependency_source.py package fetch-nuget Package.Id 1.2.3
python3 scripts/inspect_dependency_source.py resolve Package.Id --ref 1.2.3 --json
python3 scripts/inspect_dependency_source.py resolve Package.Id --ref 1.2.3 --receipt
```

For source that already exists locally:

```bash
python3 scripts/inspect_dependency_source.py repo add-local /absolute/path/to/source --alias example
python3 scripts/inspect_dependency_source.py resolve example --json
```

See [the CLI reference](references/cli.md) for every command and [source inspection workflows](references/workflows.md) for exact-ref, NuGet, local, offline, and recovery procedures.

## Trigger examples

- “Why does Newtonsoft.Json 13.0.3 behave this way internally?”
- “Resolve this SDK tag to the exact commit before debugging it.”
- “Cache this dependency source once so my other agents can reuse it.”
- “Prove which source revision supports this finding.”
- “Show me the shared dependency-source catalog and its integrity warnings.”
- “Register this existing local checkout without copying or modifying it.”

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

The included `SKILL.md` instructs an agent to resolve before downloading, treat `resolve --json` as the stable local integration contract, and attach `resolve --receipt` evidence to source-dependent findings. JSON identifies the repository, requested version or ref, selected artifact, resolved commit, verification state, source path, package provenance, and deterministic failure information. The receipt projects the same evidence into privacy-conscious Markdown. See the [public data contract](references/schema.md) for exact JSON fields, errors, enriched manifests, and operation events.

The source catalog is evidence, not authority by itself. Agents should:

1. Match the project's resolved dependency version.
2. Prefer `exact_commit` or `exact_tag` provenance.
3. Treat `heuristic_tag` as a lead requiring further verification.
4. Stop when an exact requested ref is unavailable instead of analyzing another branch.
5. Cite the ref, commit, provenance, and path in findings.
6. Include a Source Receipt when the finding depends on resolved source evidence.
7. Keep managed source artifacts read-only because other projects and agents share them.

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

## Safety boundary and privacy

- No telemetry is collected.
- Catalog metadata and source trees stay on the user's machine.
- On POSIX systems, catalog directories are restricted to the owning user (`0700`) and catalog state files use owner-only permissions (`0600`).
- Outbound traffic occurs only for explicit remote work such as GitHub registration,
  metadata refresh, source fetch, or package resolution.
- Credentials are removed from persisted and displayed remotes and redacted from errors, events, JSON, and APIs.
- Archive extraction rejects traversal, symlink escape, excessive member counts, and excessive size.
- Managed deletion is containment-checked and never removes a registered user-owned local tree.
- The dashboard never exposes arbitrary source files, environment variables, secrets, or mutation endpoints.
- Agents must preview the exact repository, managed artifacts, and registered local sources, then pause for explicit authorization before any `repo remove` operation. Execution uses only the returned exact repository ID and plan token; any intervening deletion-set or visible plan change forces a new preview and authorization. It also confirms every pre-existing preserved path still exists afterward. Purging managed cache requires a separate, explicit approval before `--yes` may be used.

Localhost-only does not mean share-safe. Review repository names, local paths, package versions, and operation history before publishing logs or sharing the dashboard screen.

Source Receipts are safer to share than raw JSON because they omit local paths, remotes, aliases, internal IDs, and path-derived identity suffixes. Repository and package names may still be sensitive; review them before publication.

## Repository map

| Path | Purpose |
| --- | --- |
| `SKILL.md` | Agent-facing triggers, workflow, evidence rules, and destructive-operation pause. |
| `scripts/` | Standard-library CLI, catalog, provider, dashboard, and receipt implementation. |
| `references/` | CLI, public schema, architecture, and provider-specific workflow contracts. |
| `examples/` | Real reproducible dependency evidence and checked-in output. |
| `showcase/` | Deterministic demo fixture, recorder, and regeneration instructions. |
| `assets/dashboard/` | Offline dashboard HTML, CSS, and JavaScript. |
| `tests/` | Unit, integration, safety, receipt, and real-browser validation. |

## Development

Runtime code must remain Python-standard-library-only. Development and browser validation may use optional tooling.

```bash
# Standard-library unit and integration tests.
python3 -m unittest discover -s tests -p 'test_*.py' -v

# Validate the Skill package metadata and structure.
python3 /path/to/skill-creator/scripts/quick_validate.py .

# Validate release metadata, documentation links, privacy, CI, and showcase.
python3 scripts/validate_release.py

# Optional browser validation (after `pip install playwright` and
# `playwright install chromium`).
python3 tests/browser_validation.py
```

Browser tests should cover bilingual persistence, repository switching, incremental operation timelines, responsive layouts, system themes, accessibility, security headers, and console/network errors.

Before publishing, confirm that the full test suite and Skill validation pass, the working tree is clean, and installed Codex and Claude copies pass the same Skill validation.

See [CHANGELOG.md](CHANGELOG.md) for release history and the [draft v1.0.0 notes](docs/release-notes/v1.0.0.md) for the publication narrative.

## Acknowledgements

- The cross-agent package follows the [Agent Skills specification](https://agentskills.io/specification).
- [opensrc](https://github.com/vercel-labs/opensrc) is a useful reference for broad package-source retrieval.
- [Context7](https://context7.com/docs/overview) is a useful complement for version-aware documentation and examples.
- The Claude marketplace layout follows [Anthropic's public Agent Skills marketplace](https://github.com/anthropics/skills/blob/main/.claude-plugin/marketplace.json).

## License

[MIT](LICENSE) © 2026 Momean.
