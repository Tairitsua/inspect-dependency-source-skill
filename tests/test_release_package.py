"""Regression coverage for the complete public release surface."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SHOWCASE = ROOT / "showcase"
for module_root in (SCRIPTS, SHOWCASE):
    if str(module_root) not in sys.path:
        sys.path.insert(0, str(module_root))

from record_dashboard import _assert_share_safe  # noqa: E402
from validate_release import (  # noqa: E402
    _privacy_failures,
    _release_text_files,
    validate_repository,
)


class ReleasePackageTests(unittest.TestCase):
    def test_public_release_surface_is_complete(self) -> None:
        result = validate_repository(ROOT)
        self.assertEqual(result.failures, [], "\n".join(result.failures))

    def test_slash_style_windows_home_is_rejected_in_shipped_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "assets" / "dashboard" / "app.js"
            asset.parent.mkdir(parents=True)
            asset.write_text(
                'const leaked = "file:///C:/Users/example/private/source";\n',  # release-privacy-fixture
                encoding="utf-8",
            )

            private_hits, token_hits = _privacy_failures(
                root, _release_text_files(root)
            )

        self.assertEqual(token_hits, [])
        self.assertTrue(any("slash-style Windows user home" in hit for hit in private_hits))

    def test_showcase_rejects_slash_style_windows_home(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "private/sensitive markers"):
            _assert_share_safe("file:///C:/Users/example/private/source")  # release-privacy-fixture

    def test_terminal_home_paths_are_rejected_even_in_shipped_tests(self) -> None:
        fixtures = (
            "/" + "mnt/c/Users/Alice",  # release-privacy-fixture
            "C:" + "\\Users\\Alice",  # release-privacy-fixture
            "C:" + "/Users/Alice",  # release-privacy-fixture
            "/" + "Users/alice",  # release-privacy-fixture
            "/" + "home/alice",  # release-privacy-fixture
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shipped_test = root / "tests" / "test_private_path.py"
            shipped_test.parent.mkdir(parents=True)
            shipped_test.write_text("\n".join(fixtures), encoding="utf-8")

            private_hits, _token_hits = _privacy_failures(
                root, _release_text_files(root)
            )

        self.assertEqual(len(private_hits), len(fixtures))

    def test_exact_current_home_without_trailing_separator_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "config.txt"
            asset.write_text(str(Path.home().resolve()), encoding="utf-8")

            private_hits, _token_hits = _privacy_failures(
                root, _release_text_files(root)
            )

        self.assertTrue(any("current user home" in hit for hit in private_hits))

    def test_fixture_marker_cannot_bypass_scan_outside_allowlisted_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "assets" / "config.txt"
            asset.parent.mkdir(parents=True)
            asset.write_text(
                "/" + "home/alice # release-privacy-fixture",
                encoding="utf-8",
            )

            private_hits, _token_hits = _privacy_failures(
                root, _release_text_files(root)
            )

        self.assertTrue(any("Linux user home" in hit for hit in private_hits))


if __name__ == "__main__":
    unittest.main()
