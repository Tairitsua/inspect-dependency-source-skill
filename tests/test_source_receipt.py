"""Behavior and privacy tests for dependency Source Receipts."""

from __future__ import annotations

import io
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import inspect_dependency_source as cli
from _catalog_receipt import ResolveRequest, build_source_receipt, render_source_receipt
from _catalog_service import CatalogService


MARKDOWN_AVAILABLE = importlib.util.find_spec("markdown") is not None


class _CodeContentProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.code_depth = 0
        self.tags_inside_code: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if self.code_depth:
            self.tags_inside_code.append(tag)
        if tag == "code":
            self.code_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "code":
            self.code_depth -= 1


COMMIT = "0a2e291c0d9c0c7675d445703e51750363a549ef"
VERIFIED_AT = "2026-07-17T07:03:52+00:00"


def _contract(
    *,
    resolution_kind: str = "exact_commit",
    verification_state: str = "verified",
    expected_commit: str | None = COMMIT,
    observed_commit: str | None = COMMIT,
    package_kind: str | None = "exact_commit",
) -> dict[str, object]:
    package = None
    if package_kind is not None:
        package = {
            "ecosystem": "nuget",
            "id": "Newtonsoft.Json",
            "version": "13.0.3",
            "requested_ref": COMMIT,
            "resolved_ref": COMMIT,
            "resolution_kind": package_kind,
            "expected_commit": expected_commit,
        }
    return {
        "status": "ok",
        "source_path": "/private/catalog/repos/source",
        "verification_state": verification_state,
        "resolution_kind": resolution_kind,
        "repository": {
            "id": "private-repository-id",
            "canonical_name": "JamesNK/Newtonsoft.Json",
            "display_name": "Newtonsoft.Json",
            "provider": "github",
            "remote_url": "https://user:secret@example.invalid/private.git",
            "aliases": ["private-alias"],
        },
        "artifact": {
            "id": "private-artifact-id",
            "kind": "github_archive",
            "ref": COMMIT,
            "detected_version": None,
            "expected_commit": expected_commit,
            "actual_commit": observed_commit,
            "verification_state": verification_state,
            "resolution_kind": resolution_kind,
            "verified_at": VERIFIED_AT,
        },
        "package": package,
    }


def _invoke_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main(arguments)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class SourceReceiptTests(unittest.TestCase):
    def test_exact_nuget_commit_renders_proven_receipt_without_private_fields(self) -> None:
        receipt = build_source_receipt(
            _contract(), ResolveRequest("Newtonsoft.Json", "13.0.3")
        )
        rendered = render_source_receipt(receipt)

        self.assertEqual(receipt.verdict, "PROVEN")
        self.assertEqual(receipt.resolution_kind, "exact_commit")
        self.assertIn("NuGet · Newtonsoft.Json 13.0.3", rendered)
        self.assertIn("JamesNK/Newtonsoft.Json (github)", rendered)
        self.assertEqual(rendered.count(COMMIT), 3)
        self.assertIn("absolute path withheld", rendered)
        self.assertNotIn("/private/catalog", rendered)
        self.assertNotIn("user:secret", rendered)
        self.assertNotIn("private-alias", rendered)
        self.assertNotIn("private-repository-id", rendered)
        self.assertNotIn("private-artifact-id", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertEqual(rendered, render_source_receipt(receipt))

    def test_checked_in_real_example_matches_the_receipt_renderer(self) -> None:
        receipt = build_source_receipt(
            _contract(), ResolveRequest("Newtonsoft.Json", "13.0.3")
        )
        expected = (
            REPOSITORY_ROOT
            / "examples"
            / "newtonsoft-json-13.0.3"
            / "source-receipt.md"
        ).read_text(encoding="utf-8")

        self.assertEqual(render_source_receipt(receipt) + "\n", expected)

    def test_exact_tag_is_proven_only_when_commits_agree_and_source_is_verified(self) -> None:
        contract = _contract(resolution_kind="exact_tag", package_kind=None)
        contract["artifact"]["ref"] = "v13.0.3"  # type: ignore[index]
        receipt = build_source_receipt(
            contract,
            ResolveRequest("newtonsoft", "v13.0.3"),
        )

        self.assertEqual(receipt.verdict, "PROVEN")
        self.assertEqual(receipt.verdict_label, "exact tag")
        self.assertIn("tags can move", render_source_receipt(receipt))

    def test_nested_package_kind_controls_package_receipt_verdict(self) -> None:
        receipt = build_source_receipt(
            _contract(resolution_kind="exact_commit", package_kind="heuristic_tag"),
            ResolveRequest("newtonsoft.json", "13.0.3"),
        )

        self.assertEqual(receipt.verdict, "CANDIDATE")
        self.assertEqual(receipt.resolution_kind, "heuristic_tag")
        self.assertNotIn("PROVEN", render_source_receipt(receipt))

    def test_package_receipt_requires_an_explicit_matching_version(self) -> None:
        omitted = build_source_receipt(
            _contract(), ResolveRequest("Newtonsoft.Json")
        )
        mismatched = build_source_receipt(
            _contract(), ResolveRequest("Newtonsoft.Json", "12.0.1")
        )

        self.assertEqual(omitted.verdict, "BLOCKED")
        self.assertEqual(omitted.verdict_label, "package version not specified")
        self.assertIn("version not specified", omitted.request)
        self.assertNotIn("13.0.3", omitted.request)
        self.assertEqual(mismatched.verdict, "BLOCKED")
        self.assertEqual(mismatched.verdict_label, "package version mismatch")
        self.assertIn("12.0.1", mismatched.request)

    def test_incidental_package_binding_does_not_relabel_repository_request(self) -> None:
        contract = _contract(resolution_kind="exact_tag", package_kind="heuristic_tag")
        contract["artifact"]["ref"] = "v13.0.3"  # type: ignore[index]
        receipt = build_source_receipt(
            contract,
            ResolveRequest("repository-alias", "v13.0.3"),
        )

        self.assertEqual(receipt.verdict, "PROVEN")
        self.assertEqual(receipt.resolution_kind, "exact_tag")
        self.assertTrue(receipt.request.startswith("Repository ·"))

    def test_opaque_local_and_generic_git_identity_suffixes_are_withheld(self) -> None:
        local = _contract(
            resolution_kind="unresolved",
            expected_commit=None,
            observed_commit=None,
            package_kind=None,
        )
        local["repository"].update(  # type: ignore[union-attr]
            {
                "canonical_name": "local:private-source#c0dd85ef62",
                "display_name": "private-source",
                "provider": "local",
            }
        )
        local["artifact"].update(  # type: ignore[union-attr]
            {
                "kind": "local",
                "ref": "1.0.0@local-c0dd85ef62",
            }
        )
        local_rendered = render_source_receipt(
            build_source_receipt(local, ResolveRequest("private-source"))
        )

        self.assertIn("Repository · private-source", local_rendered)
        self.assertIn("<code>1.0.0</code>", local_rendered)
        self.assertNotIn("c0dd85ef62", local_rendered)
        self.assertNotIn("@local-", local_rendered)

        generic = _contract(resolution_kind="exact_tag", package_kind=None)
        generic["repository"].update(  # type: ignore[union-attr]
            {
                "canonical_name": "git:source.example/team/private#0123abcd",
                "display_name": "private",
                "provider": "git",
            }
        )
        generic["artifact"]["ref"] = "v13.0.3"  # type: ignore[index]
        generic_rendered = render_source_receipt(
            build_source_receipt(generic, ResolveRequest("private", "v13.0.3"))
        )

        self.assertIn("git:source.example/team/private", generic_rendered)
        self.assertNotIn("0123abcd", generic_rendered)

    def test_file_git_remotes_never_expose_local_paths(self) -> None:
        for label, remote, canonical, provider in (
            (
                "posix",
                "file:///home/alice/private/repo.git",  # release-privacy-fixture
                "git:file/home/alice/private/repo#0123abcd",  # release-privacy-fixture
                "git",
            ),
            (
                "windows hybrid",
                "file:///C:/Users/Alice/secret/repo.git",  # release-privacy-fixture
                "git:file/C:/Users/Alice/secret/repo#0123abcd",  # release-privacy-fixture
                "local",
            ),
        ):
            with self.subTest(label=label):
                contract = _contract(package_kind=None)
                contract["repository"].update(  # type: ignore[union-attr]
                    {
                        "canonical_name": canonical,
                        "display_name": "repo",
                        "provider": provider,
                        "remote_url": remote,
                    }
                )
                rendered = render_source_receipt(
                    build_source_receipt(
                        contract, ResolveRequest("repo", COMMIT)
                    )
                )

                self.assertIn("local Git source", rendered)
                self.assertNotIn(remote, rendered)
                self.assertNotIn("home/alice", rendered)
                self.assertNotIn("Users/Alice", rendered)
                self.assertNotIn(canonical, rendered)

    def test_uncertain_or_inconsistent_evidence_fails_closed(self) -> None:
        cases = (
            (
                "unresolved",
                _contract(
                    resolution_kind="unresolved",
                    expected_commit=None,
                    observed_commit=None,
                    package_kind=None,
                ),
                "BLOCKED",
            ),
            (
                "unverified exact",
                _contract(verification_state="unverified"),
                "BLOCKED",
            ),
            (
                "commit mismatch",
                _contract(observed_commit="f" * 40),
                "BLOCKED",
            ),
            (
                "matching non-commit labels",
                _contract(expected_commit="main", observed_commit="main"),
                "BLOCKED",
            ),
            (
                "unknown kind",
                _contract(resolution_kind="future_kind", package_kind="future_kind"),
                "BLOCKED",
            ),
            (
                "heuristic",
                _contract(package_kind="heuristic_tag"),
                "CANDIDATE",
            ),
        )
        for label, contract, expected in cases:
            with self.subTest(label=label):
                receipt = build_source_receipt(
                    contract,
                    ResolveRequest("Newtonsoft.Json", "13.0.3"),
                )
                self.assertEqual(receipt.verdict, expected)
                if expected != "PROVEN":
                    self.assertNotIn("> **PROVEN", render_source_receipt(receipt))

    def test_markdown_values_are_single_line_and_escaped(self) -> None:
        contract = _contract(resolution_kind="exact_tag", package_kind=None)
        contract["repository"]["canonical_name"] = (  # type: ignore[index]
            "owner/<script>|repository\n"
            "x **bold** [click](javascript:alert(1)) `tick` *em*\\tail"
            "\x1b\u202eforged"
        )
        receipt = build_source_receipt(contract, ResolveRequest("alias", "v1"))
        rendered = render_source_receipt(receipt)

        self.assertIn("owner/&lt;script&gt;&#124;repository", rendered)
        self.assertIn("&#42;&#42;bold&#42;&#42;", rendered)
        self.assertIn("&#91;click&#93;(javascript:alert(1))", rendered)
        self.assertIn("&#96;tick&#96;", rendered)
        self.assertIn("&#42;em&#42;&#92;tail", rendered)
        self.assertIn("&#92;u001B&#92;u202Eforged", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("repository\nforged", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)

    @unittest.skipUnless(
        MARKDOWN_AVAILABLE,
        "Python-Markdown is optional development tooling.",
    )
    def test_rendered_html_cannot_activate_untrusted_markdown(self) -> None:
        import markdown

        contract = _contract(resolution_kind="exact_tag", package_kind=None)
        contract["repository"]["canonical_name"] = (  # type: ignore[index]
            "owner/x **bold** [click](javascript:alert(1)) `tick` *em* | row"
        )
        receipt = build_source_receipt(contract, ResolveRequest("alias", "v1"))
        rendered = render_source_receipt(receipt)
        rendered_html = markdown.markdown(rendered, extensions=["tables"])
        probe = _CodeContentProbe()
        probe.feed(rendered_html)

        self.assertEqual(probe.tags_inside_code, [])
        self.assertEqual(probe.code_depth, 0)
        self.assertEqual(rendered_html.count("<code>"), rendered.count("<code>"))
        self.assertEqual(rendered_html.count("<tr>"), 10)

    def test_receipt_cli_is_opt_in_and_json_contract_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "pyproject.toml").write_text(
                '[project]\nname = "receipt-example"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            catalog = root / "catalog"
            CatalogService(catalog).add_local(source, aliases=("receipt-example",))

            code, stdout, stderr = _invoke_cli(
                [
                    "--catalog-root",
                    str(catalog),
                    "resolve",
                    "receipt-example",
                    "--receipt",
                ]
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertIn("Dependency Source Receipt", stdout)
            self.assertIn("> **BLOCKED", stdout)
            self.assertIn("Repository · source", stdout)
            self.assertNotIn(str(source.resolve()), stdout)
            self.assertNotIn("@local-", stdout)
            self.assertNotRegex(stdout, r"#[0-9a-f]{8,64}")

            code, stdout, stderr = _invoke_cli(
                ["--catalog-root", str(catalog), "resolve", "receipt-example"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(stdout.splitlines()[0], str(source.resolve()))

            code, stdout, stderr = _invoke_cli(
                [
                    "--catalog-root",
                    str(catalog),
                    "resolve",
                    "receipt-example",
                    "--json",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["source_path"], str(source.resolve()))
            self.assertNotIn("receipt", payload)

    def test_cli_blocks_package_receipt_when_version_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "README.md").write_text("source\n", encoding="utf-8")
            subprocess.run(
                ["git", "init", "--initial-branch", "main"],
                cwd=source,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Receipt Test"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "receipt@example.invalid"],
                cwd=source,
                check=True,
            )
            subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
            subprocess.run(
                ["git", "commit", "--message", "seed"],
                cwd=source,
                check=True,
                capture_output=True,
            )
            catalog = root / "catalog"
            service = CatalogService(catalog)
            repository = service.add_local(source, aliases=("Receipt.Package",))
            artifact = service.store.repository(repository["id"])[
                "preferred_artifact"
            ]
            service.store.bind_package(
                {
                    "id": "binding-receipt-package",
                    "ecosystem": "nuget",
                    "package_id": "Receipt.Package",
                    "version": "1.0.0",
                    "repository_id": repository["id"],
                    "artifact_id": artifact["id"],
                    "requested_ref": artifact["actual_commit"],
                    "resolved_ref": artifact["actual_commit"],
                    "resolution_kind": "exact_commit",
                    "expected_commit": artifact["actual_commit"],
                }
            )

            code, stdout, stderr = _invoke_cli(
                [
                    "--catalog-root",
                    str(catalog),
                    "resolve",
                    "Receipt.Package",
                    "--receipt",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertIn("**BLOCKED — package version not specified.**", stdout)
            self.assertIn("Receipt.Package (version not specified)", stdout)
            self.assertNotIn("Receipt.Package 1.0.0", stdout)

    def test_receipt_errors_never_emit_partial_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, stdout, stderr = _invoke_cli(
                [
                    "--catalog-root",
                    str(Path(temporary) / "catalog"),
                    "resolve",
                    "missing",
                    "--receipt",
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("error [not_found]", stderr)
        self.assertNotIn("Receipt", stderr)


if __name__ == "__main__":
    unittest.main()
