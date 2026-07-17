# Changelog

All notable changes to this project will be documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Shared user-level dependency source catalog for GitHub, generic Git, local source trees, and exact NuGet package versions.
- Stable `resolve --json` integration contract and privacy-conscious `resolve --receipt` evidence.
- Read-only bilingual localhost dashboard with responsive layouts and persistent view state.
- Target-preserving repository removal plans that enumerate managed cache and preserved local source trees before authorization.
- Reproducible dashboard showcase and release-readiness CI for Python, Skill packaging, and Chromium behavior.
- Cross-agent installation through the skills CLI and Claude Code marketplace metadata.

### Security

- Exact requested refs fail closed instead of falling back to a default branch.
- Source Receipts neutralize Markdown/control-character injection and withhold local identity details.
- Source Receipts block unversioned package evidence, reject malformed commit identities, and withhold file-remote paths.
- Archive extraction, managed promotion, and deletion enforce path-containment boundaries.
- Local source registration rejects overlap with catalog-owned storage.
- Managed-cache purge requires a safe preview, explicit authorization, and both destructive flags.
- Removal executes only against the exact planned repository ID and snapshot token, serializes against source acquisition, and post-verifies every pre-existing preserved path.
- Credentials are sanitized before persistence and redacted from diagnostics, JSON, and dashboard APIs.

### Known limitations

- Package-aware resolution currently supports NuGet only.
- Source Receipts still disclose dependency and repository names.
- Dashboard views contain local operational metadata and are not inherently share-safe.

[Unreleased]: https://github.com/Tairitsua/inspect-dependency-source-skill/compare/43c29e005372fe189587968787f5711a57a7832f...HEAD
