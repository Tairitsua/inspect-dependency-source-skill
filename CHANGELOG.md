# Changelog

All notable changes to this project will be documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Reusable user-level dependency source catalog for GitHub, generic Git, local source trees, and exact NuGet package versions.
- Stable local `resolve --json` integration contract with validated source paths, provenance, and verification state.
- Read-only bilingual localhost dashboard with responsive layouts and persistent view state.
- Target-preserving repository removal plans that enumerate managed cache and preserved local source trees before authorization.
- Static dashboard overview and release-readiness CI for Python, Skill packaging, and real Chromium behavior.
- User-level installation through the skills CLI and Claude Code marketplace metadata, with project-level `AGENTS.md` routing guidance.

### Security

- Exact requested refs fail closed instead of falling back to a default branch.
- Archive extraction, managed promotion, and deletion enforce path-containment boundaries.
- Local source registration rejects overlap with catalog-owned storage.
- Managed-cache purge requires a safe preview, explicit authorization, and both destructive flags.
- Removal executes only against the exact planned repository ID and snapshot token, serializes against source acquisition, and post-verifies every pre-existing preserved path.
- Credentials are sanitized before persistence and redacted from diagnostics, JSON, and dashboard APIs.

### Known limitations

- Package-aware resolution currently supports NuGet only.
- CLI JSON and dashboard views contain local paths and operational metadata and must be treated as sensitive local data.
