from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/local_ci"))
from agent_ci.delivery import (
    DeliveryPending,
    GiteeRedirects,
    GiteeReleaseClient,
    LOG_EXCERPT,
    publish_evidence,
    seal_artifacts,
)
from agent_ci.protocol import (
    ContractError,
    canonical,
    scope_covers,
    validate_delivery,
    validate_execution_summary,
)
from agent_ci.relay import GitRelay


class FakeAttachments:
    def __init__(self):
        self.rows, self.bytes, self.uploads = [], {}, 0
        self.offline, self.uncertain, self.corrupt = False, False, False

    def release(self, task, run_id, result_digest):
        if self.offline:
            raise OSError("simulated offline")
        return {"id": 1}

    def attachments(self, release_id):
        return self.rows.copy()

    def upload(self, release_id, name, path):
        self.uploads += 1
        row = {
            "id": self.uploads,
            "name": name,
            "browser_download_url": "https://gitee.com/test/ci/attachment",
        }
        self.rows.append(row)
        self.bytes[row["id"]] = path.read_bytes()
        if self.uncertain:
            self.uncertain = False
            raise OSError("response lost after successful server write")
        return row

    def verified(self, release_id, attachment, expected):
        data = self.bytes[attachment["id"]]
        return (
            not self.corrupt
            and len(data) == expected["size"]
            and hashlib.sha256(data).hexdigest() == expected["sha256"]
        )


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sealed = self.root / "run/sealed"
        self.sealed.mkdir(parents=True)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        (self.evidence / "junit.xml").write_text('<testsuite tests="1" failures="0"/>')
        self.log = self.root / "command.log"
        self.log.write_text("test passed\n")
        self.records = [
            {
                "execution_id": "environment-1",
                "tool_id": "environment",
                "status": "pass",
                "exit_code": 0,
                "artifact_dir": str(self.evidence),
                "log_path": str(self.log),
            }
        ]
        self.task = {"task_id": "a" * 64, "tested_sha": "b" * 40}
        self.client = FakeAttachments()

    def tearDown(self):
        self.tmp.cleanup()

    def result(self):
        artifacts = seal_artifacts(self.sealed, self.records)
        summary = {
            "schema": "triton-anchor-executions/v1",
            "task_id": self.task["task_id"],
            "run_id": "run-1",
            "executions": [
                {
                    "execution_id": "environment-1",
                    "tool_id": "environment",
                    "status": "pass",
                    "exit_code": 0,
                    "artifact_ids": [row["artifact_id"] for row in artifacts],
                    "variant": "candidate",
                    "tested_sha": self.task["tested_sha"],
                    "environment_fingerprint": "e" * 64,
                    "original_subject": True,
                }
            ],
        }
        raw = canonical(summary) + b"\n"
        (self.sealed / "execution-summary.json").write_bytes(raw)
        result = {
            "schema": "triton-anchor-local-ci/v4",
            "task": self.task,
            "run_id": "run-1",
            "status": "pass",
            "required_checks": ["environment"],
            "checks": [
                {
                    "tool_id": "environment",
                    "status": "pass",
                    "execution_id": "environment-1",
                }
            ],
            "environment": {"environment_fingerprint": "e" * 64},
            "artifacts": artifacts,
            "execution_summary_sha256": hashlib.sha256(raw).hexdigest(),
        }
        (self.sealed / "result.json").write_bytes(canonical(result) + b"\n")
        return result

    def test_uncertain_upload_is_queried_and_hash_verified_without_duplicate(self):
        result = self.result()
        self.client.uncertain = True
        index = publish_evidence(
            self.client, self.task, "run-1", self.sealed, result, "c" * 64
        )
        self.assertEqual(index["status"], "ready")
        count = self.client.uploads
        again = publish_evidence(
            self.client, self.task, "run-1", self.sealed, result, "c" * 64, index
        )
        self.assertEqual(again, index)
        self.assertEqual(self.client.uploads, count)
        self.assertEqual(validate_delivery(result, "c" * 64, index), "ready")

    def test_mismatched_uploaded_bytes_cannot_be_ready(self):
        result = self.result()
        self.client.corrupt = True
        index = publish_evidence(
            self.client, self.task, "run-1", self.sealed, result, "c" * 64
        )
        self.assertEqual(index["status"], "pending")
        self.assertTrue(all(row["status"] == "pending" for row in index["artifacts"]))

    def test_pending_publication_preserves_pass_and_only_small_documents_enter_git(
        self,
    ):
        self.result()
        original = (self.sealed / "result.json").read_bytes()
        remote = self.root / "gitee.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
        relay = GitRelay(
            str(remote),
            self.root / "relay",
            allow_local=True,
            attachment_client=self.client,
        )
        self.client.offline = True
        with self.assertRaises(DeliveryPending):
            relay.publish_result(self.task, "run-1", self.sealed)
        relay.refresh()
        prefix = f"runs/v4/{self.task['task_id']}/run-1"
        self.assertEqual(
            relay.read_json(relay.results_branch, prefix + "/result.json")["status"],
            "pass",
        )
        self.assertEqual(
            relay.read_json(relay.results_branch, prefix + "/delivery-index.json")[
                "status"
            ],
            "pending",
        )
        self.client.offline = False
        relay.publish_result(self.task, "run-1", self.sealed)
        relay.refresh()
        self.assertEqual(
            relay.read_json(relay.results_branch, prefix + "/delivery-index.json")[
                "status"
            ],
            "ready",
        )
        self.assertEqual((self.sealed / "result.json").read_bytes(), original)
        objects = relay.git(
            ["rev-list", "--objects", "refs/remotes/origin/local-ci-results"]
        ).stdout.decode()
        self.assertNotIn(".gz", objects)
        self.assertNotIn("junit.xml", objects)
        self.assertNotIn("command.log", objects)

    def test_large_logs_are_labelled_excerpts_and_reports_split_losslessly(self):
        self.log.write_bytes(b"A" * LOG_EXCERPT + b"Z" * LOG_EXCERPT)
        original = bytes(range(256)) * 10
        (self.evidence / "report.bin").write_bytes(original)
        (self.evidence / "optional.whl").write_bytes(b"x" * 1000)
        rows = seal_artifacts(self.sealed, self.records, max_attachment=128)
        grouped = {}
        for row in rows:
            if not row.get("omitted"):
                self.assertLessEqual(row["size"], 128)
                grouped.setdefault(row["source_path"], []).append(row)
        report = b"".join(
            (self.sealed / row["path"]).read_bytes() for row in grouped["report.bin"]
        )
        self.assertEqual(gzip.decompress(report), original)
        packed_log = b"".join(
            (self.sealed / row["path"]).read_bytes() for row in grouped["command.log"]
        )
        excerpt = gzip.decompress(packed_log)
        self.assertIn(b"omitted", excerpt)
        self.assertTrue(excerpt.startswith(b"A"))
        self.assertTrue(excerpt.endswith(b"Z"))
        self.assertTrue(
            next(row for row in rows if row["source_path"] == "optional.whl")["omitted"]
        )

    def test_required_execution_and_artifact_links_are_checked(self):
        result = self.result()
        summary = json.loads((self.sealed / "execution-summary.json").read_bytes())
        validate_execution_summary(result, summary)
        summary["executions"][0]["artifact_ids"] = []
        with self.assertRaisesRegex(ContractError, "necessary execution evidence"):
            validate_execution_summary(result, summary)

    def test_minimum_test_scope_requires_passed_observed_cases(self):
        required = {"paths": ["tests/test_cache.py::test_recompile"]}
        self.assertFalse(scope_covers({"paths": required["paths"]}, required))
        case = {
            "file": "tests/test_cache.py",
            "name": "test_recompile",
            "status": "skipped",
        }
        self.assertFalse(scope_covers({"observed_tests": [case]}, required))
        case["status"] = "passed"
        self.assertTrue(scope_covers({"observed_tests": [case]}, required))
        case["name"] = "test_recompile_unrelated"
        self.assertFalse(scope_covers({"observed_tests": [case]}, required))
        case.update(
            name="test_recompile[param]", **{"class": "tests.test_cache.TestOther"}
        )
        self.assertTrue(scope_covers({"observed_tests": [case]}, required))
        required["paths"] = ["tests/test_cache.py::TestCache::test_recompile"]
        self.assertFalse(scope_covers({"observed_tests": [case]}, required))
        case["class"] = "tests.test_cache.TestCache"
        self.assertTrue(scope_covers({"observed_tests": [case]}, required))
        required["paths"] = ["tests/test_cache.py::TestCache::test_recompile[other]"]
        self.assertFalse(scope_covers({"observed_tests": [case]}, required))

    def test_required_execution_cannot_alias_another_behavior_or_subject(self):
        result = self.result()
        for key, value in (
            ("tool_id", "frontend_build"),
            ("variant", "base"),
            ("tested_sha", "c" * 40),
            ("environment_fingerprint", "wrong-environment"),
            ("original_subject", False),
        ):
            summary = json.loads((self.sealed / "execution-summary.json").read_bytes())
            summary["executions"][0][key] = value
            with (
                self.subTest(field=key),
                self.assertRaisesRegex(
                    ContractError, "behavior, candidate or environment"
                ),
            ):
                validate_execution_summary(result, summary)

    def test_native_association_requires_matching_successful_raw_command(self):
        result = self.result()
        summary = json.loads((self.sealed / "execution-summary.json").read_bytes())
        association = summary["executions"][0]
        source = {
            **association,
            "execution_id": "raw-command",
            "tool_id": "native_command",
            "execution_kind": "native",
            "artifact_ids": [],
        }
        association.update(
            source_execution_id="raw-command",
            record_type="check_association",
            execution_kind="native",
        )
        with self.assertRaisesRegex(ContractError, "observed command"):
            validate_execution_summary(result, summary)
        summary["executions"].append(source)
        validate_execution_summary(result, summary)
        source["exit_code"] = 1
        with self.assertRaisesRegex(ContractError, "observed command"):
            validate_execution_summary(result, summary)
        source["exit_code"] = 0
        source["source_execution_id"] = "another-alias"
        with self.assertRaisesRegex(ContractError, "observed command"):
            validate_execution_summary(result, summary)

    def test_documented_gitee_auth_encoding_and_safe_errors(self):
        token = "private+credential/value"
        client = GiteeReleaseClient("https://gitee.com/test/ci.git", token=token)
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, limit):
                return b"{}"

        def open_request(request, timeout):
            requests.append(request)
            return Response()

        with patch.object(client.opener, "open", side_effect=open_request):
            client.request("/releases/1/attach_files?page=2")
            self.assertEqual(
                parse_qs(urlsplit(requests[-1].full_url).query)["access_token"], [token]
            )
            client.request("/releases", "POST", canonical({"tag_name": "ci/test"}))
            self.assertEqual(json.loads(requests[-1].data)["access_token"], token)
            client.upload(1, "example.bin", self.log)
            self.assertIn(b'name="access_token"', requests[-1].data)
            self.assertIn(token.encode(), requests[-1].data)
        for error in (
            URLError("failed " + token),
            HTTPError("https://gitee.com?access_token=" + token, 403, token, {}, None),
        ):
            with patch.object(client.opener, "open", side_effect=error):
                with self.assertRaises(DeliveryPending) as raised:
                    client.request("/releases/1/attach_files")
                self.assertNotIn(token, str(raised.exception))
                self.assertNotIn("access_token", str(raised.exception))
        redirect = GiteeRedirects().redirect_request(
            Request("https://gitee.com/api?access_token=" + token),
            None,
            302,
            "Found",
            {},
            "https://files.gitee.com/asset?access_token="
            + token
            + "&signature=public%20signed%2Furl",
        )
        self.assertNotIn("access_token", redirect.full_url)
        self.assertIn("signature=public%20signed%2Furl", redirect.full_url)

    def test_sealing_redacts_text_exports_and_preserves_host_and_binary_evidence(self):
        secret = "fixture-private-model-token"
        xml = f"<testsuite><system-out>{secret}</system-out></testsuite>".encode()
        (self.evidence / "junit.xml").write_bytes(xml)
        self.log.write_text("Authorization: " + secret)
        binary = b"\x00\xff" + secret.encode()
        (self.evidence / "optional.whl").write_bytes(binary)
        rows = seal_artifacts(
            self.sealed, self.records, lambda text: text.replace(secret, "[REDACTED]")
        )
        for row in rows:
            exported = gzip.decompress((self.sealed / row["path"]).read_bytes())
            if row["source_path"] in {"junit.xml", "command.log"}:
                self.assertNotIn(secret.encode(), exported)
                self.assertIn(b"[REDACTED]", exported)
                self.assertTrue(row["text_redaction_applied"])
            elif row["source_path"] == "optional.whl":
                self.assertEqual(exported, binary)
                self.assertFalse(row["text_redaction_applied"])
        self.assertEqual((self.evidence / "junit.xml").read_bytes(), xml)
        self.assertIn(secret, self.log.read_text())

    def test_native_associations_export_the_raw_command_log_once(self):
        original = {**self.records[0], "execution_id": "native-1", "artifact_dir": ""}
        first = {
            **self.records[0],
            "execution_id": "credit-1",
            "source_execution_id": "native-1",
        }
        second = {**first, "execution_id": "credit-2"}
        rows = seal_artifacts(self.sealed, [original, first, second])
        logs = [row for row in rows if row["source_path"] == "command.log"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["execution_id"], "native-1")
        self.assertEqual(
            {row["execution_id"] for row in rows if row["source_path"] == "junit.xml"},
            {"credit-1", "credit-2"},
        )

    def test_optional_only_retry_preserves_required_delivery_and_expiry(self):
        (self.evidence / "optional.whl").write_bytes(b"optional wheel")
        result = self.result()
        saved = publish_evidence(
            self.client, self.task, "run-1", self.sealed, result, "c" * 64
        )
        required = [dict(row) for row in saved["artifacts"] if row["required"]]
        optional = next(row for row in saved["artifacts"] if not row["required"])
        optional["status"] = "pending"
        self.client.rows = []
        count = self.client.uploads
        for artifact in result["artifacts"]:
            if artifact["required"]:
                (self.sealed / artifact["path"]).unlink()
        index = publish_evidence(
            self.client,
            self.task,
            "run-1",
            self.sealed,
            result,
            "c" * 64,
            saved,
            optional_only=True,
        )
        self.assertEqual(self.client.uploads, count + 1)
        self.assertEqual(
            [row for row in index["artifacts"] if row["required"]], required
        )
        self.assertEqual(index["status"], "ready")
        index["status"] = "expired"
        for row in index["artifacts"]:
            row["status"] = "expired"
        self.client.offline = True
        self.assertEqual(
            publish_evidence(
                self.client,
                self.task,
                "run-1",
                self.sealed,
                result,
                "c" * 64,
                index,
                optional_only=True,
            ),
            index,
        )


if __name__ == "__main__":
    unittest.main()
