#!/usr/bin/env python3
"""Command-line interface for the global Inspect Dependency Source catalog."""

from __future__ import annotations

import argparse
import json
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from _catalog_models import CatalogError, ValidationError
from _catalog_paths import (
    HOME_ENV,
    load_user_config,
    platform_config_path,
    redact_text,
    resolve_catalog_root,
    save_catalog_root,
)
from _catalog_service import CatalogService


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally clean-break command surface."""
    parser = argparse.ArgumentParser(
        prog="inspect-dependency-source",
        description="Ground coding agents in exact, reusable dependency source.",
    )
    parser.add_argument(
        "--catalog-root",
        help="Use an absolute catalog root for this invocation (highest precedence).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="Initialize the global catalog and dashboard.")
    init_parser.add_argument("--catalog-root", dest="init_catalog_root")
    init_parser.add_argument("--no-dashboard", action="store_true")

    doctor = commands.add_parser("doctor", help="Check local runtime and optional provider capabilities.")
    doctor.add_argument("--json", action="store_true")

    config = commands.add_parser("config", help="Inspect or change the persisted user catalog root.")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show")
    set_root = config_commands.add_parser("set-root")
    set_root.add_argument("root")

    repo = commands.add_parser("repo", help="Manage repository identities and source artifacts.")
    repo_commands = repo.add_subparsers(dest="repo_command", required=True)

    add_github = repo_commands.add_parser("add-github")
    add_github.add_argument("repository")
    add_github.add_argument("--alias", action="append", default=[])

    add_git = repo_commands.add_parser("add-git")
    add_git.add_argument("url")
    add_git.add_argument("--alias", action="append", default=[])

    add_local = repo_commands.add_parser("add-local")
    add_local.add_argument("path")
    add_local.add_argument("--alias", action="append", default=[])

    scan_local = repo_commands.add_parser("scan-local")
    scan_local.add_argument("path", nargs="?")
    scan_local.add_argument("--max-depth", type=int, default=3)
    scan_local.add_argument("--update-existing", action="store_true")

    refresh = repo_commands.add_parser("refresh")
    refresh.add_argument("query", nargs="?")
    refresh.add_argument("--all", action="store_true")

    tags = repo_commands.add_parser("tags")
    tags.add_argument("query")
    tags.add_argument("--match")
    tags.add_argument("--limit", type=int, default=100)
    tags.add_argument("--refresh", action="store_true")
    tags.add_argument("--json", action="store_true")

    fetch = repo_commands.add_parser("fetch")
    fetch.add_argument("query")
    selection = fetch.add_mutually_exclusive_group(required=True)
    selection.add_argument("--ref")
    selection.add_argument("--default-branch", action="store_true")
    fetch.add_argument("--force", action="store_true")

    repo_list = repo_commands.add_parser("list")
    repo_list.add_argument("query", nargs="?")
    repo_list.add_argument("--json", action="store_true")

    remove = repo_commands.add_parser("remove")
    remove.add_argument("query")
    remove.add_argument("--purge-managed-cache", action="store_true")
    remove.add_argument("--yes", action="store_true")

    package = commands.add_parser("package", help="Resolve package versions to repository source.")
    package_commands = package.add_subparsers(dest="package_command", required=True)
    nuget = package_commands.add_parser("fetch-nuget")
    nuget.add_argument("package_id")
    nuget.add_argument("version")
    nuget.add_argument("--alias", action="append", default=[])
    nuget.add_argument("--force", action="store_true")

    resolve = commands.add_parser("resolve", help="Resolve a query to a verified source path.")
    resolve.add_argument("query")
    resolve.add_argument("--ref")
    resolve.add_argument("--json", action="store_true")

    verify = commands.add_parser("verify", help="Reconcile one or all persisted source artifacts.")
    verify.add_argument("query", nargs="?")
    verify.add_argument("--all", action="store_true")
    verify.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="Show global catalog and dashboard status.")
    status.add_argument("--json", action="store_true")

    dashboard = commands.add_parser("dashboard", help="Control the read-only localhost dashboard.")
    dashboard_commands = dashboard.add_subparsers(dest="dashboard_action", required=True)
    dashboard_start = dashboard_commands.add_parser("start", help="Start or reuse the dashboard.")
    dashboard_start.add_argument("--port", type=int)
    dashboard_commands.add_parser("status", help="Show verified dashboard status.")
    dashboard_commands.add_parser("stop", help="Stop the verified dashboard process.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, execute, and render one CLI request with stable error semantics."""
    args = build_parser().parse_args(argv)
    json_output = bool(getattr(args, "json", False))
    try:
        if args.command == "config":
            result = _handle_config(args)
            _render(result, json_output=False, label="Configuration")
            return 0
        selected_root = getattr(args, "init_catalog_root", None) or args.catalog_root
        root = resolve_catalog_root(selected_root)
        service = CatalogService(root)
        result, label = _dispatch(service, args)
        _render(result, json_output=json_output, label=label)
        return 0
    except CatalogError as exc:
        _render_error(exc, json_output=json_output)
        return exc.exit_code
    except KeyboardInterrupt:
        error = {"status": "error", "error": {"code": "interrupted", "message": "Interrupted."}}
        if json_output:
            print(json.dumps(error, ensure_ascii=False, separators=(",", ":")))
        else:
            print("error [interrupted]: Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        error = CatalogError(f"Unexpected internal error: {exc}")
        error.code = "internal_error"
        _render_error(error, json_output=json_output)
        return 1


def _dispatch(service: CatalogService, args: argparse.Namespace) -> tuple[Any, str]:
    if args.command == "init":
        return service.initialize(dashboard=not args.no_dashboard), "Initialized"
    if args.command == "doctor":
        return service.doctor(), "Doctor"
    if args.command == "status":
        return service.status(), "Status"
    if args.command == "resolve":
        return service.resolve(args.query, ref=args.ref), "Resolved source"
    if args.command == "verify":
        if bool(args.query) == bool(args.all):
            raise ValidationError("Choose a repository query or --all.")
        return service.verify(None if args.all else args.query), "Verification"
    if args.command == "dashboard":
        return (
            service.dashboard(args.dashboard_action, port=getattr(args, "port", None)),
            "Dashboard",
        )
    if args.command == "repo":
        return _dispatch_repository(service, args)
    if args.command == "package" and args.package_command == "fetch-nuget":
        return (
            service.fetch_nuget(
                args.package_id,
                args.version,
                aliases=args.alias,
                force=args.force,
            ),
            "NuGet source",
        )
    raise ValidationError("Unsupported command.")


def _dispatch_repository(service: CatalogService, args: argparse.Namespace) -> tuple[Any, str]:
    command = args.repo_command
    if command == "add-github":
        return service.add_github(args.repository, aliases=args.alias), "Repository"
    if command == "add-git":
        return service.add_git(args.url, aliases=args.alias), "Repository"
    if command == "add-local":
        return service.add_local(args.path, aliases=args.alias), "Repository"
    if command == "scan-local":
        path = args.path or str(Path.cwd())
        return (
            service.scan_local(path, max_depth=args.max_depth, update_existing=args.update_existing),
            "Local scan",
        )
    if command == "refresh":
        if bool(args.query) == bool(args.all):
            raise ValidationError("Choose a repository query or --all.")
        return (
            service.refresh_all() if args.all else service.refresh_repository(args.query),
            "Repository refresh",
        )
    if command == "tags":
        return (
            service.repository_tags(
                args.query,
                match=args.match,
                limit=args.limit,
                refresh=args.refresh,
            ),
            "Tags",
        )
    if command == "fetch":
        return (
            service.fetch(
                args.query,
                ref=args.ref,
                default_branch=args.default_branch,
                force=args.force,
            ),
            "Source artifact",
        )
    if command == "list":
        return service.list_repositories(args.query), "Repositories"
    if command == "remove":
        return (
            service.remove(
                args.query,
                purge_managed_cache=args.purge_managed_cache,
                yes=args.yes,
            ),
            "Repository removal",
        )
    raise ValidationError("Unsupported repository command.")


def _handle_config(args: argparse.Namespace) -> dict[str, Any]:
    config_path = platform_config_path()
    if args.config_command == "show":
        persisted = load_user_config(config_path)
        return {
            "config_path": str(config_path),
            "persisted_root": persisted.get("root_dir"),
            "environment_root": os.environ.get(HOME_ENV),
            "effective_root": str(resolve_catalog_root(args.catalog_root)),
        }
    if args.config_command == "set-root":
        root = save_catalog_root(args.root, config_path)
        return {"config_path": str(config_path), "persisted_root": str(root)}
    raise ValidationError("Unsupported configuration command.")


def _render(payload: Any, *, json_output: bool, label: str) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default))
        return
    if isinstance(payload, dict) and payload.get("status") == "ok" and payload.get("source_path"):
        print(f"{payload['source_path']}")
        print(
            f"  repository: {payload['repository']['canonical_name']}\n"
            f"  ref: {payload['artifact']['ref']}\n"
            f"  commit: {payload['artifact'].get('actual_commit') or 'unknown'}\n"
            f"  provenance: {payload['resolution_kind']} ({payload['verification_state']})"
        )
        return
    print(f"{label}:")
    for line in _text_lines(payload):
        print(f"  {line}")


def _text_lines(payload: Any, prefix: str = "") -> list[str]:
    if isinstance(payload, dict):
        lines: list[str] = []
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.extend(_text_lines(value, prefix=prefix + "  "))
            else:
                lines.append(f"{prefix}{key}: {_scalar(value)}")
        return lines
    if isinstance(payload, list):
        lines = []
        if not payload:
            return [f"{prefix}(none)"]
        for index, value in enumerate(payload, start=1):
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}[{index}]")
                lines.extend(_text_lines(value, prefix=prefix + "  "))
            else:
                lines.append(f"{prefix}- {_scalar(value)}")
        return lines
    return [f"{prefix}{_scalar(payload)}"]


def _scalar(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _render_error(error: CatalogError, *, json_output: bool) -> None:
    details = _redact_payload(error.details)
    payload = {
        "status": "error",
        "error": {"code": error.code, "message": redact_text(str(error)), **details},
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default))
    else:
        print(f"error [{error.code}]: {redact_text(str(error))}", file=sys.stderr)
        candidates = error.details.get("candidates") if error.details else None
        if candidates:
            for candidate in candidates:
                print(f"  - {candidate.get('canonical_name')} ({candidate.get('repository_id')})", file=sys.stderr)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
