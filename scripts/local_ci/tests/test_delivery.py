"""Result sealing and retrying Git publication against a real local bare remote."""

import json
import subprocess
from unittest.mock import patch

import pytest

from agent_ci.delivery import MAX_FILE_BYTES, seal_result
from agent_ci.protocol import ContractError, TASK_SCHEMA, metadata_digest, task_id
from agent_ci.relay import GitRelay


def task():
    value = {
        "schema": TASK_SCHEMA,
        "repository": "likehupochuan/triton-anchor",
        "event_kind": "pull_request",
        "pr_number": 7,
        "target_branch": "main",
        "tested_sha": "a" * 40,
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
        "worker_revision_sha": "d" * 40,
        "llvm_hash": "e" * 40,
        "full": False,
        "draft": False,
        "state": "open",
        "title": "Fix behavior",
        "description": "Implementation and validation",
        "labels": [],
        "captured_at": "2026-09-11T00:00:00Z",
    }
    value["metadata_digest"] = metadata_digest(value)
    value["task_id"] = task_id(value)
    prefix = f"ci/pr-7/{value['task_id']}"
    value.update(
        task_ref=prefix + "/tested", base_task_ref=prefix + "/base",
        head_task_ref=prefix + "/head",
    )
    return value


def answer():
    return {
        "status": "pass",
        "summary": "Validated behavior",
        "checks": [{
                "tool_id": "frontend_tests", "status": "pass",
                "summary": "Tests passed", "evidence": [],
            }],
        "reviews": [
            {"kind": "pr_info", "status": "pass", "summary": "Clear"},
            {
                "kind": "architecture", "status": "pass",
                "summary": "Compatible", "evidence": ["src/file.py:12"],
            },
        ],
    }


def seal(tmp_path, value):
    return seal_result(
        task(), "20260911-run", value, {"required_checks": ["frontend_tests"]},
        {"profile": "test"}, tmp_path / "run", tmp_path / "sealed",
    )


@pytest.mark.parametrize("missing", ["check", "architecture", "pr_info"])
def test_incomplete_minimum_or_review_cannot_claim_pass(tmp_path, missing):
    value = answer()
    if missing == "check":
        value["checks"] = []
    else:
        value["reviews"] = [row for row in value["reviews"] if row["kind"] != missing]
    result = seal(tmp_path, value)
    assert result["status"] == "infra_error"
    assert result["blocking_reasons"]


def test_high_risk_review_blocks_without_explicit_blocking_flag(tmp_path):
    value = answer()
    value["findings"] = [{"severity": "high", "summary": "Public behavior broken"}]
    assert seal(tmp_path, value)["status"] == "fail"


def test_missing_referenced_file_is_incomplete_but_large_file_is_omitted(tmp_path):
    value = answer()
    value["checks"][0]["evidence"] = ["report.txt"]
    assert seal(tmp_path, value)["status"] == "infra_error"
    artifacts = tmp_path / "run/artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "report.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    result = seal(tmp_path, value)
    assert result["status"] == "pass"
    assert result["artifacts"][0]["omitted"]
    assert not (tmp_path / "sealed/artifacts/report.txt").exists()


def test_only_selected_files_are_published_and_text_is_redacted(tmp_path):
    artifacts = tmp_path / "run/artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "report.xml").write_text("<output>fixture-private-token</output>")
    (artifacts / "all-commands.log").write_text("private full trace")
    value = answer()
    value["checks"][0]["evidence"] = ["report.xml"]
    with patch.dict("os.environ", {"GITEE_TOKEN": "fixture-private-token"}):
        result = seal(tmp_path, value)
    assert result["status"] == "pass"
    assert "fixture-private-token" not in (tmp_path / "sealed/artifacts/report.xml").read_text()
    assert "fixture-private-token" in (artifacts / "report.xml").read_text()
    assert not (tmp_path / "sealed/artifacts/all-commands.log").exists()
    assert [row["path"] for row in result["artifacts"]] == ["report.xml"]


def test_git_result_and_selected_file_commit_together_and_retry_is_idempotent(tmp_path):
    artifacts = tmp_path / "run/artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "report.txt").write_text("ok")
    value = answer()
    value["checks"][0]["evidence"] = ["report.txt"]
    result = seal(tmp_path, value)
    remote = tmp_path / "relay.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    relay = GitRelay(str(remote), tmp_path / "cache", allow_local=True)
    original = relay.git
    lost = False

    def lost_response(args, **kwargs):
        nonlocal lost
        response = original(args, **kwargs)
        if args[0] == "push" and not lost:
            lost = True
            raise RuntimeError("successful push response lost")
        return response

    with patch.object(relay, "git", side_effect=lost_response):
        first = relay.publish_result(task(), result["run_id"], tmp_path / "sealed")
    second = relay.publish_result(task(), result["run_id"], tmp_path / "sealed")
    assert first == second
    branch = relay.results_branch
    count = subprocess.check_output(["git", "rev-list", "--count", branch], cwd=remote).strip()
    assert count == b"1"
    prefix = f"{branch}:runs/{task()['task_id']}/{result['run_id']}"
    published = json.loads(subprocess.check_output(["git", "show", prefix + "/result.json"], cwd=remote))
    assert published == result
    assert subprocess.check_output(["git", "show", prefix + "/artifacts/report.txt"], cwd=remote) == b"ok"
    (tmp_path / "sealed/artifacts/report.txt").write_text("changed")
    with pytest.raises(ContractError):
        relay.publish_result(task(), result["run_id"], tmp_path / "sealed")
