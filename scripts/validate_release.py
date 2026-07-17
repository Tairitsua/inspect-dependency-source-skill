#!/usr/bin/env python3
"""Validate the complete release surface without network access."""

from __future__ import annotations

import json
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MAX_PNG_BYTES = 3 * 1024 * 1024
REQUIRED_PNG_SIZE = (1440, 900)
PRIVATE_PATH_PATTERNS = (
    (
        "WSL user home",
        re.compile("/" + r"mnt/[A-Za-z]/" + r"Users/[^/\s\"'`<>]+"),
    ),
    (
        "Windows user home",
        re.compile(r"[A-Za-z]:\\" + r"Users\\[^\\\s\"'`<>]+"),
    ),
    (
        "slash-style Windows user home",
        re.compile(r"[A-Za-z]:" + "/" + r"Users/[^/\s\"'`<>]+"),
    ),
    (
        "macOS user home",
        re.compile(r"(?<![A-Za-z0-9:])" + "/" + r"Users/[^/\s\"'`<>]+"),
    ),
    ("Linux user home", re.compile("/" + r"home/[^/\s\"'`<>]+")),
)
SYNTHETIC_PRIVACY_FIXTURE = "release-privacy-fixture"
SYNTHETIC_PRIVACY_FIXTURE_FILES = frozenset({"tests/test_release_package.py"})
TOKEN_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
)
INLINE_LINK = re.compile(
    r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)"
)
REFERENCE_LINK = re.compile(r"^\[[^\]]+\]:\s*(?P<target>\S+)", re.MULTILINE)


@dataclass
class ValidationResult:
    passed: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def require(self, condition: bool, label: str, detail: str) -> None:
        (self.passed if condition else self.failures).append(
            label if condition else f"{label}: {detail}"
        )


def _read(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def _frontmatter(skill_text: str) -> tuple[str | None, str]:
    if not skill_text.startswith("---\n"):
        return None, ""
    try:
        header = skill_text.split("---\n", 2)[1]
    except IndexError:
        return None, ""
    name_match = re.search(r"^name:\s*(\S+)\s*$", header, re.MULTILINE)
    description_match = re.search(
        r"^description:\s*>-\s*\n(?P<body>(?:^[ \t]+.*\n?)+)",
        header,
        re.MULTILINE,
    )
    description = (
        " ".join(line.strip() for line in description_match.group("body").splitlines())
        if description_match
        else ""
    )
    return name_match.group(1) if name_match else None, description


def _local_link_failures(root: Path, documents: tuple[str, ...]) -> list[str]:
    failures: list[str] = []
    for relative in documents:
        source = root / relative
        text = source.read_text(encoding="utf-8")
        targets = [match.group("target") for match in INLINE_LINK.finditer(text)]
        targets.extend(match.group("target") for match in REFERENCE_LINK.finditer(text))
        for raw_target in targets:
            target = raw_target.strip("<>")
            if target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            path_text = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not path_text:
                continue
            resolved = (source.parent / path_text).resolve(strict=False)
            if not resolved.exists():
                failures.append(f"{relative} -> {target}")
    return failures


def _png_metadata(path: Path) -> tuple[tuple[int, int] | None, int]:
    if not path.is_file():
        return None, 0
    header = path.read_bytes()[:24]
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[8:12] != struct.pack(">I", 13)
        or header[12:16] != b"IHDR"
    ):
        return None, path.stat().st_size
    return struct.unpack(">II", header[16:24]), path.stat().st_size


def _release_text_files(root: Path) -> list[Path]:
    suffixes = {
        ".cfg",
        ".css",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
    text_names = {".gitignore", "LICENSE", "SKILL.md"}
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.relative_to(root).parts
        and (path.suffix.casefold() in suffixes or path.name in text_names)
    )


def _privacy_failures(root: Path, files: list[Path]) -> tuple[list[str], list[str]]:
    private_hits: list[str] = []
    token_hits: list[str] = []
    dynamic_needles = (
        (str(root.resolve()), "active repository root"),
        (str(Path.home().resolve()), "current user home"),
    )
    path_boundary = r"(?=$|[/\\\s\"'`<>),.;:\]}])"
    for path in files:
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        for needle, label in dynamic_needles:
            if needle and re.search(re.escape(needle.rstrip("/\\")) + path_boundary, text):
                private_hits.append(f"{relative}: {label}")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if (
                relative in SYNTHETIC_PRIVACY_FIXTURE_FILES
                and SYNTHETIC_PRIVACY_FIXTURE in line
            ):
                continue
            for label, pattern in PRIVATE_PATH_PATTERNS:
                if pattern.search(line):
                    private_hits.append(f"{relative}:{line_number}: {label}")
        for pattern in TOKEN_PATTERNS:
            if pattern.search(text):
                token_hits.append(f"{relative}: {pattern.pattern}")
    return sorted(set(private_hits)), sorted(set(token_hits))


def validate_repository(root: Path = ROOT) -> ValidationResult:
    result = ValidationResult()
    required_files = (
        "SKILL.md",
        "README.md",
        "README.zh-CN.md",
        "LICENSE",
        "CHANGELOG.md",
        "agents/openai.yaml",
        ".claude-plugin/marketplace.json",
        ".github/workflows/release-readiness.yml",
        "docs/images/dashboard-overview.png",
        "docs/release-notes/v1.0.0.md",
        "examples/newtonsoft-json-13.0.3/README.md",
        "tests/requirements.txt",
    )
    missing = [relative for relative in required_files if not (root / relative).is_file()]
    result.require(not missing, "required release files", f"missing {missing}")
    if missing:
        return result
    retired_files = (
        "scripts/_catalog_" + "receipt.py",
        "tests/test_source_" + "receipt.py",
        "examples/newtonsoft-json-13.0.3/source-" + "receipt.md",
        "docs/images/dashboard-" + "demo.gif",
        "showcase/README.md",
        "showcase/record_dashboard.py",
        "showcase/requirements.txt",
    )
    unexpected = [relative for relative in retired_files if (root / relative).exists()]
    result.require(
        not unexpected,
        "retired release files",
        f"unexpected {unexpected}",
    )

    skill = _read(root, "SKILL.md")
    name, description = _frontmatter(skill)
    result.require(name == "inspect-dependency-source", "Skill name", f"found {name!r}")
    result.require(
        1 <= len(description) <= 1024
        and "exact" in description.casefold()
        and "Do not use" in description,
        "Skill trigger contract",
        "description must contain positive and negative triggers within 1024 characters",
    )
    result.require(
        all(
            marker in skill
            for marker in (
                "resolve --json",
                "package fetch-nuget",
                "source_path",
                "read-only",
            )
        ),
        "exact-source workflow",
        "resolve, acquisition, source-path, or read-only guidance is absent",
    )
    removal_reference = _read(root, "references/cli.md")
    result.require(
        "repo remove <query> --plan --json" in removal_reference
        and "repo remove <repository.id>" in removal_reference
        and "--plan-token <plan_token>" in removal_reference
        and "exists_after: true" in removal_reference,
        "destructive-operation gate",
        "advanced CLI guidance lacks plan, exact-ID execution, or post-verification",
    )

    try:
        marketplace = json.loads(_read(root, ".claude-plugin/marketplace.json"))
    except json.JSONDecodeError as exc:
        result.require(False, "Claude marketplace manifest", f"invalid JSON: {exc}")
        marketplace = {}
    plugins = marketplace.get("plugins")
    plugin = plugins[0] if isinstance(plugins, list) and len(plugins) == 1 else {}
    marketplace_ok = (
        marketplace.get("$schema")
        == "https://json.schemastore.org/claude-code-marketplace.json"
        and marketplace.get("name") == "inspect-dependency-source"
        and marketplace.get("owner", {}).get("name") == "Tairitsua"
        and plugin.get("name") == "inspect-dependency-source"
        and plugin.get("source") == "./"
        and plugin.get("strict") is False
        and plugin.get("skills") == ["./"]
        and plugin.get("license") == "MIT"
    )
    result.require(marketplace_ok, "Claude marketplace manifest", "unexpected manifest contract")

    readme = _read(root, "README.md")
    readme_zh = _read(root, "README.zh-CN.md")
    required_readme_markers = (
        "npx skills add Tairitsua/inspect-dependency-source-skill --global",
        "claude plugin marketplace add Tairitsua/inspect-dependency-source-skill",
        "https://skills.sh/b/tairitsua/inspect-dependency-source-skill",
        "AGENTS.md",
        "@AGENTS.md",
        "## Dependency source inspection",
        "resolve --json",
        "docs/images/dashboard-overview.png",
        "examples/newtonsoft-json-13.0.3/README.md",
    )
    result.require(
        all(marker in readme for marker in required_readme_markers),
        "English README release path",
        "global install, project routing, exact-source, example, or dashboard marker is absent",
    )
    result.require(
        required_readme_markers[0] in readme_zh
        and "AGENTS.md" in readme_zh
        and "@AGENTS.md" in readme_zh
        and "source_path" in readme_zh
        and "resolve --json" in readme_zh
        and "docs/images/dashboard-overview.png" in readme_zh,
        "Chinese README release path",
        "global install, project routing, exact-source, or dashboard marker is absent",
    )

    workflow = _read(root, ".github/workflows/release-readiness.yml")
    workflow_markers = (
        'python: ["3.11", "3.14"]',
        "persist-credentials: false",
        "python -m unittest discover",
        "tests/test_catalog_core.py CliContractTests",
        "tests/test_removal_safety.py",
        "agentskills validate",
        "skills@1.5.19",
        "tests/requirements.txt",
        "tests/browser_validation.py",
        "scripts/validate_release.py",
    )
    result.require(
        all(marker in workflow for marker in workflow_markers)
        and workflow.count("persist-credentials: false") == 3,
        "release-readiness workflow",
        "matrix, package, browser, credential, or validator gate is absent",
    )

    png_size, png_bytes = _png_metadata(root / "docs/images/dashboard-overview.png")
    result.require(
        png_size == REQUIRED_PNG_SIZE and 0 < png_bytes <= MAX_PNG_BYTES,
        "dashboard overview artifact",
        f"found dimensions={png_size}, bytes={png_bytes}",
    )

    release_documents = tuple(
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.md"))
        if ".git" not in path.relative_to(root).parts
    )
    broken_links = _local_link_failures(root, release_documents)
    result.require(not broken_links, "local documentation links", f"broken {broken_links}")

    private_hits, token_hits = _privacy_failures(root, _release_text_files(root))
    result.require(
        not private_hits and not token_hits,
        "release leakage scan",
        f"private paths={private_hits}, token patterns={token_hits}",
    )

    retired_markers = (
        "Source " + "Receipt",
        "source-" + "receipt",
        "--" + "receipt",
        "share-" + "conscious",
    )
    formal_verdict_pattern = re.compile(
        r"\b(?:" + "|".join(("PRO" + "VEN", "CANDI" + "DATE", "BLOCK" + "ED")) + r")\b"
    )
    retired_hits: list[str] = []
    for path in _release_text_files(root):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in retired_markers) or formal_verdict_pattern.search(text):
            retired_hits.append(path.relative_to(root).as_posix())
    result.require(
        not retired_hits,
        "retired evidence surface",
        f"obsolete terminology remains in {sorted(set(retired_hits))}",
    )
    changelog = _read(root, "CHANGELOG.md")
    notes = _read(root, "docs/release-notes/v1.0.0.md")
    result.require(
        "## [Unreleased]" in changelog
        and "### Security" in changelog
        and "Draft only" in notes,
        "release narrative",
        "changelog security section or draft-release warning is absent",
    )
    return result


def main() -> int:
    root = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else ROOT
    result = validate_repository(root)
    if result.failures:
        print("Release package validation: FAIL", file=sys.stderr)
        for failure in result.failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"Release package validation: PASS ({len(result.passed)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
