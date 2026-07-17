"""Safety contracts for previewing and executing repository removal."""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import inspect_dependency_source as cli
import _catalog_service as catalog_service_module
from _catalog_models import AmbiguousError, NotFoundError, ValidationError
from _catalog_service import CatalogService


def _invoke_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main(arguments)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class RemovalSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed_hybrid_repository(
        self,
    ) -> tuple[CatalogService, Path, Path, Path, str]:
        external_source = self.root / "user-owned-source"
        external_source.mkdir()
        external_marker = external_source / "keep.txt"
        external_marker.write_text("preserve me\n", encoding="utf-8")

        catalog_root = self.root / "catalog"
        service = CatalogService(catalog_root)
        repository = service.add_local(external_source, aliases=("remove-target",))
        managed_cache = service.artifacts.repository_cache_path(repository["id"])
        managed_source = managed_cache / "artifacts" / "managed-artifact" / "source"
        managed_source.mkdir(parents=True)
        (managed_source / "delete.txt").write_text("managed\n", encoding="utf-8")
        service.store.upsert_artifact(
            repository["id"],
            {
                "id": "managed-artifact",
                "kind": "git_clone",
                "ref": "v1.0.0",
                "source_path": str(managed_source),
                "status": "ready",
                "resolution_kind": "exact_tag",
                "verification_state": "verified",
                "external": False,
            },
            prefer=False,
        )
        return (
            service,
            catalog_root,
            external_source,
            managed_cache,
            repository["id"],
        )

    def test_plan_is_stable_and_confirmed_purge_preserves_local_source(self) -> None:
        service, _catalog, external_source, managed_cache, repository_id = (
            self._seed_hybrid_repository()
        )

        plan = service.removal_plan("remove-target")

        self.assertEqual(plan["status"], "ok")
        self.assertEqual(plan["repository"]["id"], repository_id)
        self.assertTrue(plan["purge"]["safe_to_purge"])
        self.assertTrue(plan["purge"]["local_sources_excluded"])
        self.assertRegex(plan["plan_token"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            service.removal_plan(repository_id)["plan_token"], plan["plan_token"]
        )
        self.assertEqual(
            [item["id"] for item in plan["purge"]["managed_artifacts"]],
            ["managed-artifact"],
        )
        self.assertEqual(
            [item["path"] for item in plan["purge"]["preserved_local_sources"]],
            [str(external_source)],
        )
        self.assertTrue(managed_cache.exists())
        self.assertTrue((external_source / "keep.txt").exists())
        self.assertIsNotNone(service.store.repository(repository_id))

        with self.assertRaisesRegex(ValidationError, "requires --yes"):
            service.remove(repository_id, purge_managed_cache=True, yes=False)
        with self.assertRaisesRegex(ValidationError, "requires --purge-managed-cache"):
            service.remove(repository_id, purge_managed_cache=False, yes=True)
        with self.assertRaisesRegex(ValidationError, "exact repository.id"):
            service.remove("remove-target", purge_managed_cache=False, yes=False)
        with self.assertRaisesRegex(ValidationError, "requires --plan-token"):
            service.remove(repository_id, purge_managed_cache=False, yes=False)

        result = service.remove(
            repository_id,
            purge_managed_cache=True,
            yes=True,
            plan_token=plan["plan_token"],
        )

        self.assertTrue(result["purged_managed_cache"])
        self.assertFalse(managed_cache.exists())
        self.assertTrue((external_source / "keep.txt").exists())
        self.assertTrue(result["preserved_local_sources"][0]["exists_after"])
        with self.assertRaises(NotFoundError):
            service.store.repository(repository_id)

    def test_metadata_only_removal_preserves_managed_and_external_files(self) -> None:
        service, _catalog, external_source, managed_cache, repository_id = (
            self._seed_hybrid_repository()
        )

        plan = service.removal_plan(repository_id)
        result = service.remove(
            repository_id,
            purge_managed_cache=False,
            yes=False,
            plan_token=plan["plan_token"],
        )

        self.assertFalse(result["purged_managed_cache"])
        self.assertTrue(managed_cache.exists())
        self.assertTrue((external_source / "keep.txt").exists())
        with self.assertRaises(NotFoundError):
            service.store.repository(repository_id)

    def test_external_path_overlapping_managed_cache_blocks_purge(self) -> None:
        service, _catalog, external_source, managed_cache, repository_id = (
            self._seed_hybrid_repository()
        )
        overlapping = managed_cache / "artifacts" / "misclassified" / "source"
        overlapping.mkdir(parents=True)
        service.store.upsert_artifact(
            repository_id,
            {
                "id": "misclassified-external",
                "kind": "local",
                "ref": "working-tree",
                "source_path": str(overlapping),
                "status": "ready",
                "resolution_kind": "unresolved",
                "verification_state": "unverified",
                "external": True,
            },
            prefer=False,
        )
        service.store.upsert_artifact(
            repository_id,
            {
                "id": "misclassified-ancestor",
                "kind": "local",
                "ref": "working-tree-ancestor",
                "source_path": str(managed_cache.parent),
                "status": "ready",
                "resolution_kind": "unresolved",
                "verification_state": "unverified",
                "external": True,
            },
            prefer=False,
        )

        plan = service.removal_plan("remove-target")

        self.assertFalse(plan["purge"]["safe_to_purge"])
        self.assertFalse(plan["purge"]["local_sources_excluded"])
        self.assertTrue(plan["purge"]["blocking_reasons"])
        self.assertTrue(
            any(
                str(managed_cache.parent) in reason
                for reason in plan["purge"]["blocking_reasons"]
            )
        )
        with self.assertRaisesRegex(ValidationError, "unsafe removal plan"):
            service.remove(
                repository_id,
                purge_managed_cache=True,
                yes=True,
                plan_token=plan["plan_token"],
            )
        self.assertTrue(managed_cache.exists())
        self.assertTrue(external_source.exists())

    def test_cli_plan_is_json_and_ambiguous_queries_fail_closed(self) -> None:
        service, catalog_root, _external, managed_cache, _repository_id = (
            self._seed_hybrid_repository()
        )
        before = service.store.summary()

        code, stdout, stderr = _invoke_cli(
            [
                "--catalog-root",
                str(catalog_root),
                "repo",
                "remove",
                "remove-target",
                "--plan",
                "--json",
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["operation"], "repository_removal_plan")
        self.assertRegex(payload["plan_token"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(payload["purge"]["requires_explicit_authorization"])
        after_store = CatalogService(catalog_root).store
        after = after_store.summary()
        for key in (
            "repository_count",
            "local_repository_count",
            "artifact_count",
            "ready_artifact_count",
        ):
            self.assertEqual(after[key], before[key])
        self.assertEqual(
            after_store.repository(payload["repository"]["id"])["canonical_name"],
            payload["repository"]["canonical_name"],
        )
        self.assertTrue(managed_cache.exists())

        for suffix in ("one", "two"):
            service.store.add_repository(
                repository_id=f"ambiguous-{suffix}",
                provider="git",
                canonical_name=f"example/shared-{suffix}",
                display_name=f"Shared {suffix}",
                remote_url=f"https://example.invalid/shared-{suffix}.git",
                origin_kind="git",
            )
        with self.assertRaises(AmbiguousError):
            service.removal_plan("shared")

    def test_local_registration_rejects_catalog_path_overlap(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        catalog_root = workspace / "catalog"
        service = CatalogService(catalog_root)

        inside_catalog = catalog_root / "repos" / "user-owned-checkout"
        inside_catalog.mkdir(parents=True)
        (inside_catalog / "pyproject.toml").write_text(
            '[project]\nname = "unsafe-local"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "must not overlap"):
            service.add_local(inside_catalog)

        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "catalog-parent"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "must not overlap"):
            service.add_local(workspace)

        with self.assertRaisesRegex(ValidationError, "must not be inside"):
            service.scan_local(inside_catalog, max_depth=1, update_existing=False)

    def test_retargeted_registered_path_blocks_removal_plan(self) -> None:
        service, _catalog, external_source, managed_cache, repository_id = (
            self._seed_hybrid_repository()
        )
        shutil.rmtree(external_source)
        try:
            external_source.symlink_to(external_source, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")

        plan = service.removal_plan(repository_id)

        self.assertFalse(plan["purge"]["local_sources_excluded"])
        self.assertFalse(plan["purge"]["safe_to_purge"])
        self.assertFalse(
            plan["purge"]["preserved_local_sources"][0]["path_classified"]
        )
        self.assertTrue(
            any(
                "cannot be safely classified" in reason
                for reason in plan["purge"]["blocking_reasons"]
            )
        )
        with self.assertRaisesRegex(ValidationError, "unsafe removal plan"):
            service.remove(
                repository_id,
                purge_managed_cache=True,
                yes=True,
                plan_token=plan["plan_token"],
            )
        self.assertTrue(managed_cache.exists())

    def test_post_verification_detects_preserved_source_loss(self) -> None:
        service, _catalog, external_source, _managed_cache, repository_id = (
            self._seed_hybrid_repository()
        )
        guarded_purge = service.artifacts.purge_repository

        def simulate_regressed_purge(target_repository_id: str) -> None:
            guarded_purge(target_repository_id)
            shutil.rmtree(external_source)

        service.artifacts.purge_repository = simulate_regressed_purge  # type: ignore[method-assign]
        plan = service.removal_plan(repository_id)

        with self.assertRaisesRegex(
            ValidationError, "post-verification failed"
        ):
            service.remove(
                repository_id,
                purge_managed_cache=True,
                yes=True,
                plan_token=plan["plan_token"],
            )

        self.assertIsNotNone(service.store.repository(repository_id))

    def test_changed_repository_state_invalidates_authorized_plan(self) -> None:
        service, _catalog, external_source, managed_cache, repository_id = (
            self._seed_hybrid_repository()
        )
        authorized = service.removal_plan(repository_id)
        late_source = managed_cache / "artifacts" / "late-artifact" / "source"
        late_source.mkdir(parents=True)
        (late_source / "late.txt").write_text("late\n", encoding="utf-8")
        service.store.upsert_artifact(
            repository_id,
            {
                "id": "late-artifact",
                "kind": "git_clone",
                "ref": "v2.0.0",
                "source_path": str(late_source),
                "status": "ready",
                "resolution_kind": "exact_tag",
                "verification_state": "verified",
                "external": False,
            },
            prefer=False,
        )

        with self.assertRaisesRegex(ValidationError, "plan changed"):
            service.remove(
                repository_id,
                purge_managed_cache=True,
                yes=True,
                plan_token=authorized["plan_token"],
            )

        self.assertTrue(managed_cache.exists())
        self.assertTrue(late_source.exists())
        self.assertTrue(external_source.exists())
        self.assertIsNotNone(service.store.repository(repository_id))

    def test_dashboard_reconciliation_does_not_invalidate_unchanged_plan(self) -> None:
        service, _catalog, _external, _managed_cache, repository_id = (
            self._seed_hybrid_repository()
        )
        authorized = service.removal_plan(repository_id)

        service.store.reconcile_cached_metrics()

        reconciled = service.removal_plan(repository_id)
        self.assertEqual(reconciled, authorized)

    def test_same_count_metadata_replacement_invalidates_authorized_plan(self) -> None:
        service, _catalog, external_source, managed_cache, repository_id = (
            self._seed_hybrid_repository()
        )
        authorized = service.removal_plan(repository_id)

        with service.store.connect(write=True) as connection:
            changed = connection.execute(
                """
                UPDATE aliases
                SET alias='replacement-alias',normalized_alias='replacement alias'
                WHERE repository_id=? AND normalized_alias='remove target'
                """,
                (repository_id,),
            ).rowcount
        self.assertEqual(changed, 1)

        replacement = service.removal_plan(repository_id)
        self.assertEqual(
            replacement["metadata_removal"]["aliases"],
            authorized["metadata_removal"]["aliases"],
        )
        self.assertNotEqual(
            replacement["metadata_removal"]["deletion_set_digest"],
            authorized["metadata_removal"]["deletion_set_digest"],
        )
        self.assertNotEqual(replacement["plan_token"], authorized["plan_token"])
        with self.assertRaisesRegex(ValidationError, "plan changed"):
            service.remove(
                repository_id,
                purge_managed_cache=True,
                yes=True,
                plan_token=authorized["plan_token"],
            )
        self.assertTrue(managed_cache.exists())
        self.assertTrue(external_source.exists())
        self.assertIsNotNone(service.store.repository(repository_id))

    def test_registration_manifest_write_cannot_escape_concurrent_purge(self) -> None:
        catalog_root = self.root / "race-catalog"
        registrar = CatalogService(catalog_root)
        remover = CatalogService(catalog_root)
        stub_started = threading.Event()
        allow_stub_write = threading.Event()
        removal_finished = threading.Event()
        failures: list[BaseException] = []
        original_write = catalog_service_module.atomic_write_json

        def paused_write(path: Path, payload: object) -> None:
            if path.name == "manifest.stub.json":
                stub_started.set()
                if not allow_stub_write.wait(timeout=5):
                    raise TimeoutError("Timed out waiting to release the stub writer.")
            original_write(path, payload)

        def register() -> None:
            try:
                registrar.add_git("https://example.invalid/team/race.git")
            except BaseException as exc:  # pragma: no cover - asserted in parent thread
                failures.append(exc)

        def remove(repository_id: str, plan_token: str) -> None:
            try:
                remover.remove(
                    repository_id,
                    purge_managed_cache=True,
                    yes=True,
                    plan_token=plan_token,
                )
            except BaseException as exc:  # pragma: no cover - asserted in parent thread
                failures.append(exc)
            finally:
                removal_finished.set()

        with mock.patch.object(
            catalog_service_module, "atomic_write_json", side_effect=paused_write
        ):
            registration_thread = threading.Thread(target=register, daemon=True)
            registration_thread.start()
            self.assertTrue(stub_started.wait(timeout=2))
            repositories = remover.store.repositories()
            self.assertEqual(len(repositories), 1)
            repository_id = repositories[0]["id"]
            plan = remover.removal_plan(repository_id)

            removal_thread = threading.Thread(
                target=remove,
                args=(repository_id, plan["plan_token"]),
                daemon=True,
            )
            removal_thread.start()
            self.assertFalse(removal_finished.wait(timeout=0.2))
            allow_stub_write.set()
            registration_thread.join(timeout=5)
            removal_thread.join(timeout=5)

        self.assertFalse(registration_thread.is_alive())
        self.assertFalse(removal_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse(remover.artifacts.repository_cache_path(repository_id).exists())
        with self.assertRaises(NotFoundError):
            remover.store.repository(repository_id)

    def test_repository_guard_serializes_acquisition_and_removal_scope(self) -> None:
        service, catalog_root, _external, _managed_cache, repository_id = (
            self._seed_hybrid_repository()
        )
        contender = CatalogService(catalog_root)
        waiting = threading.Event()
        entered = threading.Event()

        def enter_same_repository() -> None:
            with contender.artifacts.repository_guard(
                repository_id, on_wait=waiting.set
            ):
                entered.set()

        with service.artifacts.repository_guard(repository_id):
            thread = threading.Thread(target=enter_same_repository, daemon=True)
            thread.start()
            self.assertTrue(waiting.wait(timeout=2))
            self.assertFalse(entered.is_set())

        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(entered.is_set())


if __name__ == "__main__":
    unittest.main()
