#!/usr/bin/env python3
"""Validate the complete public release surface without network access."""

from __future__ import annotations

import json
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MAX_GIF_BYTES = 8 * 1024 * 1024
REQUIRED_GIF_SIZE = (1120, 630)
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
SYNTHETIC_PRIVACY_FIXTURE_FILES = frozenset(
    {"tests/test_release_package.py", "tests/test_source_receipt.py"}
)
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


def _gif_metadata(path: Path) -> tuple[tuple[int, int] | None, int]:
    if not path.is_file():
        return None, 0
    header = path.read_bytes()[:10]
    if len(header) != 10 or header[:6] not in {b"GIF87a", b"GIF89a"}:
        return None, path.stat().st_size
    return struct.unpack("<HH", header[6:10]), path.stat().st_size


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
        "docs/images/dashboard-demo.gif",
        "docs/images/dashboard-overview.png",
        "docs/release-notes/v1.0.0.md",
        "examples/newtonsoft-json-13.0.3/README.md",
        "examples/newtonsoft-json-13.0.3/source-receipt.md",
        "showcase/README.md",
        "showcase/record_dashboard.py",
        "showcase/requirements.txt",
    )
    missing = [relative for relative in required_files if not (root / relative).is_file()]
    result.require(not missing, "required release files", f"missing {missing}")
    if missing:
        return result

    skill = _read(root, "SKILL.md")
    name, description = _frontmatter(skill)
    result.require(name == "inspect-dependency-source", "Skill name", f"found {name!r}")
    result.require(
        1 <= len(description) <= 1024
        and "Manage and reuse exact" in description
        and "Do not use" in description,
        "Skill trigger contract",
        "description must contain positive and negative triggers within 1024 characters",
    )
    result.require(
        "repo remove <query> --plan --json" in skill
        and "repo remove <repository.id>" in skill
        and "--plan-token <plan_token>" in skill
        and "exists_after: true" in skill,
        "destructive-operation gate",
        "plan, exact-ID execution, or post-verification instruction is absent",
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
        "docs/images/dashboard-demo.gif",
        "examples/newtonsoft-json-13.0.3/source-receipt.md",
    )
    result.require(
        all(marker in readme for marker in required_readme_markers),
        "English README release path",
        "install, badge, receipt, or showcase marker is absent",
    )
    result.require(
        "docs/images/dashboard-demo.gif" in readme_zh
        and required_readme_markers[0] in readme_zh,
        "Chinese README release path",
        "install or showcase marker is absent",
    )

    workflow = _read(root, ".github/workflows/release-readiness.yml")
    workflow_markers = (
        'python: ["3.11", "3.14"]',
        "persist-credentials: false",
        "python -m unittest discover",
        "tests/test_removal_safety.py",
        "agentskills validate",
        "skills@1.5.19",
        "tests/browser_validation.py",
        "showcase/record_dashboard.py",
        "apt-get install --no-install-recommends -y ffmpeg",
        "--screenshot-output",
        "scripts/validate_release.py",
    )
    result.require(
        all(marker in workflow for marker in workflow_markers)
        and workflow.count("persist-credentials: false") == 3,
        "release-readiness workflow",
        "matrix, package, browser, credential, or validator gate is absent",
    )

    gif_size, gif_bytes = _gif_metadata(root / "docs/images/dashboard-demo.gif")
    result.require(
        gif_size == REQUIRED_GIF_SIZE and 0 < gif_bytes <= MAX_GIF_BYTES,
        "dashboard showcase artifact",
        f"found dimensions={gif_size}, bytes={gif_bytes}",
    )

    png_size, png_bytes = _png_metadata(root / "docs/images/dashboard-overview.png")
    result.require(
        png_size == REQUIRED_PNG_SIZE and 0 < png_bytes <= MAX_PNG_BYTES,
        "dashboard overview artifact",
        f"found dimensions={png_size}, bytes={png_bytes}",
    )

    public_documents = tuple(
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.md"))
        if ".git" not in path.relative_to(root).parts
    )
    broken_links = _local_link_failures(root, public_documents)
    result.require(not broken_links, "local documentation links", f"broken {broken_links}")

    private_hits, token_hits = _privacy_failures(root, _release_text_files(root))
    result.require(
        not private_hits and not token_hits,
        "public artifact privacy scan",
        f"private paths={private_hits}, token patterns={token_hits}",
    )

    receipt = _read(root, "examples/newtonsoft-json-13.0.3/source-receipt.md")
    result.require(
        "PROVEN" in receipt
        and "0a2e291c0d9c0c7675d445703e51750363a549ef" in receipt
        and "Source path" not in receipt,
        "checked-in Source Receipt",
        "proof state, exact commit, or share-conscious projection is invalid",
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
