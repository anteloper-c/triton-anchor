from __future__ import annotations

import io
import hashlib
import json
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path

from scripts.compliance.cli import build_parser, main
from scripts.compliance.core import (
    evaluate_artifact,
    evaluate_candidate,
    generate_sbom,
    notice_entries,
    reconcile_discoveries,
    render_notices,
    stable_json,
)
from scripts.compliance.release import artifact_sbom_link
from scripts.compliance.source_snapshot import (
    SourceSnapshotValidationError,
    inspect_source_snapshot,
    source_snapshot_discoveries,
)
from scripts.compliance.tests.helpers import component


def make_source_archives(
    root: Path,
    *,
    zip_files: dict[str, bytes],
    tar_files: dict[str, bytes] | None = None,
) -> tuple[Path, Path]:
    source_zip = root / "source.zip"
    source_tar = root / "source.tar.gz"
    with zipfile.ZipFile(source_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(zip_files.items()):
            archive.writestr(f"zip-root/{name}", data)
    with tarfile.open(source_tar, "w:gz") as archive:
        for name, data in sorted((tar_files or zip_files).items()):
            info = tarfile.TarInfo(f"tar-root/{name}")
            info.mode = 0o644
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return source_zip, source_tar


class SourceSnapshotTests(unittest.TestCase):
    def test_cli_source_mode_is_distinct_from_wheel_input(self) -> None:
        parser = build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "artifact-evaluation",
                    "--wheel",
                    "artifact.whl",
                    "--source-zip",
                    "source.zip",
                ]
            )

    def test_cli_source_evaluation_writes_one_sbom_and_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_zip, source_tar = make_source_archives(
                root, zip_files={"README.md": b"snapshot\n"}
            )
            product = component("triton-anchor", licenses=["Apache-2.0"])
            product["third_party"] = False
            product["usages"] = [
                {
                    "category": "distributed",
                    "status": "confirmed",
                    "target": "source-snapshot",
                    "path_patterns": ["README.md"],
                    "evidence_ids": [],
                }
            ]
            registry = root / "registry.json"
            policy = root / "policy.json"
            risks = root / "risks.json"
            output = root / "output"
            registry.write_text(
                json.dumps({"schema_version": 1, "components": [product]}),
                encoding="utf-8",
            )
            policy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "approved",
                        "expressions": {"Apache-2.0": {"decision": "allow"}},
                        "vulnerability_threshold": "high",
                    }
                ),
                encoding="utf-8",
            )
            risks.write_text(
                json.dumps({"schema_version": 1, "records": []}),
                encoding="utf-8",
            )
            commit = "1" * 40
            exit_code = main(
                [
                    "artifact-evaluation",
                    "--source-zip",
                    str(source_zip),
                    "--source-tar",
                    str(source_tar),
                    "--source-repository",
                    "RACE-org/triton-anchor",
                    "--source-reference-kind",
                    "commit",
                    "--source-reference",
                    commit,
                    "--source-commit",
                    commit,
                    "--source-version",
                    commit,
                    "--registry",
                    str(registry),
                    "--policy",
                    str(policy),
                    "--risk-acceptances",
                    str(risks),
                    "--output-dir",
                    str(output),
                ]
            )
            report = json.loads(
                (output / "compliance-report.json").read_text(encoding="utf-8")
            )
            link = json.loads(
                (output / "artifact-sbom-link.json").read_text(encoding="utf-8")
            )
            sbom_count = len(list(output.glob("*-source-snapshot.cdx.json")))

        self.assertEqual(1, exit_code)
        self.assertEqual("github-source-snapshot", report["artifact"]["artifact_kind"])
        self.assertEqual("not-applicable", report["promotion_status"])
        self.assertEqual(2, len(link["artifact"]["representations"]))
        self.assertEqual(1, sbom_count)

    def test_two_archive_representations_share_one_tree_identity_and_sbom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_zip, source_tar = make_source_archives(
                Path(temporary), zip_files={"LICENSE": b"license", "src/a.py": b"x=1\n"}
            )
            commit = "1" * 40
            artifact = inspect_source_snapshot(
                source_zip,
                source_tar,
                repository="RACE-org/triton-anchor",
                reference_kind="tag",
                reference="v1.0",
                commit_sha=commit,
                version="1.0",
            )
            zip_sha256 = hashlib.sha256(source_zip.read_bytes()).hexdigest()
            tar_sha256 = hashlib.sha256(source_tar.read_bytes()).hexdigest()

        sbom = generate_sbom(artifact, [], target="source-snapshot")
        link = artifact_sbom_link(artifact, sbom)
        other_identity = {
            **artifact,
            "filename": "triton-anchor-v1.0-other-source-snapshot",
            "source_reference": "v1.0-other",
        }
        other_sbom = generate_sbom(other_identity, [], target="source-snapshot")
        properties = {
            item["name"]: item["value"]
            for item in sbom["metadata"]["component"]["properties"]
        }

        self.assertEqual("github-source-snapshot", artifact["artifact_kind"])
        representations = {
            item["format"]: item for item in link["artifact"]["representations"]
        }
        self.assertEqual({"zip", "tar.gz"}, set(representations))
        self.assertEqual(zip_sha256, representations["zip"]["sha256"])
        self.assertEqual(tar_sha256, representations["tar.gz"]["sha256"])
        self.assertNotEqual(zip_sha256, tar_sha256)
        self.assertEqual(artifact["sha256"], link["artifact"]["sha256"])
        self.assertEqual(
            hashlib.sha256(stable_json(sbom).encode("utf-8")).hexdigest(),
            link["sbom"]["sha256"],
        )
        self.assertNotEqual(sbom["serialNumber"], other_sbom["serialNumber"])
        self.assertEqual(
            "normalized-archive-content-tree",
            properties["triton-anchor:hash-scope"],
        )
        self.assertNotIn("triton-anchor:python-tag", properties)
        self.assertNotIn("triton-anchor:abi-tag", properties)
        self.assertNotIn("triton-anchor:platform-tag", properties)

    def test_archive_content_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_zip, source_tar = make_source_archives(
                Path(temporary),
                zip_files={"src/a.py": b"zip\n"},
                tar_files={"src/a.py": b"tar\n"},
            )
            with self.assertRaisesRegex(
                SourceSnapshotValidationError, "do not contain the same"
            ):
                inspect_source_snapshot(
                    source_zip,
                    source_tar,
                    repository="RACE-org/triton-anchor",
                    reference_kind="tag",
                    reference="v1.0",
                    commit_sha="1" * 40,
                    version="1.0",
                )

    def test_tar_representation_must_be_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_zip, _ = make_source_archives(
                root, zip_files={"README.md": b"snapshot\n"}
            )
            source_tar = root / "source.tar"
            with tarfile.open(source_tar, "w") as archive:
                data = b"snapshot\n"
                info = tarfile.TarInfo("tar-root/README.md")
                info.mode = 0o644
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

            with self.assertRaisesRegex(
                SourceSnapshotValidationError, "cannot read source tar archive"
            ):
                inspect_source_snapshot(
                    source_zip,
                    source_tar,
                    repository="RACE-org/triton-anchor",
                    reference_kind="tag",
                    reference="v1.0",
                    commit_sha="1" * 40,
                    version="1.0",
                )

    def test_local_git_tag_establishes_formal_source_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "core.autocrlf", "false"],
                check=True,
            )
            (root / "README.md").write_text("snapshot\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "snapshot"], check=True)
            gitlink_commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (root / ".gitmodules").write_text(
                '[submodule "FlagGems"]\n\tpath = FlagGems\n\turl = https://example.test/FlagGems\n',
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(root), "add", ".gitmodules"], check=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{gitlink_commit},FlagGems",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "commit", "-q", "-m", "add gitlink"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "tag", "v1.0"], check=True)
            commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            source_zip = root / "source.zip"
            source_tar = root / "source.tar.gz"
            subprocess.run(
                ["git", "-C", str(root), "archive", "--format=zip", "--prefix=source/", "-o", str(source_zip), commit],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "archive", "--format=tar.gz", "--prefix=source/", "-o", str(source_tar), commit],
                check=True,
            )

            artifact = inspect_source_snapshot(
                source_zip,
                source_tar,
                repository="RACE-org/triton-anchor",
                reference_kind="tag",
                reference="v1.0",
                commit_sha=commit,
                version="1.0",
                repository_root=root,
            )
            with self.assertRaisesRegex(SourceSnapshotValidationError, "resolves to"):
                inspect_source_snapshot(
                    source_zip,
                    source_tar,
                    repository="RACE-org/triton-anchor",
                    reference_kind="tag",
                    reference="v1.0",
                    commit_sha="0" * 40,
                    version="1.0",
                    repository_root=root,
                )

            discoveries = source_snapshot_discoveries(artifact)
            gitlink_discovery = next(
                item
                for item in discoveries["items"]
                if item.get("declaration_source") == "gitlink"
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "update-index",
                    "--cacheinfo",
                    f"160000,{commit},FlagGems",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "commit", "-q", "-m", "move gitlink"],
                check=True,
            )
            moved_commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            moved_zip = root / "moved.zip"
            moved_tar = root / "moved.tar.gz"
            subprocess.run(
                ["git", "-C", str(root), "archive", "--format=zip", "--prefix=source/", "-o", str(moved_zip), moved_commit],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "archive", "--format=tar.gz", "--prefix=source/", "-o", str(moved_tar), moved_commit],
                check=True,
            )
            moved_artifact = inspect_source_snapshot(
                moved_zip,
                moved_tar,
                repository="RACE-org/triton-anchor",
                reference_kind="commit",
                reference=moved_commit,
                commit_sha=moved_commit,
                version=moved_commit,
                repository_root=root,
            )

        self.assertEqual("verified-tag-commit", artifact["source_identity_binding"])
        self.assertEqual("normalized-git-tree", artifact["hash_scope"])
        self.assertEqual(gitlink_commit, artifact["gitlinks"][0]["commit"])
        self.assertEqual(gitlink_commit, gitlink_discovery["version"])
        self.assertNotEqual(artifact["sha256"], moved_artifact["sha256"])

        registered = component("flaggems", "0" * 40, distribution="test-only")
        registered["name"] = "FlagGems"
        registered["usages"][0]["target"] = "source-snapshot"
        mismatch = reconcile_discoveries(
            {"schema_version": 1, "components": [registered]},
            [
                {
                    "source": "source-snapshot",
                    "status": "success",
                    "issues": [],
                    "items": [gitlink_discovery],
                }
            ],
            target="source-snapshot",
        )
        self.assertIn(
            "gitlink-version-mismatch",
            {issue.get("code") for issue in mismatch["execution_issues"]},
        )

    def test_formal_source_candidate_requires_verified_tag_binding(self) -> None:
        root = component("triton-anchor", licenses=["Apache-2.0"])
        root["third_party"] = False
        root["usages"] = [
            {
                "category": "distributed",
                "status": "confirmed",
                "target": "source-snapshot",
                "path_patterns": ["README.md"],
                "evidence_ids": [],
            }
        ]
        registry = {"schema_version": 1, "components": [root]}
        canonical_notice = render_notices(notice_entries(registry, None)).encode(
            "utf-8"
        )
        artifact = {
            "artifact_kind": "github-source-snapshot",
            "filename": "triton-anchor-v1.0-source-snapshot",
            "name": "triton-anchor",
            "version": "1.0",
            "sha256": "1" * 64,
            "hash_scope": "normalized-git-tree",
            "repository": "RACE-org/triton-anchor",
            "reference_kind": "tag",
            "source_reference": "v1.0",
            "source_commit": "2" * 40,
            "source_identity_binding": "verified-tag-commit",
            "representations": [
                {
                    "filename": "source.zip",
                    "format": "zip",
                    "sha256": "3" * 64,
                    "size": 1,
                },
                {
                    "filename": "source.tar.gz",
                    "format": "tar.gz",
                    "sha256": "4" * 64,
                    "size": 1,
                },
            ],
            "gitlinks": [],
            "files": [
                {
                    "path": "THIRD_PARTY_NOTICES.md",
                    "size": len(canonical_notice),
                    "sha256": hashlib.sha256(canonical_notice).hexdigest(),
                }
            ],
        }
        inputs = {
            "artifact": artifact,
            "registry": registry,
            "policy": {
                "schema_version": 1,
                "status": "approved",
                "expressions": {"Apache-2.0": {"decision": "allow"}},
                "vulnerability_threshold": "high",
            },
            "risk_acceptances": {"schema_version": 1, "records": []},
            "discovery_reports": [
                {
                    "source": "source-snapshot",
                    "status": "success",
                    "issues": [],
                    "items": [
                        {"kind": "source-snapshot-file", "path": "README.md"}
                    ],
                },
                {"source": "scancode-source", "status": "success", "issues": [], "items": []},
                {"source": "syft", "status": "success", "issues": [], "items": []},
                {"source": "dependency-inventory", "status": "success", "issues": [], "items": []},
                {"source": "osv", "status": "success", "issues": [], "items": []},
            ],
            "vulnerabilities": [],
            "vulnerability_coverage": [],
            "today": date(2026, 8, 24),
            "target": "source-snapshot",
        }

        technical, technical_sbom, technical_link, technical_notices = (
            evaluate_artifact(**inputs)
        )
        formal, formal_sbom, formal_link, formal_notices = evaluate_candidate(
            **inputs
        )
        self.assertEqual("not-applicable", technical["promotion_status"])
        self.assertEqual("pass", formal["promotion_status"], formal)
        self.assertEqual(technical_sbom, formal_sbom)
        self.assertEqual(technical_link, formal_link)
        self.assertEqual(technical_notices, formal_notices)
        self.assertEqual(
            technical["compliance_blockers"], formal["compliance_blockers"]
        )

        inputs["discovery_reports"][0]["issues"] = [
            "source inventory identity mismatch"
        ]
        inventory_issue, inventory_issue_sbom, _, _ = evaluate_candidate(
            **inputs
        )
        self.assertEqual("fail", inventory_issue["execution_status"])
        self.assertEqual("complete", inventory_issue["evidence_status"])
        self.assertEqual("incomplete", inventory_issue["sbom_inventory_status"])
        self.assertEqual(
            "incomplete", inventory_issue_sbom["compositions"][0]["aggregate"]
        )
        inputs["discovery_reports"][0]["issues"] = []

        artifact["files"] = []
        missing_notice, _, _, _ = evaluate_candidate(**inputs)
        self.assertEqual("blocked", missing_notice["promotion_status"])
        self.assertIn(
            "source-notice-missing",
            {item.get("code") for item in missing_notice["notice_findings"]},
        )
        artifact["files"] = [
            {
                "path": "THIRD_PARTY_NOTICES.md",
                "size": len(canonical_notice),
                "sha256": "0" * 64,
            }
        ]
        drifted_notice, _, _, _ = evaluate_candidate(**inputs)
        self.assertEqual("blocked", drifted_notice["promotion_status"])
        self.assertIn(
            "source-notice-drift",
            {item.get("code") for item in drifted_notice["notice_findings"]},
        )
        artifact["files"] = [
            {
                "path": "THIRD_PARTY_NOTICES.md",
                "size": len(canonical_notice),
                "sha256": hashlib.sha256(canonical_notice).hexdigest(),
            }
        ]

        artifact["representations"] = []
        missing_archives, _, _, _ = evaluate_candidate(**inputs)
        self.assertEqual("incomplete", missing_archives["evidence_status"])
        self.assertEqual("blocked", missing_archives["promotion_status"])
        artifact["representations"] = [
            {
                "filename": "source.zip",
                "format": "zip",
                "sha256": "3" * 64,
                "size": 1,
            },
            {
                "filename": "source.tar.gz",
                "format": "tar.gz",
                "sha256": "4" * 64,
                "size": 1,
            },
        ]
        missing_inventory_inputs = {
            **inputs,
            "discovery_reports": [
                report
                for report in inputs["discovery_reports"]
                if report["source"] != "dependency-inventory"
            ],
        }
        missing_inventory, _, _, _ = evaluate_candidate(**missing_inventory_inputs)
        self.assertEqual("incomplete", missing_inventory["evidence_status"])
        self.assertEqual("blocked", missing_inventory["promotion_status"])

        artifact["reference_kind"] = "commit"
        artifact["source_identity_binding"] = "verified-commit"
        blocked, _, _, _ = evaluate_candidate(**inputs)
        self.assertEqual("blocked", blocked["promotion_status"])
        self.assertEqual(
            {"candidate-source-tag-binding-not-established"},
            {item["code"] for item in blocked["promotion_findings"]},
        )


if __name__ == "__main__":
    unittest.main()
