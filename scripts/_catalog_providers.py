"""Source-provider adapters for GitHub, generic Git, local trees, and NuGet."""

from __future__ import annotations

import json
import io
import itertools
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

from _catalog_models import (
    CapabilityError,
    NuGetMetadata,
    ProviderError,
    RefNotFoundError,
    ResolutionKind,
    ResolvedRef,
    ValidationError,
)
from _catalog_paths import is_link_or_reparse, redact_text, sanitize_remote


USER_AGENT = "inspect-dependency-source/1.0"
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_NUGET_MEMBERS = 20_000
MAX_NUGET_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_NUSPEC_BYTES = 4 * 1024 * 1024
MAX_NUSPEC_COMPRESSION_RATIO = 200
MAX_GIT_LISTING_BYTES = 16 * 1024 * 1024
SOURCE_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "pom.xml",
    "Directory.Build.props",
    "global.json",
)


def validate_github_name(value: str) -> str:
    """Validate and normalize an ``owner/repository`` GitHub identity."""
    candidate = value.strip().removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate):
        raise ValidationError("GitHub repositories must use the owner/repository form.")
    if any(part in {".", ".."} for part in candidate.split("/")):
        raise ValidationError("GitHub repository identity contains an invalid path segment.")
    return candidate


def github_name_from_remote(remote: str | None) -> str | None:
    """Return an owner/repository identity for a recognized GitHub remote."""
    clean = sanitize_remote(remote)
    if not clean:
        return None
    match = re.match(
        r"^(?:https?://|ssh://git@|git@)?github\.com(?::|/)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
        clean,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return validate_github_name(f"{match.group(1)}/{match.group(2)}")
    except ValidationError:
        return None


def run_git(arguments: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run Git without a shell, with bounded output and deterministic diagnostics."""
    return run_git_bounded(arguments, cwd=cwd, check=check)


def run_git_bounded(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    max_bytes: int = MAX_GIT_LISTING_BYTES,
    timeout_seconds: float = 600,
) -> subprocess.CompletedProcess[str]:
    """Run Git while bounding merged output before it reaches memory."""
    if shutil.which("git") is None:
        raise CapabilityError("Git is required for this command but was not found on PATH.")
    if max_bytes <= 0:
        raise ValidationError("Git output limit must be positive.")
    try:
        process = subprocess.Popen(
            ["git", *arguments],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_git_environment(),
        )
    except OSError as exc:
        raise CapabilityError(f"Unable to execute Git: {redact_text(str(exc))}") from exc
    timed_out = threading.Event()

    def kill_for_timeout() -> None:
        timed_out.set()
        try:
            process.kill()
        except OSError:
            pass

    timer = threading.Timer(max(timeout_seconds, 0), kill_for_timeout)
    timer.daemon = True
    timer.start()
    output = bytearray()
    try:
        assert process.stdout is not None
        while chunk := process.stdout.read(64 * 1024):
            output.extend(chunk)
            if len(output) > max_bytes:
                process.kill()
                process.wait()
                raise ProviderError(
                    f"Git command output exceeds the {max_bytes}-byte safety limit."
                )
        return_code = process.wait()
    finally:
        timer.cancel()
        if process.stdout is not None:
            process.stdout.close()
    if timed_out.is_set():
        raise ProviderError(f"Git command timed out after {timeout_seconds:g} seconds.")
    text_output = output.decode("utf-8", errors="replace")
    completed = subprocess.CompletedProcess(
        ["git", *arguments], return_code, text_output, ""
    )
    if check and return_code != 0:
        detail = redact_text(text_output.strip())
        raise ProviderError(f"Git command failed: {detail or 'unknown Git error'}")
    return completed


def _git_environment() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:http:ssh:git:file",
        "LC_ALL": "C",
        "LANG": "C",
    }


class GitHubClient:
    """Minimal direct GitHub REST client with optional user authentication."""

    def __init__(self, *, token: str | None = None, api_base: str = "https://api.github.com") -> None:
        self.token = token if token is not None else discover_github_token()
        self.api_base = api_base.rstrip("/")

    def _request(self, path: str, *, accept: str = "application/vnd.github+json") -> urllib.response.addinfourl:
        headers = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.api_base}{path}", headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code in {404, 422}:
                raise RefNotFoundError("GitHub could not find the requested repository or ref.") from exc
            message = _http_error_message(exc)
            raise ProviderError(f"GitHub request failed ({exc.code}): {message}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"GitHub request failed: {redact_text(str(exc))}") from exc

    def repository(self, full_name: str) -> dict[str, Any]:
        """Return sanitized repository metadata."""
        name = validate_github_name(full_name)
        with self._request(f"/repos/{name}") as response:
            payload = _read_json_response(response)
        return {
            "full_name": validate_github_name(str(payload["full_name"])),
            "display_name": str(payload.get("name") or name.split("/", 1)[1]),
            "remote_url": sanitize_remote(str(payload.get("clone_url") or f"https://github.com/{name}.git")),
            "default_branch": str(payload.get("default_branch") or ""),
            "private": bool(payload.get("private")),
        }

    def list_tags(self, full_name: str, *, limit: int = 1000) -> list[dict[str, str | None]]:
        """List GitHub tags with their peeled commit identifiers."""
        name = validate_github_name(full_name)
        tags: list[dict[str, str | None]] = []
        page = 1
        while len(tags) < limit:
            per_page = min(100, limit - len(tags))
            with self._request(f"/repos/{name}/tags?per_page={per_page}&page={page}") as response:
                payload = _read_json_response(response)
            if not isinstance(payload, list):
                raise ProviderError("GitHub returned an invalid tag response.")
            for item in payload:
                if isinstance(item, dict) and item.get("name"):
                    commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
                    tags.append({"name": str(item["name"]), "commit_sha": commit.get("sha")})
            if len(payload) < per_page:
                break
            page += 1
        return tags

    def resolve_ref(self, full_name: str, requested_ref: str) -> ResolvedRef:
        """Resolve exactly one GitHub ref; never substitute the default branch."""
        name = validate_github_name(full_name)
        ref = validate_ref(requested_ref)
        encoded = urllib.parse.quote(ref, safe="")
        try:
            with self._request(f"/repos/{name}/commits/{encoded}") as response:
                payload = _read_json_response(response)
        except RefNotFoundError as exc:
            raise RefNotFoundError(f"GitHub ref does not exist: {name}@{ref}") from exc
        sha = str(payload.get("sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise ProviderError("GitHub returned an invalid commit identifier.")
        resolution = ResolutionKind.EXACT_COMMIT if _looks_like_commit(ref) else ResolutionKind.EXACT_TAG
        return ResolvedRef(ref, ref, sha.lower(), resolution)

    def is_tag(self, full_name: str, requested_ref: str) -> bool:
        """Authoritatively test whether an exact GitHub tag ref exists."""
        name = validate_github_name(full_name)
        ref = validate_ref(requested_ref)
        encoded = urllib.parse.quote(f"tags/{ref}", safe="")
        try:
            with self._request(f"/repos/{name}/git/ref/{encoded}") as response:
                payload = _read_json_response(response)
        except RefNotFoundError:
            return False
        return isinstance(payload, dict) and str(payload.get("ref") or "") == f"refs/tags/{ref}"

    def default_branch(self, full_name: str) -> str:
        """Return the provider-declared default branch name."""
        branch = self.repository(full_name).get("default_branch")
        if not branch:
            raise ProviderError("GitHub did not declare a default branch.")
        return validate_ref(str(branch))

    def download_archive(self, full_name: str, ref: str, destination: Path, *, max_bytes: int) -> int:
        """Stream one exact GitHub tar archive to a staging path."""
        name = validate_github_name(full_name)
        encoded = urllib.parse.quote(validate_ref(ref), safe="")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._request(
            f"/repos/{name}/tarball/{encoded}", accept="application/vnd.github+json"
        ) as response, destination.open("wb") as handle:
            return _copy_limited(response, handle, max_bytes=max_bytes)


def discover_github_token() -> str | None:
    """Return an opt-in GitHub token without making authentication mandatory."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token.strip()
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        completed = subprocess.run(
            [gh, "auth", "token"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = completed.stdout.strip()
    return token if completed.returncode == 0 and token else None


def validate_git_remote(value: str, *, allow_file: bool = True) -> str:
    """Validate a generic Git URL and return its sanitized persisted form."""
    candidate = value.strip()
    if (
        not candidate
        or "::" in candidate
        or any(character in candidate for character in ("\n", "\r", "\0"))
    ):
        raise ValidationError("Git remote must be a non-empty URL.")
    if candidate.startswith("-"):
        raise ValidationError("Git remote cannot begin with an option prefix.")
    local_candidate = Path(candidate).expanduser()
    if local_candidate.is_absolute():
        if not allow_file:
            raise ValidationError("Package-declared Git remotes cannot use local filesystem paths.")
        return local_candidate.resolve(strict=False).as_uri()
    clean = sanitize_remote(candidate)
    if not clean:
        raise ValidationError("Git remote is invalid.")
    if "://" in clean:
        parsed = urllib.parse.urlsplit(clean)
        allowed = {"https", "http", "ssh", "git"}
        if allow_file:
            allowed.add("file")
        if parsed.scheme.casefold() not in allowed:
            raise ValidationError(f"Unsupported Git remote scheme: {parsed.scheme or '(missing)'}")
        if parsed.scheme.casefold() == "file":
            if parsed.hostname not in {None, "", "localhost"} or not Path(parsed.path).is_absolute():
                raise ValidationError("File Git remotes must use an absolute local file URL.")
        elif not parsed.hostname:
            raise ValidationError("Network Git remotes must include a hostname.")
    elif not re.fullmatch(
        r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+:[A-Za-z0-9._~!$&'()*+,;=:@%/+-]+",
        clean,
    ):
        raise ValidationError("Git remote must use HTTP(S), SSH, Git, file URL, or strict SCP syntax.")
    return clean


def normalized_git_remote_identity(remote_url: str) -> str:
    """Normalize only scheme and host while preserving case-sensitive Git path identity."""
    clean = validate_git_remote(remote_url)
    if "://" in clean:
        parsed = urllib.parse.urlsplit(clean)
        host = parsed.hostname.casefold() if parsed.hostname else ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        if parsed.username:
            host = f"{parsed.username}@{host}"
        return urllib.parse.urlunsplit(
            (parsed.scheme.casefold(), host, parsed.path, "", "")
        )
    user_host, path = clean.split(":", 1)
    if "@" in user_host:
        user, host = user_host.rsplit("@", 1)
        user_host = f"{user}@{host.casefold()}"
    else:
        user_host = user_host.casefold()
    return f"{user_host}:{path}"


def validate_ref(value: str) -> str:
    """Reject option injection, control characters, and path-traversal-like refs."""
    ref = value.strip()
    if (
        not ref
        or len(ref) > 512
        or ref.startswith("-")
        or any(character.isspace() or ord(character) < 32 for character in ref)
        or ".." in ref
        or "@{" in ref
        or ref.endswith(".")
        or ref.endswith("/")
        or "\\" in ref
    ):
        raise ValidationError(f"Invalid Git ref: {value!r}")
    return ref


def generic_default_branch(remote_url: str) -> str:
    """Read the symbolic remote HEAD without fetching source content."""
    remote = validate_git_remote(remote_url)
    completed = run_git(["ls-remote", "--symref", remote, "HEAD"], check=False)
    if completed.returncode != 0:
        detail = redact_text((completed.stderr or completed.stdout).strip())
        raise ProviderError(f"Unable to inspect the remote default branch: {detail}")
    match = re.search(r"^ref:\s+refs/heads/(\S+)\s+HEAD$", completed.stdout, flags=re.MULTILINE)
    if not match:
        raise ProviderError("The Git remote does not advertise a symbolic default branch.")
    return validate_ref(match.group(1))


def generic_tags(remote_url: str) -> list[dict[str, str | None]]:
    """List tags from a generic Git remote without cloning it."""
    remote = validate_git_remote(remote_url)
    completed = run_git_bounded(["ls-remote", "--tags", remote], check=False)
    if completed.returncode != 0:
        detail = redact_text((completed.stderr or completed.stdout).strip())
        raise ProviderError(f"Unable to list Git tags: {detail}")
    tags: dict[str, dict[str, str | None]] = {}
    for line in completed.stdout.splitlines():
        if "\trefs/tags/" not in line:
            continue
        sha, raw_ref = line.split("\t", 1)
        name = raw_ref.removeprefix("refs/tags/")
        peeled = name.endswith("^{}")
        name = name.removesuffix("^{}")
        if peeled or name not in tags:
            tags[name] = {"name": name, "commit_sha": sha.lower()}
    return sorted(tags.values(), key=lambda item: str(item["name"]).casefold())


def resolve_generic_ref(remote_url: str, requested_ref: str) -> ResolvedRef:
    """Resolve a generic remote ref to its advertised commit without substitution."""
    remote = validate_git_remote(remote_url)
    ref = validate_ref(requested_ref)
    if _looks_like_commit(ref):
        return ResolvedRef(ref, ref, ref.lower(), ResolutionKind.EXACT_COMMIT)
    patterns = [ref, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"]
    completed = run_git(["ls-remote", remote, *patterns], check=False)
    if completed.returncode != 0:
        detail = redact_text((completed.stderr or completed.stdout).strip())
        raise ProviderError(f"Unable to resolve Git ref '{ref}': {detail}")
    matches: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, advertised = line.split("\t", 1)
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            matches[advertised] = sha.lower()
    peeled_tag = matches.get(f"refs/tags/{ref}^{{}}")
    tag = peeled_tag or matches.get(f"refs/tags/{ref}")
    if tag:
        return ResolvedRef(ref, ref, tag, ResolutionKind.EXACT_TAG)
    branch = matches.get(f"refs/heads/{ref}") or matches.get(ref)
    if branch:
        return ResolvedRef(ref, ref, branch, ResolutionKind.EXACT_COMMIT)
    raise RefNotFoundError(f"Git ref does not exist: {ref}")


def fetch_generic_git(remote_url: str, ref: str, destination: Path) -> str:
    """Fetch and check out one exact ref into an empty staging directory."""
    remote = validate_git_remote(remote_url)
    exact_ref = validate_ref(ref)
    destination.mkdir(parents=True, exist_ok=False)
    run_git(["init", "--quiet"], cwd=destination)
    run_git(["remote", "add", "origin", remote], cwd=destination)
    completed = run_git(
        ["fetch", "--quiet", "--depth", "1", "origin", exact_ref], cwd=destination, check=False
    )
    if completed.returncode != 0:
        detail = redact_text((completed.stderr or completed.stdout).strip())
        if _git_missing_ref(detail):
            raise RefNotFoundError(f"Git ref does not exist: {exact_ref}")
        raise ProviderError(f"Unable to fetch Git ref '{exact_ref}': {detail}")
    run_git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=destination)
    commit = run_git(["rev-parse", "HEAD"], cwd=destination).stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ProviderError("Git returned an invalid checked-out commit identifier.")
    return commit


def inspect_local_source(source_path: str | Path) -> dict[str, Any]:
    """Inspect one user-owned source tree without changing it."""
    raw = Path(source_path).expanduser()
    if not raw.is_absolute():
        raise ValidationError("Local source paths must be absolute.")
    lexical = Path(os.path.abspath(raw))
    if is_link_or_reparse(lexical):
        raise ValidationError("Local source path cannot be a link or reparse point.")
    canonical = lexical.resolve(strict=False)
    if not canonical.is_dir():
        raise ValidationError(f"Local source directory does not exist: {canonical}")
    markers = detect_markers(canonical)
    git = inspect_local_git(canonical)
    return {
        "path": str(raw.absolute()),
        "canonical_path": str(canonical),
        "display_name": canonical.name,
        "detected_version": detect_version(canonical),
        "markers": markers,
        **git,
    }


def inspect_local_git(source_path: Path) -> dict[str, Any]:
    """Return a non-mutating Git snapshot when the tree belongs to a repository."""
    if shutil.which("git") is None:
        return {"branch": None, "commit_sha": None, "dirty": None, "remote_url": None}
    inside = run_git(["rev-parse", "--is-inside-work-tree"], cwd=source_path, check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"branch": None, "commit_sha": None, "dirty": None, "remote_url": None}
    commit = run_git(["rev-parse", "HEAD"], cwd=source_path, check=False)
    branch = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=source_path, check=False)
    status = run_git(["status", "--porcelain", "--untracked-files=normal"], cwd=source_path, check=False)
    remote = run_git(["remote", "get-url", "origin"], cwd=source_path, check=False)
    return {
        "branch": branch.stdout.strip() or None,
        "commit_sha": commit.stdout.strip().lower() or None,
        "dirty": bool(status.stdout) if status.returncode == 0 else None,
        "remote_url": sanitize_remote(remote.stdout.strip()) if remote.returncode == 0 else None,
    }


def detect_markers(source_path: Path) -> list[str]:
    """Return bounded implementation markers present at a source root."""
    markers = [
        marker
        for marker in SOURCE_MARKERS
        if (source_path / marker).exists()
        and not is_link_or_reparse(source_path / marker)
    ]
    for pattern in ("*.sln", "*.slnx", "*.csproj", "*.fsproj", "*.go"):
        if next(
            (
                candidate
                for candidate in source_path.glob(pattern)
                if not is_link_or_reparse(candidate)
            ),
            None,
        ):
            markers.append(pattern)
    return markers


def is_probable_source_root(source_path: Path) -> bool:
    """Conservatively identify repository/package roots during local scanning."""
    return bool(detect_markers(source_path))


def scan_local_roots(base_path: str | Path, *, max_depth: int, excluded: Iterable[Path] = ()) -> list[Path]:
    """Discover source roots without following symlink directories."""
    base = Path(base_path).expanduser()
    if not base.is_absolute():
        raise ValidationError("Scan paths must be absolute.")
    if is_link_or_reparse(base):
        raise ValidationError("Scan root cannot be a link or reparse point.")
    root = base.resolve(strict=False)
    if not root.is_dir():
        raise ValidationError(f"Scan directory does not exist: {root}")
    if not 0 <= max_depth <= 20:
        raise ValidationError("Scan max depth must be between 0 and 20.")
    blocked = [path.resolve(strict=False) for path in excluded]
    found: list[Path] = []
    for current, dirnames, _ in os.walk(root, followlinks=False):
        path = Path(current)
        depth = len(path.relative_to(root).parts)
        dirnames[:] = [
            name
            for name in dirnames
            if not is_link_or_reparse(path / name)
            and not any(_is_relative_to((path / name).resolve(strict=False), excluded_root) for excluded_root in blocked)
        ]
        if is_probable_source_root(path):
            found.append(path)
            dirnames[:] = [name for name in dirnames if name not in {".git", "node_modules", "bin", "obj"}]
        if depth >= max_depth:
            dirnames.clear()
    return found


def detect_version(source_path: Path) -> str | None:
    """Detect a declared version from common dependency metadata."""
    package_json = source_path / "package.json"
    if package_json.is_file():
        try:
            payload = json.loads(
                _read_text_limited(package_json, 4 * 1024 * 1024, root=source_path)
            )
            if isinstance(payload, dict) and payload.get("version"):
                return str(payload["version"])
        except (OSError, json.JSONDecodeError, UnicodeError, ProviderError):
            pass
    pyproject = source_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = _read_text_limited(pyproject, 1024 * 1024, root=source_path)
            match = re.search(r"(?m)^\s*version\s*=\s*['\"]([^'\"]+)['\"]", text)
            if match:
                return match.group(1)
        except (OSError, UnicodeError, ProviderError):
            pass
    project_files = [path.name for path in itertools.islice(source_path.glob("*.csproj"), 10)]
    for name in ("Directory.Build.props", *project_files):
        candidate = source_path / name
        if candidate.is_file():
            try:
                root = ElementTree.fromstring(
                    _read_text_limited(candidate, 4 * 1024 * 1024, root=source_path)
                )
                for tag in ("Version", "VersionPrefix", "PackageVersion"):
                    node = next((item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == tag), None)
                    if node is not None and node.text and node.text.strip():
                        return node.text.strip()
            except (OSError, ElementTree.ParseError, ProviderError):
                continue
    return None


class NuGetClient:
    """Read exact package provenance from NuGet's flat-container API."""

    def __init__(self, base_url: str = "https://api.nuget.org/v3-flatcontainer") -> None:
        self.base_url = base_url.rstrip("/")

    def metadata(self, package_id: str, version: str) -> NuGetMetadata:
        """Download one exact package and parse only its nuspec provenance."""
        package = validate_package_component(package_id, label="package ID")
        package_version = validate_package_component(version, label="package version")
        lower_id = package.casefold()
        lower_version = package_version.casefold()
        url = f"{self.base_url}/{urllib.parse.quote(lower_id)}/{urllib.parse.quote(lower_version)}/{urllib.parse.quote(lower_id)}.{urllib.parse.quote(lower_version)}.nupkg"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                content_length = int(response.headers.get("Content-Length", "0") or 0)
                if content_length > MAX_METADATA_BYTES:
                    raise ProviderError("NuGet package metadata archive exceeds the safety limit.")
                with tempfile.SpooledTemporaryFile(max_size=MAX_METADATA_BYTES) as handle:
                    _copy_limited(response, handle, max_bytes=MAX_METADATA_BYTES)
                    handle.seek(0)
                    with zipfile.ZipFile(handle) as archive:
                        members = archive.infolist()
                        if len(members) > MAX_NUGET_MEMBERS:
                            raise ProviderError("NuGet package contains too many archive members.")
                        if sum(max(member.file_size, 0) for member in members) > MAX_NUGET_EXPANDED_BYTES:
                            raise ProviderError("NuGet package expanded size exceeds the safety limit.")
                        nuspec_members = [
                            member
                            for member in members
                            if member.filename.casefold().endswith(".nuspec")
                        ]
                        if len(nuspec_members) != 1:
                            raise ProviderError("NuGet package does not contain exactly one nuspec file.")
                        nuspec_member = nuspec_members[0]
                        if nuspec_member.file_size > MAX_NUSPEC_BYTES:
                            raise ProviderError("NuGet nuspec exceeds the safety limit.")
                        ratio = nuspec_member.file_size / max(nuspec_member.compress_size, 1)
                        if ratio > MAX_NUSPEC_COMPRESSION_RATIO:
                            raise ProviderError("NuGet nuspec compression ratio exceeds the safety limit.")
                        with archive.open(nuspec_member) as source:
                            output = io.BytesIO()
                            _copy_limited(source, output, max_bytes=MAX_NUSPEC_BYTES)
                            nuspec = output.getvalue()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RefNotFoundError(f"NuGet package does not exist: {package} {package_version}") from exc
            raise ProviderError(f"NuGet request failed ({exc.code}): {_http_error_message(exc)}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, zipfile.BadZipFile) as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"NuGet request failed: {redact_text(str(exc))}") from exc
        return parse_nuspec(nuspec, package_id=package, version=package_version)


def parse_nuspec(content: bytes, *, package_id: str, version: str) -> NuGetMetadata:
    """Parse repository and project provenance from a nuspec document."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ProviderError(f"NuGet package nuspec is malformed: {exc}") from exc
    metadata = next((item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "metadata"), root)
    repository = next((item for item in metadata if item.tag.rsplit("}", 1)[-1] == "repository"), None)
    project_url_node = next((item for item in metadata if item.tag.rsplit("}", 1)[-1] == "projectUrl"), None)
    repository_url = sanitize_remote(repository.attrib.get("url")) if repository is not None else None
    repository_commit = repository.attrib.get("commit", "").strip() if repository is not None else ""
    project_url = sanitize_remote(project_url_node.text.strip()) if project_url_node is not None and project_url_node.text else None
    return NuGetMetadata(
        package_id=package_id,
        version=version,
        repository_url=repository_url,
        repository_commit=repository_commit or None,
        project_url=project_url,
    )


def validate_package_component(value: str, *, label: str) -> str:
    """Validate a NuGet path component before constructing a provider URL."""
    candidate = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,200}", candidate) or candidate in {".", ".."}:
        raise ValidationError(f"Invalid NuGet {label}: {value!r}")
    return candidate


def choose_nuget_ref(metadata: NuGetMetadata, tags: list[dict[str, Any]]) -> ResolvedRef:
    """Prefer an exact nuspec commit, then conservative version-tag candidates."""
    if metadata.repository_commit:
        commit = validate_ref(metadata.repository_commit)
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
            raise ProviderError(
                "NuGet repository metadata declares a non-commit source revision; refusing to treat it as exact provenance."
            )
        return ResolvedRef(commit, commit, commit.lower(), ResolutionKind.EXACT_COMMIT)
    by_name = {str(item["name"]): item for item in tags}
    exact_candidates = nuget_tag_candidates(metadata)
    for candidate in exact_candidates:
        if candidate in by_name:
            return ResolvedRef(metadata.version, candidate, by_name[candidate].get("commit_sha"), ResolutionKind.EXACT_TAG)
    normalized_version = _normalized_version(metadata.version)
    heuristic = [
        item for item in tags if _normalized_version(str(item["name"])) == normalized_version
    ]
    if len(heuristic) == 1:
        item = heuristic[0]
        return ResolvedRef(
            metadata.version,
            str(item["name"]),
            item.get("commit_sha"),
            ResolutionKind.HEURISTIC_TAG,
        )
    raise RefNotFoundError(
        f"No unique repository commit or tag could be proven for {metadata.package_id} {metadata.version}."
    )


def nuget_tag_candidates(metadata: NuGetMetadata) -> tuple[str, ...]:
    """Return conservative exact tag names for one NuGet package version."""
    return (
        metadata.version,
        f"v{metadata.version}",
        f"{metadata.package_id}-{metadata.version}",
        f"{metadata.package_id}/v{metadata.version}",
    )


def metadata_as_dict(metadata: NuGetMetadata) -> dict[str, Any]:
    """Return a JSON-ready metadata projection."""
    return asdict(metadata)


def _read_json_response(response: Any) -> Any:
    length = int(response.headers.get("Content-Length", "0") or 0)
    if length > MAX_METADATA_BYTES:
        raise ProviderError("Provider metadata response exceeds the safety limit.")
    content = response.read(MAX_METADATA_BYTES + 1)
    if len(content) > MAX_METADATA_BYTES:
        raise ProviderError("Provider metadata response exceeds the safety limit.")
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProviderError("Provider returned malformed JSON metadata.") from exc


def _read_text_limited(path: Path, max_bytes: int, *, root: Path) -> str:
    lexical_root = Path(os.path.abspath(root))
    lexical_path = Path(os.path.abspath(path))
    if not _is_relative_to(lexical_path, lexical_root) or is_link_or_reparse(lexical_path):
        raise ProviderError(f"Metadata file is outside its source root or linked: {path.name}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lexical_path, flags)
    except OSError as exc:
        raise ProviderError(f"Metadata file cannot be opened safely: {path.name}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise ProviderError(f"Metadata file must be a single regular file: {path.name}")
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ProviderError(f"Metadata file exceeds the {max_bytes}-byte safety limit: {path.name}")
    return content.decode("utf-8")


def _copy_limited(source: Any, destination: Any, *, max_bytes: int) -> int:
    total = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            return total
        total += len(chunk)
        if total > max_bytes:
            raise ProviderError(f"Download exceeds the {max_bytes}-byte safety limit.")
        destination.write(chunk)


def _http_error_message(error: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(error.read(64 * 1024))
        message = str(payload.get("message") or error.reason)
    except (OSError, json.JSONDecodeError, UnicodeError, AttributeError):
        message = str(error.reason)
    return redact_text(message)


def _looks_like_commit(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{7,64}", value))


def _git_missing_ref(detail: str) -> bool:
    lowered = detail.casefold()
    return any(
        marker in lowered
        for marker in (
            "couldn't find remote ref",
            "could not find remote branch",
            "not our ref",
            "invalid refspec",
            "remote ref does not exist",
        )
    )


def _normalized_version(value: str) -> str:
    lowered = value.casefold().strip()
    lowered = re.sub(r"^(?:refs/tags/)?v", "", lowered)
    match = re.search(r"\d+(?:\.\d+){1,3}(?:[-+][0-9a-z.-]+)?", lowered)
    return match.group(0) if match else lowered


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
