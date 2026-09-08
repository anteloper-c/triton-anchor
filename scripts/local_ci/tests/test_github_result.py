from __future__ import annotations

import copy
import base64
import hashlib
import importlib.util
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlparse


GITHUB = Path(__file__).resolve().parents[1] / "github"


def module(name):
    spec = importlib.util.spec_from_file_location(name, GITHUB / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


receiver = module("receive_result")
builder = module("build_task_metadata")


def admitted_task(external=False):
    values = dict(REPOSITORY="anteloper-c/triton-anchor", PR_NUMBER="19", TASK_REF="ci/pr-19/topic",
                  TESTED_SHA="c" * 40, TESTED_REF="refs/pull/19/merge", WORKER_REVISION_SHA="d" * 40,
                  BASE_SHA="a" * 40, BASE_REF="main", HEAD_SHA="b" * 40, HEAD_REF="topic",
                  HEAD_REPO="contributor/triton-anchor" if external else "anteloper-c/triton-anchor",
                  TARGET_BRANCH="main", TRITON_VERSION="3.0", CHANGED_PATHS_JSON='["README.md"]', CAPTURED_AT="2026-09-08T12:00:00Z", RUN_ID="101", RUN_ATTEMPT="1",
                  PREFLIGHT_PASSED="true", EXTERNAL_APPROVAL="true" if external else "false")
    pull = dict(number=19, title="Correct masked load semantics", body="Preserve inactive lanes; validated using existing regression tests.",
                state="open", draft=False, head=dict(sha=values["HEAD_SHA"], ref="topic", repo=dict(full_name=values["HEAD_REPO"])),
                base=dict(sha=values["BASE_SHA"], ref="main"), labels=[dict(name="bug")])
    return values, pull


def result_fixture(external=False):
    values, pull = admitted_task(external)
    task = builder.build_metadata(values, pull)
    expected = {field: task[field] for field in receiver.IDENTITY}
    control_files = {"scripts/local_ci/runtime/engine.py": "e" * 64}
    control = dict(verified=True, mode="git", actual_sha=task["worker_revision_sha"], files=control_files,
                   tree_sha256=hashlib.sha256(json.dumps(control_files, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    container_files = {path[len('scripts/local_ci/'):]: value for path, value in control_files.items()}
    control['container'] = dict(verified=True, mount_read_only=True, container_id='a' * 64,
                               tree_sha256=hashlib.sha256(json.dumps(container_files, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    result = dict(expected, schema=receiver.SCHEMA, conclusion="success", blocking_reasons=[],
                  checks=[dict(id="architecture_review", status="passed", required=True, reason="Verified contract", evidence=[{"path": "README.md", "reason": "Scope reviewed"}]),
                          dict(id='pr_information', status='passed', required=True, reason='PR intent and scope are complete', evidence=[])]
                         + [dict(id=tool, status="skipped", required=False, reason="Documentation-only scope", evidence=[]) for tool in receiver.TOOLS],
                  ai_review={"summary": "Documentation scope reviewed", 'pr_information': {'status': 'passed', 'summary': 'PR title and description match scope'}, "architecture": {"status": "passed", "summary": "Contract is preserved", "evidence": [{"path": "README.md", "reason": "No architecture change"}]}},
                  evidence=[], source_unchanged=True, validation_scope="production", control_identity=control)
    return task, expected, result


class MetadataAdmissionTests(unittest.TestCase):
    def test_task_id_changes_with_control_or_dispatch_attempt(self):
        values, pull = admitted_task()
        original = builder.build_metadata(values, pull)["task_id"]
        for field, value in (("RUN_ATTEMPT", "2"), ("WORKER_REVISION_SHA", "e" * 40)):
            changed = dict(values, **{field: value})
            self.assertNotEqual(original, builder.build_metadata(changed, pull)["task_id"])

    def test_preflight_and_external_approval_cannot_be_omitted(self):
        values, pull = admitted_task(True)
        for field in ("PREFLIGHT_PASSED", "EXTERNAL_APPROVAL"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                builder.build_metadata(dict(values, **{field: "false"}), pull)

    def test_pr_drift_is_rejected_before_relay_publish(self):
        values, pull = admitted_task()
        for mutation in (lambda p: p["head"].update(sha="e" * 40), lambda p: p["base"].update(sha="e" * 40),
                         lambda p: p.update(draft=True), lambda p: p.update(state="closed")):
            changed = copy.deepcopy(pull)
            mutation(changed)
            with self.assertRaises(ValueError):
                builder.build_metadata(values, changed)

    def test_manual_full_keeps_full_and_uses_ai(self):
        values, _ = admitted_task()
        values.update(PR_NUMBER="", TASK_REF="ci/full/ci_repo", FLAGGEMS_MODE="full", BASE_SHA="", BASE_REF="")
        task = builder.build_metadata(values)
        self.assertEqual((task["event_kind"], task["flaggems_mode"], task["execution_mode"]), ("push", "full", "ai"))


class ResultGateTests(unittest.TestCase):
    def test_current_complete_result_passes(self):
        task, expected, result = result_fixture(True)
        self.assertEqual(receiver.validate_result(result, task, expected), "success")

    def test_every_identity_field_is_bound(self):
        task, expected, result = result_fixture()
        for field in receiver.IDENTITY:
            with self.subTest(field=field), self.assertRaises(receiver.StaleTask):
                receiver.validate_result(dict(result, **{field: "different"}), task, expected)

    def test_legacy_result_never_passes(self):
        task, expected, result = result_fixture()
        result["schema"] = "triton-anchor-local-ci-result/v3"
        with self.assertRaises(ValueError):
            receiver.validate_result(result, task, expected)

    def test_required_skip_failure_and_error_cannot_be_green(self):
        task, expected, result = result_fixture()
        for status in ("skipped", "not_applicable", "failed", "error"):
            result["checks"][0]["status"] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                receiver.validate_result(result, task, expected)

    def test_result_cannot_remove_architecture_or_downgrade_minimum_flags(self):
        task, expected, result = result_fixture()
        missing = copy.deepcopy(result)
        missing["checks"] = missing["checks"][1:]
        with self.assertRaises(ValueError):
            receiver.validate_result(missing, task, expected)
        task["changed_paths"] = ["csrc/Transforms/LowerToLinalg.cpp"]
        # Every dynamic required flag is false, but the trusted minimum still requires compilation.
        for check in result["checks"]:
            check["required"] = False
        with self.assertRaises(ValueError):
            receiver.validate_result(result, task, expected)

    def test_source_attestation_and_review_summary_required(self):
        task, expected, result = result_fixture()
        for mutate in (lambda r: r.pop("source_unchanged"), lambda r: r["ai_review"].update(summary=""),
                       lambda r: r["ai_review"]["architecture"].update(evidence=[])):
            changed = copy.deepcopy(result)
            mutate(changed)
            with self.assertRaises(ValueError):
                receiver.validate_result(changed, task, expected)

    def test_development_or_unverified_control_cannot_satisfy_production(self):
        task, expected, result = result_fixture()
        for mutate in (lambda r: r.update(validation_scope="local_acceptance"),
                       lambda r: r.pop("validation_scope"),
                       lambda r: r["control_identity"].update(verified=False),
                       lambda r: r["control_identity"].update(actual_sha="f" * 40),
                       lambda r: r["control_identity"].update(tree_sha256="f" * 64),
                       lambda r: r["control_identity"]['container'].update(verified=False),
                       lambda r: r["control_identity"]['container'].update(mount_read_only=False),
                       lambda r: r["control_identity"]['container'].update(tree_sha256='f' * 64),
                       lambda r: r["control_identity"].update(files={})):
            changed = copy.deepcopy(result)
            mutate(changed)
            with self.assertRaises(ValueError):
                receiver.validate_result(changed, task, expected)

    def test_pr_information_review_cannot_be_omitted_or_downgraded(self):
        task, expected, result = result_fixture()
        for mutate in (lambda r: r['ai_review'].pop('pr_information'),
                       lambda r: r['ai_review']['pr_information'].update(summary=''),
                       lambda r: r['ai_review']['pr_information'].update(status='failed'),
                       lambda r: r.update(checks=[c for c in r['checks'] if c['id'] != 'pr_information']),
                       lambda r: next(c for c in r['checks'] if c['id'] == 'pr_information').update(status='skipped', required=False)):
            changed = copy.deepcopy(result)
            mutate(changed)
            with self.assertRaises(ValueError):
                receiver.validate_result(changed, task, expected)

    def test_required_tools_need_matching_successful_host_receipts(self):
        task, expected, result = result_fixture()
        task["changed_paths"] = ["python/triton_anchor/__init__.py"]
        policy = receiver.minimum_checks(task["changed_paths"], {"triton_version": "3.0"})
        for check in result["checks"]:
            if check["id"] in policy["required"] and check["id"] != "architecture_review":
                reference = "receipt-" + check["id"]
                check.update(status="passed", evidence=[reference])
                result["evidence"].append(dict(id=reference, tool=check["id"], returncode=0, termination=""))
        self.assertEqual(receiver.validate_result(result, task, expected), "success")
        result["evidence"][0]["tool"] = "custom_test"
        with self.assertRaises(ValueError):
            receiver.validate_result(result, task, expected)

    def test_optional_not_applicable_needs_reason(self):
        task, expected, result = result_fixture()
        result["checks"].append(dict(id="optional_extra", status="not_applicable", required=False, reason="This profile is frontend only"))
        self.assertEqual(receiver.validate_result(result, task, expected), "success")
        result["checks"][-1]["reason"] = ""
        with self.assertRaises(ValueError):
            receiver.validate_result(result, task, expected)

    def test_approval_for_a_different_revision_is_rejected(self):
        task, expected, result = result_fixture(True)
        task["approval"]["tested_sha"] = "e" * 40
        with self.assertRaises(ValueError):
            receiver.validate_result(result, task, expected)

    def test_published_result_hash_and_path_are_verified(self):
        task, expected, result = result_fixture()
        content = json.dumps(result).encode()
        digest = hashlib.sha256(content).hexdigest()
        path = f"runs/{task['task_id']}/run1/result.json"
        index = dict(task_id=task["task_id"], run_id="run1", result_path=path, result_sha256=digest,
                     manifest_path=f"runs/{task['task_id']}/run1/publish-manifest.json")
        manifest = dict(files=[dict(path=path, sha256=digest, size=len(content))])
        self.assertEqual(receiver.validate_artifacts(index, manifest, content, task["task_id"]), result)
        with self.assertRaises(ValueError):
            receiver.validate_artifacts(index, manifest, content + b" ", task["task_id"])
        index["result_path"] = f"runs/{task['task_id']}/run1/../../other/result.json"
        with self.assertRaises(ValueError):
            receiver.validate_artifacts(index, manifest, content, task["task_id"])

    def test_current_github_base_and_merge_checked_before_writes(self):
        task, expected, result = result_fixture()

        class FakeAPI:
            writes = []

            def call(self, method, path, body=None):
                if method != "GET":
                    self.writes.append((method, path, body))
                if "/pulls/" in path:
                    return dict(state="open", draft=False, head=dict(sha=expected["head_sha"]),
                                base=dict(sha="e" * 40, ref=expected["target_branch"]))
                raise AssertionError(path)

        api = FakeAPI()
        with self.assertRaises(receiver.StaleTask):
            receiver.publish(api, expected, result, "success", "https://example.test/result", "local-ci/summary")
        self.assertEqual(api.writes, [])


class ReceiverHTTPTests(unittest.TestCase):
    """Real HTTP requests against local fixtures; no live GitHub or Gitee writes."""

    def run_receiver(self, tamper=False):
        task, expected, result = result_fixture()
        data = json.dumps(result).encode()
        digest = hashlib.sha256(data).hexdigest()
        result_path = f"runs/{task['task_id']}/run1/result.json"
        manifest_path = f"runs/{task['task_id']}/run1/publish-manifest.json"
        index = dict(task_id=task["task_id"], run_id="run1", result_path=result_path,
                     manifest_path=manifest_path, result_sha256=digest)
        manifest = dict(files=[dict(path=result_path, sha256=digest, size=len(data))])
        relay = {"task-metadata.json": json.dumps(task).encode(), f"tasks/{task['task_id']}/latest.json": json.dumps(index).encode(),
                 result_path: data + (b" " if tamper else b""), manifest_path: json.dumps(manifest).encode()}
        writes = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = unquote(urlparse(self.path).path)
                if path.endswith("/files"):
                    value = [{"filename": "README.md", "status": "modified"}]
                elif path.endswith("/pulls/19"):
                    value = dict(state="open", draft=False, head=dict(sha=expected["head_sha"]),
                                 base=dict(sha=expected["base_sha"], ref=expected["target_branch"]))
                elif path.endswith("/git/ref/pull/19/merge"):
                    value = {"object": {"sha": expected["tested_sha"]}}
                elif "/git/commits/" in path:
                    value = {"parents": [{"sha": expected["base_sha"]}, {"sha": expected["head_sha"]}]}
                elif "/contents/" in path:
                    name = path.split("/contents/", 1)[1]
                    content = b"__version__ = '3.0.0'\n" if name == "triton/python/triton/__init__.py" else relay[name]
                    value = {"encoding": "base64", "content": base64.b64encode(content).decode()}
                elif path.endswith("/comments"):
                    value = []
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                writes.append((self.path, body))
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id": 1}')

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = "http://127.0.0.1:" + str(server.server_address[1])
            argv = ["--repository", expected["repository"], "--gitee-owner", "heron-mc", "--gitee-repo", "new-test-relay",
                    "--task-id", task["task_id"], "--task-ref", task["task_ref"], "--sha", expected["tested_sha"],
                    "--worker-revision-sha", expected["worker_revision_sha"], "--target-branch", "main", "--pr-number", "19",
                    "--expected-head-sha", expected["head_sha"], "--comparison-base-sha", expected["base_sha"],
                    "--github-api", url, "--gitee-api", url, "--timeout-seconds", "0"]
            try:
                with patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": "", "GITEE_TOKEN": "", "GITHUB_OUTPUT": ""}):
                    if tamper:
                        with self.assertRaises(ValueError):
                            receiver.main(argv)
                    else:
                        self.assertEqual(receiver.main(argv), 0)
            finally:
                server.shutdown()
                thread.join(timeout=5)
        return writes

    def test_real_http_receives_and_writes_exact_sha_statuses(self):
        writes = self.run_receiver()
        statuses = [item for item in writes if "/statuses/" in item[0]]
        self.assertEqual(len(statuses), 2)
        self.assertTrue(all(body["state"] == "success" for _, body in statuses))
        self.assertEqual(len([item for item in writes if "/comments" in item[0]]), 1)

    def test_bad_published_bytes_produce_no_remote_writes(self):
        self.assertEqual(self.run_receiver(tamper=True), [])


if __name__ == "__main__":
    unittest.main()
