"""Focused tests for bounded global-catalog reconciliation."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from _catalog_artifacts import ArtifactManager
from _catalog_service import CatalogService


class ReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self._temporary.name)
        self.root = self.temp / "catalog"
        self.source = self.temp / "dependency"
        self.source.mkdir()
        (self.source / "pyproject.toml").write_text(
            '[project]\nname = "dependency"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        self.service = CatalogService(self.root)
        self.repository = self.service.add_local(self.source)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_full_artifact_hashing_is_throttled_but_can_be_forced(self) -> None:
        manager = ArtifactManager(self.root)
        with mock.patch.object(
            ArtifactManager,
            "verify_record",
            side_effect=lambda _manager, artifact: manager.verify_record(artifact),
            autospec=True,
        ) as verifier:
            first = self.service.store.reconcile_cached_metrics()
            first_count = verifier.call_count
            second = self.service.store.reconcile_cached_metrics()
            second_count = verifier.call_count
            forced = self.service.store.reconcile_cached_metrics(force_deep=True)

        self.assertTrue(first["deep_reconciled"])
        self.assertGreater(first_count, 0)
        self.assertFalse(second["deep_reconciled"])
        self.assertEqual(second_count, first_count)
        self.assertTrue(forced["deep_reconciled"])
        self.assertGreater(verifier.call_count, second_count)

    def test_shallow_cycle_refreshes_local_git_snapshot(self) -> None:
        self.service.store.reconcile_cached_metrics()
        current_commit = "b" * 40
        with (
            mock.patch(
                "_catalog_providers.inspect_local_git",
                return_value={
                    "branch": "release/2.0",
                    "commit_sha": current_commit,
                    "dirty": True,
                    "remote_url": None,
                },
            ),
            mock.patch.object(
                ArtifactManager,
                "verify_record",
                side_effect=AssertionError("deep verification must be throttled"),
            ),
        ):
            result = self.service.store.reconcile_cached_metrics()

        local = self.service.store.repository(self.repository["id"])["local_sources"][0]
        self.assertFalse(result["deep_reconciled"])
        self.assertEqual(local["branch"], "release/2.0")
        self.assertEqual(local["commit_sha"], current_commit)
        self.assertTrue(local["dirty"])


if __name__ == "__main__":
    unittest.main()
