"""Core integration and safety tests for Inspect Dependency Source.

The suite deliberately uses only Python's standard library and local resources. Remote
provider behavior is covered through local bare Git repositories or small test doubles so
the checks remain deterministic and work offline.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import inspect_dependency_source as cli
from _catalog_artifacts import ArtifactManager, safe_extract_tar, source_tree_digest
from _catalog_models import (
    ArtifactStatus,
    CatalogError,
    NuGetMetadata,
    OperationStatus,
    ProviderError,
    RefNotFoundError,
    ResolutionKind,
    ResolvedRef,
    SourceUnavailableError,
    ValidationError,
    VerificationState,
)
from _catalog_providers import (
    choose_nuget_ref,
    inspect_local_source,
    run_git,
    run_git_bounded,
    validate_git_remote,
)
from _catalog_paths import (
    ArtifactLock,
    HOME_ENV,
    artifact_id,
    guarded_remove,
    platform_config_path,
    platform_data_root,
    redact_text,
    resolve_catalog_root,
    sanitize_remote,
)
from _catalog_service import CatalogService
from _catalog_store import CatalogStore


def _run(arguments: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _make_source(parent: Path, name: str, *, version: str = "1.2.3") -> Path:
    source = parent / name
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8"
    )
    (source / "implementation.py").write_text("VALUE = 42\n", encoding="utf-8")
    return source


def _make_bare_git_repository(parent: Path) -> tuple[Path, str]:
    if shutil.which("git") is None:
        raise unittest.SkipTest("Git is not installed")
    working = parent / "working"
    remote = parent / "remote.git"
    working.mkdir()
    _run(["git", "init", "--initial-branch=main"], cwd=working)
    _run(["git", "config", "user.name", "Catalog Tests"], cwd=working)
    _run(["git", "config", "user.email", "catalog-tests@example.invalid"], cwd=working)
    (working / "version.txt").write_text("release-v1\n", encoding="utf-8")
    _run(["git", "add", "version.txt"], cwd=working)
    _run(["git", "commit", "-m", "release v1"], cwd=working)
    _run(["git", "tag", "v1.0.0"], cwd=working)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=working).stdout.strip().lower()
    _run(["git", "clone", "--bare", str(working), str(remote)])
    return remote, commit


def _make_local_git_source(parent: Path, remote: str) -> tuple[Path, str]:
    """Create one clean local Git source with a configurable, unreachable remote."""
    if shutil.which("git") is None:
        raise unittest.SkipTest("Git is not installed")
    source = _make_source(parent, "working-source")
    _run(["git", "init", "--initial-branch=main"], cwd=source)
    _run(["git", "config", "user.name", "Catalog Tests"], cwd=source)
    _run(["git", "config", "user.email", "catalog-tests@example.invalid"], cwd=source)
    _run(["git", "add", "."], cwd=source)
    _run(["git", "commit", "-m", "initial source"], cwd=source)
    _run(["git", "remote", "add", "origin", remote], cwd=source)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip().lower()
    return source, commit


def _invoke_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main(arguments)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class TemporaryDirectoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()


class _FakeGitProcess:
    def __init__(self, output: bytes = b"") -> None:
        self.stdout = io.BytesIO(output)
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return 0


class ProviderCommandTests(unittest.TestCase):
    def test_git_listing_output_is_bounded_before_buffering_untrusted_remote_data(self) -> None:
        class FakePipe:
            def __init__(self) -> None:
                self.reads = iter((b"x" * 17, b""))

            def read(self, size: int) -> bytes:
                del size
                return next(self.reads)

            def close(self) -> None:
                pass

        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = FakePipe()
                self.killed = False

            def kill(self) -> None:
                self.killed = True

            def wait(self) -> int:
                return 0

        process = FakeProcess()
        with (
            mock.patch("_catalog_providers.shutil.which", return_value="/usr/bin/git"),
            mock.patch("_catalog_providers.subprocess.Popen", return_value=process),
        ):
            with self.assertRaises(ProviderError):
                run_git_bounded(["ls-remote", "--tags", "remote"], max_bytes=16)
        self.assertTrue(process.killed)

    def test_git_timeout_has_stable_provider_error(self) -> None:
        class ImmediateTimer:
            daemon = False

            def __init__(self, interval, callback):
                del interval
                self.callback = callback

            def start(self):
                self.callback()

            def cancel(self):
                pass

        with (
            mock.patch("_catalog_providers.shutil.which", return_value="/usr/bin/git"),
            mock.patch("_catalog_providers.subprocess.Popen", return_value=_FakeGitProcess()),
            mock.patch("_catalog_providers.threading.Timer", ImmediateTimer),
        ):
            with self.assertRaises(ProviderError) as raised:
                run_git(["status"])
        self.assertEqual(raised.exception.code, "provider_error")

    def test_git_execution_failure_has_stable_capability_error(self) -> None:
        with (
            mock.patch("_catalog_providers.shutil.which", return_value="/usr/bin/git"),
            mock.patch("_catalog_providers.subprocess.Popen", side_effect=OSError("cannot execute")),
        ):
            with self.assertRaises(CatalogError) as raised:
                run_git(["status"])
        self.assertEqual(raised.exception.code, "capability_unavailable")

    def test_git_forces_deterministic_english_locale(self) -> None:
        with (
            mock.patch("_catalog_providers.shutil.which", return_value="/usr/bin/git"),
            mock.patch(
                "_catalog_providers.subprocess.Popen", return_value=_FakeGitProcess()
            ) as runner,
            mock.patch.dict(os.environ, {"LC_ALL": "zh_CN.UTF-8", "LANG": "zh_CN.UTF-8"}),
        ):
            run_git(["status"])
        environment = runner.call_args.kwargs["env"]
        self.assertEqual(environment["LC_ALL"], "C")
        self.assertEqual(environment["LANG"], "C")


class CatalogPathTests(TemporaryDirectoryTestCase):
    def test_catalog_root_rejects_volume_root_and_non_dedicated_directory(self) -> None:
        with self.assertRaises(ValidationError):
            resolve_catalog_root(Path("/"))

        occupied = self.temp / "occupied"
        occupied.mkdir()
        unrelated = occupied / "unrelated.txt"
        unrelated.write_text("preserve", encoding="utf-8")
        with self.assertRaises(ValidationError):
            CatalogStore(occupied).initialize()
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve")

    def test_platform_paths_cover_linux_macos_and_windows(self) -> None:
        home = self.temp / "home"
        linux_environment = {
            "XDG_CONFIG_HOME": str(self.temp / "linux-config"),
            "XDG_DATA_HOME": str(self.temp / "linux-data"),
        }
        self.assertEqual(
            platform_config_path(environ=linux_environment, system="Linux", home=home),
            self.temp / "linux-config" / "inspect-dependency-source" / "config.json",
        )
        self.assertEqual(
            platform_data_root(environ=linux_environment, system="Linux", home=home),
            (self.temp / "linux-data" / "inspect-dependency-source").resolve(),
        )

        self.assertEqual(
            platform_config_path(environ={"UNRELATED": "1"}, system="Darwin", home=home),
            home / "Library" / "Application Support" / "Inspect Dependency Source" / "config.json",
        )
        self.assertEqual(
            platform_data_root(environ={"UNRELATED": "1"}, system="Darwin", home=home),
            (home / "Library" / "Application Support" / "Inspect Dependency Source").resolve(),
        )

        windows_environment = {
            "APPDATA": str(self.temp / "windows-roaming"),
            "LOCALAPPDATA": str(self.temp / "windows-local"),
        }
        self.assertEqual(
            platform_config_path(environ=windows_environment, system="Windows", home=home),
            self.temp / "windows-roaming" / "Inspect Dependency Source" / "config.json",
        )
        self.assertEqual(
            platform_data_root(environ=windows_environment, system="Windows", home=home),
            (self.temp / "windows-local" / "Inspect Dependency Source").resolve(),
        )

    def test_catalog_root_precedence_is_cli_environment_config_then_platform(self) -> None:
        config_path = self.temp / "config.json"
        configured = self.temp / "configured"
        config_path.write_text(json.dumps({"root_dir": str(configured)}), encoding="utf-8")
        command_line = self.temp / "command-line"
        environment = self.temp / "environment"

        self.assertEqual(
            resolve_catalog_root(
                command_line,
                environ={HOME_ENV: str(environment)},
                config_path=config_path,
                system="Linux",
                home=self.temp / "home",
            ),
            command_line.resolve(),
        )
        self.assertEqual(
            resolve_catalog_root(
                None,
                environ={HOME_ENV: str(environment)},
                config_path=config_path,
                system="Linux",
                home=self.temp / "home",
            ),
            environment.resolve(),
        )
        self.assertEqual(
            resolve_catalog_root(
                None,
                environ={"UNRELATED": "1"},
                config_path=config_path,
                system="Linux",
                home=self.temp / "home",
            ),
            configured.resolve(),
        )

        missing_config = self.temp / "missing.json"
        for system, environment_map, expected in (
            (
                "Linux",
                {"XDG_DATA_HOME": str(self.temp / "xdg-data")},
                self.temp / "xdg-data" / "inspect-dependency-source",
            ),
            (
                "Darwin",
                {"UNRELATED": "1"},
                self.temp
                / "home"
                / "Library"
                / "Application Support"
                / "Inspect Dependency Source",
            ),
            (
                "Windows",
                {"LOCALAPPDATA": str(self.temp / "win-data")},
                self.temp / "win-data" / "Inspect Dependency Source",
            ),
        ):
            with self.subTest(system=system):
                self.assertEqual(
                    resolve_catalog_root(
                        None,
                        environ=environment_map,
                        config_path=missing_config,
                        system=system,
                        home=self.temp / "home",
                    ),
                    expected.resolve(),
                )

        with self.assertRaises(ValidationError):
            resolve_catalog_root("relative/catalog", environ={"UNRELATED": "1"})


class CatalogStoreTests(TemporaryDirectoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.temp / "catalog"
        self.store = CatalogStore(self.root)
        self.store.initialize()

    def test_sqlite_supports_concurrent_readers_and_short_writers(self) -> None:
        start = threading.Event()

        def writer(index: int) -> str:
            start.wait(timeout=5)
            repository_id = f"repo--concurrent-{index:02d}"
            self.store.add_repository(
                repository_id=repository_id,
                provider="local",
                canonical_name=f"local:concurrent-{index:02d}",
                display_name=f"Concurrent {index:02d}",
                remote_url=None,
                origin_kind="local",
                aliases=(f"alias-{index:02d}",),
            )
            return repository_id

        def reader() -> None:
            start.wait(timeout=5)
            for _ in range(30):
                self.store.repositories()
                self.store.summary()

        with ThreadPoolExecutor(max_workers=9) as executor:
            writers = [executor.submit(writer, index) for index in range(16)]
            readers = [executor.submit(reader) for _ in range(2)]
            start.set()
            written = [future.result(timeout=30) for future in writers]
            for future in readers:
                future.result(timeout=30)

        self.assertEqual(len(written), 16)
        self.assertEqual(self.store.summary()["repository_count"], 16)
        with self.store.connect() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            schema_version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual(journal_mode.casefold(), "wal")
        self.assertEqual(schema_version, "1")

    def test_concurrent_first_initialization_is_serialized(self) -> None:
        fresh_root = self.temp / "first-init"
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(CatalogStore(fresh_root).initialize) for _ in range(24)]
            for future in futures:
                future.result(timeout=30)
        self.assertEqual(CatalogStore(fresh_root).summary()["repository_count"], 0)

    def test_same_ref_in_two_repositories_keeps_distinct_artifacts(self) -> None:
        repository_ids = ("repo--alpha", "repo--beta")
        for repository_id in repository_ids:
            self.store.add_repository(
                repository_id=repository_id,
                provider="local",
                canonical_name=f"local:{repository_id}",
                display_name=repository_id,
                remote_url=None,
                origin_kind="local",
            )
            source = self.temp / repository_id
            source.mkdir()
            identity = artifact_id(repository_id, "local", "v1.0.0")
            self.store.upsert_artifact(
                repository_id,
                {
                    "id": identity,
                    "kind": "local",
                    "ref": "v1.0.0",
                    "source_path": str(source),
                    "status": "ready",
                    "resolution_kind": "exact_tag",
                    "verification_state": "verified",
                    "external": True,
                },
            )
        alpha = self.store.resolve_artifact(repository_ids[0])
        beta = self.store.resolve_artifact(repository_ids[1])
        self.assertNotEqual(alpha["id"], beta["id"])
        self.assertEqual(alpha["repository_id"], repository_ids[0])
        self.assertEqual(beta["repository_id"], repository_ids[1])

    def test_database_symlink_is_rejected_without_touching_target(self) -> None:
        root = self.temp / "symlink-database"
        (root / "state").mkdir(parents=True)
        victim = self.temp / "database-victim"
        victim.write_text("keep", encoding="utf-8")
        try:
            (root / "state" / "catalog.sqlite3").symlink_to(victim)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"File symlinks are unavailable: {exc}")
        with self.assertRaises(ValidationError):
            CatalogStore(root).initialize()
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_database_sidecar_hardlink_is_rejected_without_touching_target(self) -> None:
        root = self.temp / "hardlink-sidecar"
        (root / "state").mkdir(parents=True)
        victim = self.temp / "sidecar-victim"
        victim.write_text("keep", encoding="utf-8")
        try:
            os.link(victim, root / "state" / "catalog.sqlite3-shm")
        except OSError as exc:
            self.skipTest(f"Hard links are unavailable: {exc}")
        with self.assertRaises(ValidationError):
            CatalogStore(root).initialize()
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_recovery_interrupts_only_operations_owned_by_dead_processes(self) -> None:
        dead = self.store.create_operation("repo fetch", target="dead")
        live = self.store.create_operation("repo fetch", target="live")
        self.store.update_operation(
            dead,
            status=OperationStatus.RUNNING,
            phase="download",
            message="Downloading.",
        )
        with self.store.connect(write=True) as connection:
            connection.execute("UPDATE operations SET pid=? WHERE id=?", (2_147_483_647, dead))

        self.assertEqual(self.store.recover_stale_operations(), 1)
        by_id = {operation["id"]: operation for operation in self.store.operations()}
        self.assertEqual(by_id[dead]["status"], OperationStatus.INTERRUPTED)
        self.assertEqual(by_id[dead]["error_code"], "owner_process_missing")
        self.assertEqual(by_id[live]["status"], OperationStatus.QUEUED)
        dead_events = [event for event in self.store.events() if event["operation_id"] == dead]
        self.assertEqual(dead_events[-1]["status"], OperationStatus.INTERRUPTED)
        self.assertEqual(dead_events[-1]["phase"], "recovered")

    def test_recovery_detects_pid_reuse_from_process_start_token(self) -> None:
        operation = self.store.create_operation("repo fetch", target="pid-reused")
        with self.store.connect(write=True) as connection:
            connection.execute(
                "UPDATE operations SET owner_token=? WHERE id=?",
                ("proc:old-owner", operation),
            )

        with (
            mock.patch("_catalog_store._pid_is_alive", return_value=True),
            mock.patch("_catalog_store._process_start_token", return_value="proc:new-owner"),
        ):
            self.assertEqual(self.store.recover_stale_operations(), 1)

        recovered = next(item for item in self.store.operations() if item["id"] == operation)
        self.assertEqual(recovered["status"], OperationStatus.INTERRUPTED)

    def test_service_startup_recovers_interrupted_operations(self) -> None:
        operation = self.store.create_operation("repo fetch", target="crashed-owner")
        with self.store.connect(write=True) as connection:
            connection.execute(
                "UPDATE operations SET pid=?,owner_token=? WHERE id=?",
                (2_147_483_647, "proc:gone", operation),
            )

        restarted = CatalogService(self.root)

        recovered = next(item for item in restarted.store.operations() if item["id"] == operation)
        self.assertEqual(recovered["status"], OperationStatus.INTERRUPTED)


class LocalCatalogTests(TemporaryDirectoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.temp / "catalog"
        self.service = CatalogService(self.root)

    def test_github_redirect_locks_the_existing_canonical_repository_id(self) -> None:
        historical = self.service.store.add_repository(
            repository_id="repo--historical-renamed",
            provider="github",
            canonical_name="new/renamed",
            display_name="renamed",
            remote_url="https://github.com/new/renamed.git",
            origin_kind="github",
        )
        metadata = {
            "full_name": "new/renamed",
            "display_name": "renamed",
            "remote_url": "https://github.com/new/renamed.git",
        }
        repository_guard = self.service.artifacts.repository_guard

        with (
            mock.patch("_catalog_service.GitHubClient") as github_client,
            mock.patch.object(
                self.service.artifacts,
                "repository_guard",
                side_effect=lambda repository_id, **kwargs: repository_guard(
                    repository_id, **kwargs
                ),
            ) as guarded,
        ):
            github_client.return_value.repository.return_value = metadata
            repository = self.service.add_github(
                "old/name", aliases=("redirected",)
            )
            self.assertEqual(guarded.call_args_list[0].args[0], historical["id"])
            guarded.reset_mock()
            ensured = self.service._ensure_github_repository(
                "old/name", aliases=("package-alias",)
            )

        self.assertEqual(repository["id"], historical["id"])
        self.assertEqual(ensured["id"], historical["id"])
        self.assertIn("redirected", repository["aliases"])
        self.assertIn("package-alias", ensured["aliases"])
        self.assertEqual(guarded.call_args_list[0].args[0], historical["id"])
        self.assertTrue(
            (
                self.service.artifacts.repository_cache_path(historical["id"])
                / "manifest.stub.json"
            ).is_file()
        )

    def test_local_registration_resolution_alias_matching_and_tag_filtering(self) -> None:
        source = _make_source(self.temp, "widget-source", version="2.4.6")
        repository = self.service.add_local(source, aliases=("Widget SDK", "widgets"))

        resolved = self.service.resolve("widget sdk")
        self.assertEqual(resolved["status"], "ok")
        self.assertEqual(Path(resolved["source_path"]), source.resolve())
        self.assertEqual(resolved["artifact"]["detected_version"], "2.4.6")
        self.assertEqual(resolved["resolution_kind"], ResolutionKind.UNRESOLVED)
        self.assertEqual(resolved["verification_state"], "unverified")
        self.assertIn("Widget SDK", resolved["repository"]["aliases"])

        matches = self.service.list_repositories("WIDGETS")
        self.assertEqual([item["id"] for item in matches], [repository["id"]])

        self.service.store.replace_tags(
            repository["id"],
            [
                {"name": "alpha-1.0", "commit_sha": "a" * 40},
                {"name": "beta-1.0", "commit_sha": "b" * 40},
                {"name": "beta-2.0", "commit_sha": "c" * 40},
            ],
        )
        filtered = self.service.repository_tags(
            "Widget SDK", match="BETA", limit=1, refresh=False
        )
        self.assertEqual([item["name"] for item in filtered], ["beta-1.0"])

    def test_local_resolution_does_not_walk_the_user_owned_source_tree(self) -> None:
        source = _make_source(self.temp, "large-external-source")
        repository = self.service.add_local(source)

        with mock.patch(
            "_catalog_artifacts.directory_size",
            side_effect=AssertionError("external source must not be measured during resolve"),
        ):
            resolved = self.service.resolve(repository["id"])

        self.assertEqual(resolved["status"], "ok")
        self.assertEqual(Path(resolved["source_path"]), source.resolve())

    def test_local_scan_prunes_windows_reparse_like_directories(self) -> None:
        scan_root = self.temp / "scan-root"
        hidden = _make_source(scan_root / "junction", "hidden-source")
        with mock.patch(
            "_catalog_providers.is_link_or_reparse",
            side_effect=lambda path: Path(path).name == "junction",
        ):
            result = self.service.scan_local(
                scan_root,
                max_depth=4,
                update_existing=False,
            )
        self.assertEqual(result["discovered_count"], 0)
        self.assertNotIn(str(hidden), result["skipped"])

    def test_unicode_alias_and_case_sensitive_local_paths_remain_distinct(self) -> None:
        upper = self.service.add_local(_make_source(self.temp, "Widget"), aliases=("组件源码",))
        self.assertEqual(self.service.resolve("组件源码")["repository"]["id"], upper["id"])
        with tempfile.TemporaryDirectory(dir="/tmp") as case_root:
            case_parent = Path(case_root)
            distinct_upper = self.service.add_local(_make_source(case_parent, "CaseSource"))
            distinct_lower = self.service.add_local(_make_source(case_parent, "casesource"))
            self.assertNotEqual(distinct_upper["id"], distinct_lower["id"])

    def test_generic_git_path_case_is_part_of_repository_identity(self) -> None:
        upper = self.service.add_git("https://example.invalid/Org/Widget.git")
        lower = self.service.add_git("https://EXAMPLE.invalid/Org/widget.git")
        self.assertNotEqual(upper["id"], lower["id"])

    def test_same_name_local_repositories_at_different_paths_remain_distinct(self) -> None:
        first = self.service.add_local(_make_source(self.temp / "first", "shared-name"))
        second = self.service.add_local(_make_source(self.temp / "second", "shared-name"))
        self.assertNotEqual(first["id"], second["id"])

    def test_local_commit_refresh_updates_one_artifact_in_place(self) -> None:
        remote = "https://example.invalid/team/commit-refresh.git"
        source, first_commit = _make_local_git_source(self.temp, remote)
        first = self.service.add_local(source)
        first_artifact = first["preferred_artifact"]

        (source / "implementation.py").write_text("VALUE = 84\n", encoding="utf-8")
        _run(["git", "add", "implementation.py"], cwd=source)
        _run(["git", "commit", "-m", "update implementation"], cwd=source)
        second_commit = _run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip().lower()
        refreshed = self.service.add_local(source)

        self.assertEqual(refreshed["id"], first["id"])
        self.assertEqual(len(refreshed["artifacts"]), 1)
        artifact = refreshed["preferred_artifact"]
        self.assertEqual(artifact["id"], first_artifact["id"])
        self.assertEqual(artifact["actual_commit"], second_commit)
        self.assertEqual(artifact["expected_commit"], second_commit)
        self.assertIn(second_commit, artifact["ref"])
        self.assertNotIn(first_commit, artifact["ref"])

    def test_local_verification_refreshes_current_commit_without_rebinding_provenance(self) -> None:
        remote = "https://example.invalid/team/live-snapshot.git"
        source, first_commit = _make_local_git_source(self.temp, remote)
        repository = self.service.add_local(source)

        (source / "implementation.py").write_text("VALUE = 84\n", encoding="utf-8")
        _run(["git", "add", "implementation.py"], cwd=source)
        _run(["git", "commit", "-m", "move local checkout"], cwd=source)
        current_commit = _run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip().lower()

        resolved = self.service.resolve(repository["id"])

        self.assertEqual(resolved["artifact"]["expected_commit"], first_commit)
        self.assertEqual(resolved["artifact"]["actual_commit"], current_commit)
        self.assertEqual(resolved["verification_state"], VerificationState.UNVERIFIED)

    def test_external_local_path_retargeted_through_symlink_is_rejected(self) -> None:
        source = _make_source(self.temp, "retargeted-local")
        repository = self.service.add_local(source)
        moved = self.temp / "moved-local"
        source.rename(moved)
        try:
            source.symlink_to(moved, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            raise unittest.SkipTest(f"Directory symlinks are unavailable: {exc}")

        with self.assertRaises(SourceUnavailableError):
            self.service.resolve(repository["id"])

    def test_add_local_rejects_lexical_symlink_path(self) -> None:
        target = _make_source(self.temp, "local-target")
        link = self.temp / "local-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            raise unittest.SkipTest(f"Directory symlinks are unavailable: {exc}")

        with self.assertRaises(ValidationError):
            self.service.add_local(link)

    def test_verification_retries_after_concurrent_artifact_generation_change(self) -> None:
        source = _make_source(self.temp, "verification-race")
        repository = self.service.add_local(source)
        original_verify = self.service.artifacts.verify_record
        calls = 0

        def verify_with_replacement(artifact):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.service.add_local(source)
                raise OSError("artifact directory was replaced during verification")
            return original_verify(artifact)

        with mock.patch.object(
            self.service.artifacts,
            "verify_record",
            side_effect=verify_with_replacement,
        ):
            resolved = self.service.resolve(repository["id"])

        self.assertEqual(resolved["status"], "ok")
        self.assertEqual(calls, 2)

    def test_verification_io_failure_has_source_unavailable_contract(self) -> None:
        source = _make_source(self.temp, "verification-io-failure")
        repository = self.service.add_local(source)

        with mock.patch.object(
            self.service.artifacts,
            "verify_record",
            side_effect=OSError("transient read failure"),
        ):
            with self.assertRaises(SourceUnavailableError):
                self.service.resolve(repository["id"])

    def test_local_remote_change_transfers_path_and_clears_old_preference(self) -> None:
        first_remote = "https://example.invalid/team/first-owner.git"
        second_remote = "https://example.invalid/team/second-owner.git"
        source, commit = _make_local_git_source(self.temp, first_remote)
        first = self.service.add_local(source)

        _run(["git", "remote", "set-url", "origin", second_remote], cwd=source)
        second = self.service.add_local(source)

        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["preferred_artifact"]["actual_commit"], commit)
        self.assertEqual(second["local_sources"][0]["canonical_path"], str(source.resolve()))
        former_owner = self.service.store.repository(first["id"])
        self.assertEqual(former_owner["local_sources"], [])
        self.assertEqual(former_owner["artifacts"], [])
        self.assertIsNone(former_owner["preferred_artifact_id"])

    def test_git_and_local_registration_are_order_independent_and_idempotent(self) -> None:
        remote = "https://example.invalid/team/order-independent.git"
        outcomes = []
        for local_first in (False, True):
            with self.subTest(local_first=local_first):
                case_root = self.temp / str(local_first)
                source, _ = _make_local_git_source(case_root, remote)
                service = CatalogService(case_root / "catalog")
                if local_first:
                    initial = service.add_local(source, aliases=("from-local",))
                    final = service.add_git(remote, aliases=("from-git",))
                else:
                    initial = service.add_git(remote, aliases=("from-git",))
                    final = service.add_local(source, aliases=("from-local",))
                repeated = service.add_git(remote, aliases=("from-repeat",))

                self.assertEqual(final["id"], initial["id"])
                self.assertEqual(repeated["id"], initial["id"])
                self.assertEqual(len(service.list_repositories()), 1)
                detail = service.store.repository(initial["id"])
                self.assertEqual(detail["provider"], "git")
                self.assertEqual(detail["origin_kind"], "hybrid")
                self.assertEqual(len(detail["local_sources"]), 1)
                self.assertEqual(
                    set(detail["aliases"]), {"from-local", "from-git", "from-repeat"}
                )
                outcomes.append(
                    {
                        "id": detail["id"],
                        "canonical_name": detail["canonical_name"],
                        "provider": detail["provider"],
                        "origin_kind": detail["origin_kind"],
                    }
                )
        self.assertEqual(outcomes[0], outcomes[1])

    def test_package_versions_remain_binding_owned_when_they_share_an_artifact(self) -> None:
        source = _make_source(self.temp, "shared-package-source")
        repository = self.service.add_local(source)
        artifact = repository["preferred_artifact"]
        for package_id, version in (("Alpha.Package", "1.0.0"), ("Beta.Package", "2.0.0")):
            self.service.store.bind_package(
                {
                    "ecosystem": "nuget",
                    "package_id": package_id,
                    "version": version,
                    "repository_id": repository["id"],
                    "artifact_id": artifact["id"],
                    "requested_ref": version,
                    "resolved_ref": artifact["ref"],
                    "resolution_kind": ResolutionKind.HEURISTIC_TAG,
                    "expected_commit": None,
                }
            )

        alpha = self.service.resolve("Alpha.Package", ref="1.0.0")
        beta = self.service.resolve("Beta.Package", ref="2.0.0")
        self.assertEqual(alpha["artifact"]["id"], beta["artifact"]["id"])
        self.assertNotIn("requested_version", alpha["artifact"])
        self.assertEqual(alpha["package"]["version"], "1.0.0")
        self.assertEqual(beta["package"]["version"], "2.0.0")

    def test_package_binding_rejects_an_artifact_owned_by_another_repository(self) -> None:
        first = self.service.add_local(_make_source(self.temp / "first-binding", "source"))
        second = self.service.add_local(_make_source(self.temp / "second-binding", "source"))

        with self.assertRaises(ValidationError):
            self.service.store.bind_package(
                {
                    "ecosystem": "nuget",
                    "package_id": "Cross.Repository.Package",
                    "version": "1.0.0",
                    "repository_id": first["id"],
                    "artifact_id": second["preferred_artifact"]["id"],
                    "requested_ref": "1.0.0",
                    "resolved_ref": second["preferred_artifact"]["ref"],
                    "resolution_kind": ResolutionKind.HEURISTIC_TAG,
                    "expected_commit": None,
                }
            )

    def test_package_binding_fails_closed_for_missing_or_known_invalid_artifact(self) -> None:
        source = _make_source(self.temp, "bound-package-source")
        repository = self.service.add_local(source)
        artifact = self.service.store.artifact(repository["preferred_artifact"]["id"])
        assert artifact is not None
        binding = {
            "ecosystem": "nuget",
            "package_id": "Bound.Package",
            "version": "1.0.0",
            "repository_id": repository["id"],
            "artifact_id": artifact["id"],
            "requested_ref": "1.0.0",
            "resolved_ref": artifact["ref"],
            "resolution_kind": ResolutionKind.HEURISTIC_TAG,
            "expected_commit": None,
        }
        self.service.store.bind_package(binding)
        self.assertTrue(
            self.service.store.update_artifact_health(
                artifact["id"],
                status=ArtifactStatus.INVALID,
                verification_state=VerificationState.FAILED,
                source_bytes=None,
                archive_bytes=None,
                file_count=None,
                expected_generation=artifact["generation"],
            )
        )
        with self.assertRaises(SourceUnavailableError):
            self.service.resolve("Bound.Package", ref="1.0.0")

        with self.service.store.connect(write=True) as connection:
            connection.execute(
                "UPDATE package_bindings SET artifact_id=NULL WHERE package_id=?",
                ("Bound.Package",),
            )
        with self.assertRaises(SourceUnavailableError):
            self.service.resolve("Bound.Package", ref="1.0.0")

    def test_stale_health_write_cannot_clobber_new_artifact_generation(self) -> None:
        source = _make_source(self.temp, "artifact-generation")
        repository = self.service.add_local(source)
        stale = self.service.store.artifact(repository["preferred_artifact"]["id"])
        assert stale is not None
        refreshed = self.service.add_local(source)
        current = self.service.store.artifact(refreshed["preferred_artifact"]["id"])
        assert current is not None
        self.assertGreater(current["generation"], stale["generation"])

        changed = self.service.store.update_artifact_health(
            stale["id"],
            status=ArtifactStatus.INVALID,
            verification_state=VerificationState.FAILED,
            source_bytes=None,
            archive_bytes=None,
            file_count=None,
            expected_generation=stale["generation"],
        )

        self.assertFalse(changed)
        persisted = self.service.store.artifact(stale["id"])
        assert persisted is not None
        self.assertEqual(persisted["status"], ArtifactStatus.READY)

    def test_secondary_invalid_artifact_is_counted_as_an_integrity_warning(self) -> None:
        source = _make_source(self.temp, "secondary-integrity")
        repository = self.service.add_local(source)
        preferred = self.service.store.artifact(repository["preferred_artifact"]["id"])
        assert preferred is not None
        secondary = {
            **preferred,
            "id": artifact_id(repository["id"], "local", "secondary-invalid"),
            "ref": "secondary-invalid",
            "status": ArtifactStatus.INVALID,
            "verification_state": VerificationState.FAILED,
        }
        self.service.store.upsert_artifact(repository["id"], secondary, prefer=False)

        summary = self.service.store.summary()
        inventory = self.service.list_repositories()[0]
        detail = self.service.store.repository(repository["id"])

        self.assertEqual(summary["integrity_warning_count"], 1)
        self.assertIn(
            "artifact_integrity_warning",
            {issue["code"] for issue in inventory["health"]["issues"]},
        )
        self.assertIn(
            "artifact_integrity_warning",
            {issue["code"] for issue in detail["health"]["issues"]},
        )

    def test_malformed_enriched_manifest_is_reported_without_breaking_inventory(self) -> None:
        source = _make_source(self.temp, "manifest-source")
        repository = self.service.add_local(source)
        manifest = self.root / "repos" / repository["id"] / "manifest.json"
        manifest.write_text('{"summary": ', encoding="utf-8")

        self.service.store.reconcile_cached_metrics()
        detail = self.service.store.repository(repository["id"])
        self.assertEqual(detail["manifest"]["status"], "invalid")
        self.assertIn("error", detail["manifest"])
        issue_codes = {issue["code"] for issue in detail["health"]["issues"]}
        self.assertIn("manifest_invalid", issue_codes)
        inventory = self.service.list_repositories()
        self.assertEqual(inventory[0]["manifest"]["status"], "invalid")

    def test_manifest_summary_type_is_bounded_before_sqlite_projection(self) -> None:
        source = _make_source(self.temp, "manifest-summary-type")
        repository = self.service.add_local(source)
        manifest = self.root / "repos" / repository["id"] / "manifest.json"
        manifest.write_text('{"summary":["not","text"]}', encoding="utf-8")

        self.service.store.reconcile_cached_metrics()

        detail = self.service.store.repository(repository["id"])
        self.assertEqual(detail["manifest"]["status"], "invalid")
        self.assertIn("summary must be a string", detail["manifest"]["error"])


class ExactGitRefTests(TemporaryDirectoryTestCase):
    def test_acquire_derives_cache_identity_from_guarded_provider_snapshot(self) -> None:
        service = CatalogService(self.temp / "catalog")
        stale = service.add_git("https://github.com/example/promoted.git")
        current = service.store.add_repository(
            repository_id=stale["id"],
            provider="github",
            canonical_name="example/promoted",
            display_name="promoted",
            remote_url="https://github.com/example/promoted.git",
            origin_kind="github",
        )
        self.assertEqual(stale["provider"], "git")
        self.assertEqual(current["provider"], "github")

        resolved = ResolvedRef(
            "v1.0.0", "v1.0.0", "a" * 40, ResolutionKind.EXACT_TAG
        )
        identity = artifact_id(stale["id"], "github_archive", resolved.resolved_ref)
        source = (
            service.artifacts.repository_cache_path(stale["id"])
            / "artifacts"
            / identity
            / "source"
        )
        source.mkdir(parents=True)
        (source / "README.md").write_text("promoted source\n", encoding="utf-8")
        service.store.upsert_artifact(
            stale["id"],
            {
                "id": identity,
                "kind": "github_archive",
                "ref": resolved.resolved_ref,
                "source_path": str(source),
                "status": ArtifactStatus.READY,
                "resolution_kind": ResolutionKind.EXACT_TAG,
                "expected_commit": resolved.commit_sha,
                "actual_commit": resolved.commit_sha,
                "verification_state": VerificationState.VERIFIED,
                "external": False,
            },
        )
        trusted_ids: list[str] = []

        def reuse_guarded_cache(**kwargs):
            trusted = kwargs["load_trusted"]()
            self.assertIsNotNone(trusted)
            trusted_ids.append(trusted["id"])
            kwargs["persist_validated"](trusted)
            return trusted

        with mock.patch.object(
            service.artifacts, "acquire_github", side_effect=reuse_guarded_cache
        ):
            artifact = service._acquire(
                stale,
                resolved,
                force=False,
                nested_operation=False,
            )

        self.assertEqual(trusted_ids, [identity])
        self.assertEqual(artifact["id"], identity)

    def test_missing_exact_ref_never_substitutes_default_branch(self) -> None:
        remote, expected_commit = _make_bare_git_repository(self.temp)
        service = CatalogService(self.temp / "catalog")
        repository = service.add_git(str(remote), aliases=("local-release",))
        service.refresh_repository(repository["id"])

        artifact = service.fetch(
            repository["id"], ref="v1.0.0", default_branch=False, force=False
        )
        self.assertEqual(artifact["ref"], "v1.0.0")
        self.assertEqual(artifact["actual_commit"], expected_commit)
        self.assertEqual(
            (Path(artifact["source_path"]) / "version.txt").read_text(encoding="utf-8"),
            "release-v1\n",
        )
        before = {item["id"] for item in service.store.artifacts(repository["id"])}

        with self.assertRaises(RefNotFoundError):
            service.fetch(
                repository["id"],
                ref="definitely-missing-ref",
                default_branch=False,
                force=False,
            )

        after = {item["id"] for item in service.store.artifacts(repository["id"])}
        self.assertEqual(after, before)
        self.assertEqual(len(after), 1)
        self.assertIsNone(
            service.store.resolve_artifact(repository["id"], ref="definitely-missing-ref")
        )

    def test_managed_git_digest_detects_ignored_working_tree_additions(self) -> None:
        remote, _commit = _make_bare_git_repository(self.temp)
        service = CatalogService(self.temp / "catalog")
        repository = service.add_git(str(remote))
        artifact = service.fetch(
            repository["id"], ref="v1.0.0", default_branch=False, force=False
        )
        source = Path(artifact["source_path"])
        (source / ".git" / "info" / "exclude").write_text("*.secret\n", encoding="utf-8")
        (source / "payload.secret").write_text("ignored but untrusted\n", encoding="utf-8")
        self.assertEqual(_run(["git", "status", "--porcelain"], cwd=source).stdout, "")
        manifest_path = source.parent / "artifact.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_digest"] = source_tree_digest(
            source, excluded_top_level=frozenset({".git"})
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(ProviderError):
            service.fetch(
                repository["id"], ref="v1.0.0", default_branch=False, force=False
            )
        with self.assertRaises(SourceUnavailableError):
            service.resolve(repository["id"], ref="v1.0.0")

    def test_managed_git_is_reverified_when_persisted_actual_commit_is_missing(self) -> None:
        remote, expected_commit = _make_bare_git_repository(self.temp)
        service = CatalogService(self.temp / "catalog")
        repository = service.add_git(str(remote), aliases=("managed-release",))
        artifact = service.fetch(
            repository["id"], ref="v1.0.0", default_branch=False, force=False
        )
        with service.store.connect(write=True) as connection:
            connection.execute(
                """
                UPDATE artifacts
                SET actual_commit=NULL,status='ready',verification_state='unverified'
                WHERE id=?
                """,
                (artifact["id"],),
            )

        resolved = service.resolve(repository["id"], ref="v1.0.0")

        self.assertEqual(resolved["verification_state"], VerificationState.VERIFIED)
        self.assertEqual(resolved["artifact"]["actual_commit"], expected_commit)

        source = Path(resolved["source_path"])
        (source / "version.txt").write_text("tampered\n", encoding="utf-8")
        with service.store.connect(write=True) as connection:
            connection.execute(
                """
                UPDATE artifacts
                SET actual_commit=NULL,status='ready',verification_state='unverified'
                WHERE id=?
                """,
                (artifact["id"],),
            )

        with self.assertRaises(SourceUnavailableError):
            service.resolve(repository["id"], ref="v1.0.0")
        persisted = service.store.artifact(artifact["id"])
        assert persisted is not None
        self.assertEqual(persisted["status"], ArtifactStatus.INVALID)
        self.assertEqual(persisted["verification_state"], VerificationState.FAILED)

    def test_resolve_rejects_unverified_managed_artifact(self) -> None:
        remote, expected_commit = _make_bare_git_repository(self.temp)
        service = CatalogService(self.temp / "catalog")
        repository = service.add_git(str(remote), aliases=("unverified-managed",))
        artifact = service.fetch(
            repository["id"], ref="v1.0.0", default_branch=False, force=False
        )
        health = {
            "status": ArtifactStatus.READY,
            "verification_state": VerificationState.UNVERIFIED,
            "source_bytes": artifact["source_bytes"],
            "archive_bytes": artifact["archive_bytes"],
            "file_count": artifact["file_count"],
            "actual_commit": expected_commit,
        }

        with mock.patch.object(service.artifacts, "verify_record", return_value=health):
            with self.assertRaises(SourceUnavailableError):
                service.resolve(repository["id"], ref="v1.0.0")


class ArtifactSafetyTests(TemporaryDirectoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.temp / "catalog"
        self.manager = ArtifactManager(self.root)

    def test_github_archive_is_downloaded_by_resolved_commit_and_cache_is_checked(self) -> None:
        commit = "a" * 40
        requested_refs: list[str] = []

        class FakeGitHubClient:
            def download_archive(self, full_name, ref, destination, *, max_bytes):
                del full_name, max_bytes
                requested_refs.append(ref)
                content = b"exact source\n"
                with tarfile.open(destination, "w:gz") as archive:
                    member = tarfile.TarInfo("repository/source.txt")
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                return destination.stat().st_size

        resolved = ResolvedRef("v1.0.0", "v1.0.0", commit, ResolutionKind.EXACT_TAG)
        artifact = self.manager.acquire_github(
            repository_id="repo--github-exact",
            full_name="example/repository",
            resolved=resolved,
            client=FakeGitHubClient(),
            force=False,
        )
        self.assertEqual(requested_refs, [commit])
        self.assertEqual(artifact["actual_commit"], commit)
        health = self.manager.verify_record(
            {
                **artifact,
                "repository_id": "repo--github-exact",
                "actual_commit": None,
            }
        )
        self.assertEqual(health["verification_state"], VerificationState.VERIFIED)
        self.assertEqual(health["actual_commit"], commit)
        moved = ResolvedRef("v1.0.0", "v1.0.0", "b" * 40, ResolutionKind.EXACT_TAG)
        with self.assertRaises(ProviderError):
            self.manager.acquire_github(
                repository_id="repo--github-exact",
                full_name="example/repository",
                resolved=moved,
                client=FakeGitHubClient(),
                force=False,
            )

    def test_github_cache_reuse_requires_persisted_trusted_digests(self) -> None:
        commit = "d" * 40
        downloads = 0

        class FakeGitHubClient:
            def download_archive(self, full_name, ref, destination, *, max_bytes):
                nonlocal downloads
                del full_name, ref, max_bytes
                downloads += 1
                content = b"trusted source\n"
                with tarfile.open(destination, "w:gz") as archive:
                    member = tarfile.TarInfo("repository/source.txt")
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                return destination.stat().st_size

        repository_id = "repo--trusted-archive"
        resolved = ResolvedRef("v1", "v1", commit, ResolutionKind.EXACT_TAG)
        trusted = self.manager.acquire_github(
            repository_id=repository_id,
            full_name="example/trusted",
            resolved=resolved,
            client=FakeGitHubClient(),
            force=False,
        )
        source = Path(trusted["source_path"])
        (source / "malicious.txt").write_text("tampered", encoding="utf-8")
        manifest_path = source.parent / "artifact.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_digest"] = source_tree_digest(source)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(ProviderError):
            self.manager.acquire_github(
                repository_id=repository_id,
                full_name="example/trusted",
                resolved=resolved,
                client=FakeGitHubClient(),
                force=False,
                load_trusted=lambda: {**trusted, "repository_id": repository_id},
            )

        self.assertEqual(downloads, 1)

    def test_async_reconciliation_persists_tampered_artifact_health(self) -> None:
        commit = "c" * 40

        class FakeGitHubClient:
            def download_archive(self, full_name, ref, destination, *, max_bytes):
                del full_name, ref, max_bytes
                content = b"verified source\n"
                with tarfile.open(destination, "w:gz") as archive:
                    member = tarfile.TarInfo("repository/source.txt")
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                return destination.stat().st_size

        service = CatalogService(self.root)
        repository = service.store.add_repository(
            repository_id="repo--reconciled",
            provider="github",
            canonical_name="example/reconciled",
            display_name="reconciled",
            remote_url="https://github.com/example/reconciled.git",
            origin_kind="github",
        )
        resolved = ResolvedRef("v1", "v1", commit, ResolutionKind.EXACT_TAG)
        artifact = self.manager.acquire_github(
            repository_id=repository["id"],
            full_name="example/reconciled",
            resolved=resolved,
            client=FakeGitHubClient(),
            force=False,
        )
        service.store.upsert_artifact(repository["id"], artifact)
        (Path(artifact["source_path"]) / "tampered.txt").write_text(
            "tampered", encoding="utf-8"
        )

        service.store.reconcile_cached_metrics()

        persisted = service.store.artifact(artifact["id"])
        assert persisted is not None
        self.assertEqual(persisted["status"], ArtifactStatus.INVALID)
        self.assertEqual(persisted["verification_state"], VerificationState.FAILED)

    def test_failed_forced_acquisition_preserves_previous_valid_artifact(self) -> None:
        resolved = ResolvedRef("v1", "v1", "a" * 40, ResolutionKind.EXACT_TAG)

        def initial_fetch(remote_url: str, ref: str, destination: Path) -> str:
            destination.mkdir(parents=True)
            (destination / "state.txt").write_text("known-good\n", encoding="utf-8")
            return "a" * 40

        with (
            mock.patch("_catalog_artifacts.fetch_generic_git", side_effect=initial_fetch),
            mock.patch(
                "_catalog_artifacts.inspect_local_git",
                return_value={"commit_sha": "a" * 40, "dirty": False},
            ),
        ):
            artifact = self.manager.acquire_git(
                repository_id="repo--transactional",
                remote_url="https://example.invalid/repository.git",
                resolved=resolved,
                force=False,
            )
        source = Path(artifact["source_path"])
        self.assertEqual((source / "state.txt").read_text(encoding="utf-8"), "known-good\n")

        with mock.patch(
            "_catalog_artifacts.fetch_generic_git",
            side_effect=ProviderError("replacement download failed"),
        ):
            with self.assertRaises(ProviderError):
                self.manager.acquire_git(
                    repository_id="repo--transactional",
                    remote_url="https://example.invalid/repository.git",
                    resolved=resolved,
                    force=True,
                )

        self.assertTrue(source.is_dir())
        self.assertEqual((source / "state.txt").read_text(encoding="utf-8"), "known-good\n")
        staging = self.root / "staging"
        self.assertEqual(list(staging.iterdir()), [])

    def test_failed_post_promotion_validation_restores_previous_artifact(self) -> None:
        resolved = ResolvedRef("v1", "v1", "a" * 40, ResolutionKind.EXACT_TAG)

        def fetch_with_state(state: str):
            def fetch(remote_url: str, ref: str, destination: Path) -> str:
                del remote_url, ref
                destination.mkdir(parents=True)
                (destination / "state.txt").write_text(state, encoding="utf-8")
                return "a" * 40

            return fetch

        with (
            mock.patch(
                "_catalog_artifacts.fetch_generic_git",
                side_effect=fetch_with_state("known-good\n"),
            ),
            mock.patch(
                "_catalog_artifacts.inspect_local_git",
                return_value={"commit_sha": "a" * 40, "dirty": False},
            ),
        ):
            artifact = self.manager.acquire_git(
                repository_id="repo--post-promotion",
                remote_url="https://example.invalid/repository.git",
                resolved=resolved,
                force=False,
            )

        source = Path(artifact["source_path"])
        with (
            mock.patch(
                "_catalog_artifacts.fetch_generic_git",
                side_effect=fetch_with_state("unverified-replacement\n"),
            ),
            mock.patch(
                "_catalog_artifacts.inspect_local_git",
                side_effect=ProviderError("simulated post-promotion validation failure"),
            ),
        ):
            with self.assertRaises(ProviderError):
                self.manager.acquire_git(
                    repository_id="repo--post-promotion",
                    remote_url="https://example.invalid/repository.git",
                    resolved=resolved,
                    force=True,
                )

        self.assertEqual((source / "state.txt").read_text(encoding="utf-8"), "known-good\n")
        self.assertFalse(any(source.parent.glob(f".{artifact['id']}.previous-*")))

    def test_failed_catalog_commit_rolls_back_forced_filesystem_replacement(self) -> None:
        service = CatalogService(self.root)
        repository = service.add_git("https://example.invalid/transactional-store.git")
        resolved = ResolvedRef("v1", "v1", "a" * 40, ResolutionKind.EXACT_TAG)

        def fetch_with_state(state: str):
            def fetch(remote_url: str, ref: str, destination: Path) -> str:
                del remote_url, ref
                destination.mkdir(parents=True)
                (destination / "state.txt").write_text(state, encoding="utf-8")
                return "a" * 40

            return fetch

        clean_snapshot = {"commit_sha": "a" * 40, "dirty": False}
        with (
            mock.patch(
                "_catalog_artifacts.fetch_generic_git",
                side_effect=fetch_with_state("known-good\n"),
            ),
            mock.patch("_catalog_artifacts.inspect_local_git", return_value=clean_snapshot),
        ):
            initial = service._acquire(
                repository,
                resolved,
                force=False,
                nested_operation=False,
            )

        with (
            mock.patch(
                "_catalog_artifacts.fetch_generic_git",
                side_effect=fetch_with_state("replacement\n"),
            ),
            mock.patch("_catalog_artifacts.inspect_local_git", return_value=clean_snapshot),
            mock.patch.object(
                service.store,
                "upsert_artifact",
                side_effect=sqlite3.OperationalError("simulated catalog commit failure"),
            ),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                service._acquire(
                    repository,
                    resolved,
                    force=True,
                    nested_operation=False,
                )

        source = Path(initial["source_path"])
        self.assertEqual((source / "state.txt").read_text(encoding="utf-8"), "known-good\n")
        persisted = service.store.artifact(initial["id"])
        assert persisted is not None
        self.assertEqual(persisted["actual_commit"], "a" * 40)

    def test_generic_git_source_with_escaping_symlink_is_rejected(self) -> None:
        resolved = ResolvedRef("v1", "v1", "a" * 40, ResolutionKind.EXACT_TAG)

        def malicious_fetch(remote_url: str, ref: str, destination: Path) -> str:
            del remote_url, ref
            destination.mkdir(parents=True)
            try:
                (destination / "escape").symlink_to("../../outside")
            except (NotImplementedError, OSError) as exc:
                raise unittest.SkipTest(f"File symlinks are unavailable: {exc}")
            return "a" * 40

        with mock.patch("_catalog_artifacts.fetch_generic_git", side_effect=malicious_fetch):
            with self.assertRaises(ValidationError):
                self.manager.acquire_git(
                    repository_id="repo--malicious-git",
                    remote_url="https://example.invalid/repository.git",
                    resolved=resolved,
                    force=False,
                )

    def test_managed_source_root_symlink_is_rejected_even_within_artifact(self) -> None:
        repository_id = "repo--linked-source"
        identity = artifact_id(repository_id, "git_clone", "v1")
        artifact_root = self.root / "repos" / repository_id / "artifacts" / identity
        actual = artifact_root / "actual-source"
        actual.mkdir(parents=True)
        try:
            (artifact_root / "source").symlink_to(actual, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            raise unittest.SkipTest(f"Directory symlinks are unavailable: {exc}")

        with self.assertRaises(ProviderError):
            self.manager.validated_source_path(
                {
                    "id": identity,
                    "repository_id": repository_id,
                    "source_path": str(artifact_root / "source"),
                    "external": False,
                }
            )

    def test_promotion_rolls_back_if_staged_replace_fails(self) -> None:
        repository_id = "repo--rollback"
        identity = artifact_id(repository_id, "git_clone", "v1")
        final = self.root / "repos" / repository_id / "artifacts" / identity
        final.mkdir(parents=True)
        (final / "state.txt").write_text("old\n", encoding="utf-8")
        stage = self.manager._new_stage(identity)
        (stage / "state.txt").write_text("new\n", encoding="utf-8")
        real_replace = Path.replace

        def fail_staged_replace(path: Path, target: Path) -> Path:
            if path == stage:
                raise OSError("simulated promotion failure")
            return real_replace(path, target)

        with mock.patch.object(Path, "replace", new=fail_staged_replace):
            with self.assertRaises(OSError):
                with self.manager._promote(repository_id, identity, stage, force=True):
                    pass

        self.assertEqual((final / "state.txt").read_text(encoding="utf-8"), "old\n")
        self.assertFalse(any(final.parent.glob(f".{identity}.previous-*")))

    def test_interrupted_force_replacement_restores_backup_and_cleans_staging(self) -> None:
        repository_id = "repo--recovery"
        identity = artifact_id(repository_id, "git_clone", "v1")
        artifacts_root = self.root / "repos" / repository_id / "artifacts"
        artifacts_root.mkdir(parents=True)
        backup = artifacts_root / f".{identity}.previous-crash"
        backup.mkdir()
        (backup / "known-good.txt").write_text("good", encoding="utf-8")
        orphan = self.root / "staging" / f"{identity}-orphan"
        orphan.mkdir(parents=True)
        self.manager._recover_artifact(repository_id, identity)
        final = artifacts_root / identity
        self.assertEqual((final / "known-good.txt").read_text(encoding="utf-8"), "good")
        self.assertFalse(orphan.exists())

    def test_interrupted_force_replacement_rolls_back_unvalidated_final(self) -> None:
        repository_id = "repo--recovery-with-final"
        identity = artifact_id(repository_id, "git_clone", "v1")
        artifacts_root = self.root / "repos" / repository_id / "artifacts"
        final = artifacts_root / identity
        backup = artifacts_root / f".{identity}.previous-crash"
        final.mkdir(parents=True)
        backup.mkdir()
        (final / "state.txt").write_text("unvalidated", encoding="utf-8")
        (backup / "state.txt").write_text("known-good", encoding="utf-8")

        self.manager._recover_artifact(repository_id, identity)

        self.assertEqual((final / "state.txt").read_text(encoding="utf-8"), "known-good")
        self.assertFalse(backup.exists())

    def test_source_digest_rejects_special_files(self) -> None:
        source = self.temp / "special-source"
        source.mkdir()
        fifo = source / "pipe"
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO files are unavailable")
        try:
            os.mkfifo(fifo)
        except OSError as exc:
            self.skipTest(f"FIFO files are unavailable: {exc}")

        with self.assertRaises(ValidationError):
            source_tree_digest(source)

    def test_archive_traversal_and_links_are_rejected(self) -> None:
        destination = self.temp / "expanded"
        destination.mkdir()
        traversal_archive = self.temp / "traversal.tar.gz"
        with tarfile.open(traversal_archive, "w:gz") as archive:
            payload = b"escape"
            member = tarfile.TarInfo("../outside.txt")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        with self.assertRaises(ValidationError):
            safe_extract_tar(traversal_archive, destination)
        self.assertFalse((self.temp / "outside.txt").exists())

        link_archive = self.temp / "link.tar.gz"
        with tarfile.open(link_archive, "w:gz") as archive:
            member = tarfile.TarInfo("source/link")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside.txt"
            archive.addfile(member)
        with self.assertRaises(ValidationError):
            safe_extract_tar(link_archive, destination)
        self.assertFalse((destination / "source" / "link").exists())

    def test_ref_artifact_ids_are_path_safe_and_collision_resistant(self) -> None:
        slash = artifact_id("repo-a", "git_clone", "feature/a")
        dash = artifact_id("repo-a", "git_clone", "feature-a")
        other_kind = artifact_id("repo-a", "github_archive", "feature/a")
        other_repository = artifact_id("repo-b", "git_clone", "feature/a")
        self.assertNotEqual(slash, dash)
        self.assertNotEqual(slash, other_kind)
        self.assertNotEqual(slash, other_repository)
        for identity in (slash, dash, other_kind, other_repository):
            self.assertRegex(identity, r"^artifact--[a-z0-9-]+--[0-9a-f]{24}$")
            self.assertNotIn("/", identity)
            self.assertNotIn("..", identity)

    def test_source_digest_distinguishes_file_and_directory_entries(self) -> None:
        root = self.temp / "digest-tree"
        root.mkdir()
        entry = root / "entry"
        entry.mkdir(mode=0o755)
        directory_digest = source_tree_digest(root)
        entry.rmdir()
        entry.write_bytes(b"")
        entry.chmod(0o755)
        self.assertNotEqual(directory_digest, source_tree_digest(root))

    def test_guarded_deletion_rejects_root_outside_and_symlink_escape(self) -> None:
        managed_root = self.temp / "managed"
        managed_root.mkdir()
        child = managed_root / "child"
        child.mkdir()
        (child / "data.txt").write_text("delete me", encoding="utf-8")
        guarded_remove(managed_root, child)
        self.assertFalse(child.exists())

        outside = self.temp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ValidationError):
            guarded_remove(managed_root, managed_root)
        with self.assertRaises(ValidationError):
            guarded_remove(managed_root, outside)
        self.assertTrue((outside / "keep.txt").is_file())

        link = managed_root / "escape-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")
        with self.assertRaises(ValidationError):
            guarded_remove(managed_root, link)
        self.assertTrue((outside / "keep.txt").is_file())

    def test_managed_repository_symlink_cannot_redirect_artifact_promotion(self) -> None:
        repos = self.root / "repos"
        repos.mkdir(parents=True)
        outside = self.temp / "outside-promotion"
        outside.mkdir()
        repository_id = "repo--symlinked"
        try:
            (repos / repository_id).symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")
        identity = artifact_id(repository_id, "git_clone", "v1")
        stage = self.manager._new_stage(identity)
        (stage / "source").mkdir()
        with self.assertRaises(ValidationError):
            with self.manager._promote(repository_id, identity, stage, force=False):
                pass
        self.assertEqual(list(outside.iterdir()), [])

    def test_artifact_locks_serialize_same_artifact_but_not_different_artifacts(self) -> None:
        locks = self.temp / "locks"
        first_path = locks / "artifact-a.lock"
        other_path = locks / "artifact-b.lock"
        first = ArtifactLock(first_path, timeout=2)
        first.acquire()
        waiting = threading.Event()
        acquired = threading.Event()
        failure: list[BaseException] = []

        def contender() -> None:
            try:
                second = ArtifactLock(first_path, timeout=2)
                second.acquire(on_wait=waiting.set)
                acquired.set()
                second.release()
            except BaseException as exc:  # pragma: no cover - surfaced by the main assertion
                failure.append(exc)

        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        self.assertTrue(waiting.wait(timeout=1), "same-artifact contender did not report waiting")
        self.assertFalse(acquired.is_set())

        independent = ArtifactLock(other_path, timeout=0.2)
        independent.acquire()
        independent.release()
        first.release()
        thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failure, [])
        self.assertTrue(acquired.is_set())

    def test_artifact_lock_releases_when_acquired_callback_fails(self) -> None:
        lock_path = self.temp / "locks" / "callback.lock"
        owner = ArtifactLock(lock_path, timeout=2)
        owner.acquire()
        waiting = threading.Event()
        failures: list[BaseException] = []

        def contender() -> None:
            try:
                ArtifactLock(lock_path, timeout=2).acquire(
                    on_wait=waiting.set,
                    on_acquired=lambda: (_ for _ in ()).throw(RuntimeError("callback failed")),
                )
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        self.assertTrue(waiting.wait(timeout=1))
        owner.release()
        thread.join(timeout=3)

        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeError)
        subsequent = ArtifactLock(lock_path, timeout=0.5)
        subsequent.acquire()
        subsequent.release()

    def test_lock_file_symlink_is_rejected_without_touching_target(self) -> None:
        victim = self.temp / "lock-victim"
        victim.write_text("keep", encoding="utf-8")
        lock_path = self.temp / "malicious.lock"
        try:
            lock_path.symlink_to(victim)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"File symlinks are unavailable: {exc}")
        with self.assertRaises(ValidationError):
            ArtifactLock(lock_path).acquire()
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")


class CredentialSafetyTests(TemporaryDirectoryTestCase):
    def test_diagnostic_redaction_is_bounded_for_long_non_url_text(self) -> None:
        program = "\n".join(
            (
                "import sys",
                f"sys.path.insert(0, {str(SCRIPTS_ROOT)!r})",
                "from _catalog_paths import redact_text",
                "value = 'failure token=must-not-leak ' + 'x' * 5000",
                "for _ in range(100):",
                "    redact_text(value)",
            )
        )
        try:
            subprocess.run(
                [sys.executable, "-c", program],
                check=True,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            self.fail("Diagnostic redaction exceeded its bounded runtime budget")

    def test_remote_and_diagnostic_credentials_are_redacted_before_persistence(self) -> None:
        raw_remote = "https://user:password@example.invalid/org/repo.git?token=secret#fragment"
        self.assertEqual(
            sanitize_remote(raw_remote), "https://example.invalid/org/repo.git"
        )
        self.assertEqual(
            sanitize_remote("git@github.com:owner/repository.git"),
            "git@github.com:owner/repository.git",
        )

        token = "ghp_test_token_that_must_not_leak"
        with mock.patch.dict(os.environ, {"GH_TOKEN": token}, clear=False):
            redacted = redact_text(
                f"request https://alice:pw@example.invalid/private failed with {token}"
            )
            self.assertNotIn("alice", redacted)
            self.assertNotIn("pw", redacted)
            self.assertNotIn(token, redacted)

            service = CatalogService(self.temp / "catalog")
            repository = service.add_git(raw_remote)
            self.assertEqual(
                repository["remote_url"], "https://example.invalid/org/repo.git"
            )
            operation = service.store.create_operation("repo fetch", target="credentials")
            service.store.update_operation(
                operation,
                status=OperationStatus.FAILED,
                phase="failed",
                message=f"provider rejected {raw_remote} using {token}",
                error_code="provider_error",
                error_message=f"provider rejected {raw_remote} using {token}",
            )
            persisted = next(
                item for item in service.store.operations() if item["id"] == operation
            )
            self.assertNotIn("password", persisted["message"])
            self.assertNotIn(token, persisted["message"])
            self.assertNotIn("password", persisted["error_message"])
            self.assertNotIn(token, persisted["error_message"])


class CliContractTests(TemporaryDirectoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.temp / "catalog"
        self.source = _make_source(self.temp, "cli-source", version="7.8.9")
        service = CatalogService(self.root)
        service.add_local(self.source, aliases=("cli-widget",))

    def test_resolve_json_and_text_contracts_are_stable(self) -> None:
        code, stdout, stderr = _invoke_cli(
            ["--catalog-root", str(self.root), "resolve", "cli-widget", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(Path(payload["source_path"]), self.source.resolve())
        self.assertEqual(payload["verification_state"], "unverified")
        self.assertEqual(payload["resolution_kind"], "unresolved")
        self.assertEqual(
            {
                "id",
                "canonical_name",
                "display_name",
                "provider",
                "remote_url",
                "aliases",
            },
            set(payload["repository"]),
        )
        self.assertIn("id", payload["artifact"])
        self.assertIn("actual_commit", payload["artifact"])
        self.assertIsNone(payload["package"])

        code, stdout, stderr = _invoke_cli(
            ["--catalog-root", str(self.root), "resolve", "cli-widget"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        lines = stdout.splitlines()
        self.assertEqual(lines[0], str(self.source.resolve()))
        self.assertTrue(any("repository:" in line for line in lines[1:]))
        self.assertTrue(any("provenance: unresolved (unverified)" in line for line in lines))

    def test_dashboard_validation_uses_stable_error_code_and_nested_help(self) -> None:
        code, stdout, stderr = _invoke_cli(
            [
                "--catalog-root",
                str(self.root),
                "dashboard",
                "start",
                "--port",
                "0",
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error [dashboard_error]", stderr)

        help_output = io.StringIO()
        with redirect_stdout(help_output), self.assertRaises(SystemExit) as raised:
            cli.build_parser().parse_args(["dashboard", "start", "--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--port", help_output.getvalue())

    def test_not_found_source_unavailable_and_ambiguous_exit_contracts(self) -> None:
        code, stdout, stderr = _invoke_cli(
            ["--catalog-root", str(self.root), "resolve", "does-not-exist", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        error = json.loads(stdout)
        self.assertEqual(error["status"], "error")
        self.assertEqual(error["error"]["code"], "not_found")

        code, stdout, stderr = _invoke_cli(
            [
                "--catalog-root",
                str(self.root),
                "resolve",
                "cli-widget",
                "--ref",
                "missing-ref",
                "--json",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["error"]["code"], "source_unavailable")

        service = CatalogService(self.root)
        service.add_local(_make_source(self.temp, "shared-one"), aliases=("shared",))
        service.add_local(_make_source(self.temp, "shared-two"), aliases=("shared",))
        code, stdout, stderr = _invoke_cli(
            ["--catalog-root", str(self.root), "resolve", "shared", "--json"]
        )
        self.assertEqual(code, 3)
        self.assertEqual(stderr, "")
        ambiguous = json.loads(stdout)
        self.assertEqual(ambiguous["error"]["code"], "ambiguous")
        self.assertEqual(len(ambiguous["error"]["candidates"]), 2)


class ProviderSafetyTests(unittest.TestCase):
    def test_local_version_detection_never_reads_linked_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            secret = root / "secret.json"
            secret.write_text('{"version":"TOP-SECRET-CANARY"}', encoding="utf-8")
            try:
                (source / "package.json").symlink_to(secret)
            except (NotImplementedError, OSError) as exc:
                raise unittest.SkipTest(f"File symlinks are unavailable: {exc}")

            metadata = inspect_local_source(source)

            self.assertIsNone(metadata["detected_version"])
            self.assertNotIn("package.json", metadata["markers"])
            self.assertNotIn("TOP-SECRET-CANARY", json.dumps(metadata))

    def test_nuget_non_commit_revision_is_never_claimed_as_exact(self) -> None:
        metadata = NuGetMetadata(
            package_id="Example.Package",
            version="1.2.3",
            repository_url="https://example.invalid/repository.git",
            repository_commit="main",
            project_url=None,
        )
        with self.assertRaises(ProviderError):
            choose_nuget_ref(metadata, [{"name": "v1.2.3", "commit_sha": "a" * 40}])

    def test_nuget_github_short_commit_is_expanded_and_must_match_its_prefix(self) -> None:
        short_commit = "abc1234"
        full_commit = short_commit + "d" * (40 - len(short_commit))
        metadata = NuGetMetadata(
            package_id="Example.Package",
            version="1.2.3",
            repository_url="https://github.com/example/repository.git",
            repository_commit=short_commit,
            project_url=None,
        )
        repository_metadata = {
            "full_name": "example/repository",
            "display_name": "repository",
            "remote_url": "https://github.com/example/repository.git",
        }

        temporary = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temporary, ignore_errors=True)
        service = CatalogService(temporary / "catalog")

        def stop_after_expansion(repository, resolved, **kwargs):
            del repository, kwargs
            self.assertEqual(resolved.commit_sha, full_commit)
            raise ProviderError("stop after expansion")

        with (
            mock.patch("_catalog_service.NuGetClient.metadata", return_value=metadata),
            mock.patch(
                "_catalog_service.GitHubClient.repository", return_value=repository_metadata
            ),
            mock.patch(
                "_catalog_service.GitHubClient.resolve_ref",
                return_value=ResolvedRef(
                    short_commit,
                    short_commit,
                    full_commit,
                    ResolutionKind.EXACT_COMMIT,
                ),
            ),
            mock.patch.object(service, "_acquire", side_effect=stop_after_expansion),
        ):
            with self.assertRaisesRegex(ProviderError, "stop after expansion"):
                service.fetch_nuget("Example.Package", "1.2.3", force=False)

        with (
            mock.patch("_catalog_service.NuGetClient.metadata", return_value=metadata),
            mock.patch(
                "_catalog_service.GitHubClient.resolve_ref",
                return_value=ResolvedRef(
                    short_commit,
                    short_commit,
                    "f" * 40,
                    ResolutionKind.EXACT_COMMIT,
                ),
            ),
            mock.patch.object(service, "_acquire") as acquire,
        ):
            with self.assertRaisesRegex(ProviderError, "different commit"):
                service.fetch_nuget("Example.Package", "1.2.3", force=False)
            acquire.assert_not_called()

    def test_git_remote_helper_and_untrusted_file_transports_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            validate_git_remote("ext::sh -c touch /tmp/should-not-exist")
        with self.assertRaises(ValidationError):
            validate_git_remote("file:///tmp/repository.git", allow_file=False)
        self.assertEqual(
            validate_git_remote("ssh://git@example.com/team/repository.git"),
            "ssh://git@example.com/team/repository.git",
        )
        self.assertEqual(
            sanitize_remote("https://user:secret@[::1]:8443/team/repository.git?token=secret"),
            "https://[::1]:8443/team/repository.git",
        )
        with self.assertRaises(ValidationError):
            sanitize_remote("https://[broken/repository.git")


if __name__ == "__main__":
    unittest.main()
