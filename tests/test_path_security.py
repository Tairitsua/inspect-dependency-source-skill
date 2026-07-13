"""Focused catalog-root path security regressions."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from _catalog_models import ValidationError
from _catalog_paths import (
    ArtifactLock,
    artifact_id,
    cleanup_stale_staging,
    ensure_catalog_layout,
    resolve_catalog_root,
)
from _catalog_service import CatalogService
from _catalog_store import CatalogStore


class CatalogRootSecurityTests(unittest.TestCase):
    def test_catalog_root_symlink_is_rejected_before_canonicalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            target = parent / "target"
            target.mkdir()
            link = parent / "catalog-link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")

            with self.assertRaises(ValidationError):
                resolve_catalog_root(link)
            with self.assertRaises(ValidationError):
                CatalogStore(link)
            with self.assertRaises(ValidationError):
                CatalogService(link)

    def test_stale_stage_cleanup_uses_the_matching_artifact_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "catalog"
            ensure_catalog_layout(root)
            repository_id = "repo--janitor"
            identity = artifact_id(repository_id, "git_clone", "v1")
            lock_path = root / "locks" / f"{repository_id}--{identity}.lock"
            lock = ArtifactLock(lock_path, timeout=1)
            lock.acquire()
            lock.release()
            old_time = time.time() - 48 * 60 * 60

            stale = root / "staging" / f"{identity}-{'a' * 32}"
            stale.mkdir()
            stale.touch()
            stale.chmod(0o700)
            os.utime(stale, (old_time, old_time))
            recent = root / "staging" / f"{identity}-{'b' * 32}"
            recent.mkdir()

            result = cleanup_stale_staging(root)

            self.assertEqual(result["removed"], 1)
            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())

    def test_active_artifact_lock_preserves_even_an_old_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "catalog"
            ensure_catalog_layout(root)
            repository_id = "repo--active"
            identity = artifact_id(repository_id, "git_clone", "v1")
            stage = root / "staging" / f"{identity}-{'c' * 32}"
            stage.mkdir()
            old_time = time.time() - 48 * 60 * 60
            os.utime(stage, (old_time, old_time))
            owner = ArtifactLock(
                root / "locks" / f"{repository_id}--{identity}.lock", timeout=1
            )
            owner.acquire()
            try:
                result = cleanup_stale_staging(root)
            finally:
                owner.release()

            self.assertEqual(result["busy"], 1)
            self.assertTrue(stage.exists())

    def test_service_startup_cleans_stale_generated_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "catalog"
            ensure_catalog_layout(root)
            repository_id = "repo--startup"
            identity = artifact_id(repository_id, "git_clone", "v1")
            lock_path = root / "locks" / f"{repository_id}--{identity}.lock"
            lock = ArtifactLock(lock_path, timeout=1)
            lock.acquire()
            lock.release()
            stage = root / "staging" / f"{identity}-{'d' * 32}"
            stage.mkdir()
            old_time = time.time() - 48 * 60 * 60
            os.utime(stage, (old_time, old_time))

            service = CatalogService(root)

            self.assertEqual(service.staging_cleanup["removed"], 1)
            self.assertFalse(stage.exists())


if __name__ == "__main__":
    unittest.main()
