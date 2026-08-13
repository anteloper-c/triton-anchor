import copy
import importlib.util
import io
import json
import tarfile
from argparse import Namespace
from pathlib import Path

import pytest


CODEX_AI_DIR = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


BUILDER = load_module("build_codex_ai_report", CODEX_AI_DIR / "build_codex_ai_report.py")
RENDERER = load_module("render_codex_ai_report", CODEX_AI_DIR / "render_codex_ai_report.py")


def analysis(command: str = "python3 -m pytest generated_tests/test_generated.py"):
    return {
        "summary": "未发现具体缺陷，定向验证结果支持当前修改。",
        "merge_recommendation": "当前未发现需要阻塞合入的问题。",
        "change_request_assessment": {
            "status": "not_applicable",
            "contributor_goal": "当前任务不是 PR，因此没有贡献者功能声明。",
            "expected_behavior": "当前任务不是 PR，因此预期行为声明不适用。",
            "implementation_summary": "本次仅依据代码差异完成自动审查。",
            "evidence": ["任务上下文明确标记为普通推送任务。"],
        },
        "changed_files": [
            {
                "file_id": "FILE-001",
                "summary": "调整了示例代码的执行逻辑。",
                "impact": "可能影响示例代码的返回结果。",
                "validation_strategy": "执行定向测试验证修改后的行为。",
            }
        ],
        "behavior_coverage": {
            name: {
                "scope": f"检查{name}对应的行为路径。",
                "strategy": "结合代码差异和定向验证检查。",
                "result": "未发现新的行为缺陷。",
            }
            for name in ("normal", "boundary", "error", "compatibility", "integration")
        },
        "findings": [],
        "suggested_tests": [],
        "residual_risks": ["本次仅覆盖与代码差异直接相关的路径。"],
        "test_assessment": {
            "evidence_level": "sufficient",
            "summary": ["生成并执行了一个定向测试。"],
            "commands": [
                {
                    "command": command,
                    "purpose": "生成代码路径定向测试",
                    "evidence": "定向测试执行完成。",
                    "failure_classification": "none",
                }
            ],
        },
    }


def write_archive(path: Path, entries: list[tuple[str, bytes, str]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload, kind in entries:
            member = tarfile.TarInfo(name)
            if kind == "file":
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "outside"
                archive.addfile(member)
            else:
                raise AssertionError(kind)


def build(
    tmp_path: Path,
    document: dict,
    ledger: list[dict],
    archive_entries=None,
    *,
    test_generation_expected=True,
):
    repository_root = tmp_path / "repo"
    repository_root.mkdir(exist_ok=True)
    (repository_root / "example.py").write_text("value = 1\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    analysis_path = tmp_path / "analysis.json"
    ledger_path = tmp_path / "ledger.json"
    archive_path = tmp_path / "generated.tar.gz"
    output_path = tmp_path / "report.json"
    manifest = [{"path": "example.py", "change_type": "modified"}]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    analysis_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    write_archive(
        archive_path,
        archive_entries
        if archive_entries is not None
        else [("generated_tests/test_generated.py", b"def test_x(): pass\n", "file")],
    )
    BUILDER.build_report(
        Namespace(
            analysis=analysis_path,
            output=output_path,
            manifest=manifest_path,
            command_ledger=ledger_path,
            generated_archive=archive_path,
            repository_root=repository_root,
            analysis_mode="full",
            test_generation_expected=test_generation_expected,
        )
    )
    report = json.loads(output_path.read_text(encoding="utf-8"))
    RENDERER.validate_report(report, manifest, repository_root)
    return report


def test_builder_assigns_trusted_fields_and_pass_status(tmp_path):
    command = "python3 -m pytest generated_tests/test_generated.py"
    report = build(
        tmp_path,
        analysis(command),
        [{"command": command, "exit_code": 0, "duration_seconds": 0.2}],
    )
    assert report["verdict"] == "PASS"
    assert report["changed_files"][0]["path"] == "example.py"
    assert report["changed_files"][0]["summary"] == "调整了示例代码的执行逻辑。"
    assert report["behavior_coverage"]["normal"]["scope"] == "检查normal对应的行为路径。"
    assert report["test_execution"]["status"] == "passed"
    assert report["test_execution"]["commands"][0] == {
        "id": "RUN-001",
        "purpose": "生成代码路径定向测试",
        "command": command,
        "exit_code": 0,
        "duration_seconds": 0.2,
        "status": "passed",
        "evidence": "定向测试执行完成。",
    }
    assert report["completion_marker"] == "CODEX_AI_CI_COMPLETE"


@pytest.mark.parametrize(
    ("exits", "classification", "expected"),
    [
        ([1], "product", "insufficient_evidence"),
        ([1, 1], "product", "stable_failure"),
        ([1, 0], "flaky", "flaky_failure"),
        ([2], "infrastructure", "infrastructure_failure"),
    ],
)
def test_failure_status_is_derived_from_repeated_ledger(
    tmp_path, exits, classification, expected
):
    command = "python3 -m pytest generated_tests/test_generated.py"
    document = analysis(command)
    semantic = document["test_assessment"]["commands"][0]
    semantic["failure_classification"] = classification
    document["test_assessment"]["commands"] = [
        copy.deepcopy(semantic) for _ in exits
    ]
    ledger = [
        {"command": command, "exit_code": code, "duration_seconds": 0.1}
        for code in exits
    ]
    report = build(tmp_path, document, ledger)
    assert report["test_execution"]["status"] == expected
    assert report["verdict"] == "WARNING"


def test_not_needed_with_no_commands_derives_not_run(tmp_path):
    document = analysis()
    document["test_assessment"] = {
        "evidence_level": "not_needed",
        "summary": ["本次只有文档变化，因此不需要执行测试。"],
        "commands": [],
    }
    report = build(tmp_path, document, [], archive_entries=[])
    assert report["test_execution"]["status"] == "not_run"
    assert report["verdict"] == "PASS"


def test_not_needed_with_successful_inspection_ignores_generation_hint(tmp_path):
    document = analysis("git diff --stat")
    document["test_assessment"]["evidence_level"] = "not_needed"
    report = build(
        tmp_path,
        document,
        [{"command": "git diff --stat", "exit_code": 0, "duration_seconds": 0.1}],
        archive_entries=[],
        test_generation_expected=True,
    )
    assert report["test_execution"]["status"] == "passed"


def test_sufficient_existing_validation_derives_passed_without_generated_files(tmp_path):
    command = "python3 -m pytest scripts/local_ci/results/tests/test_local_ci_bridge.py -q"
    report = build(
        tmp_path,
        analysis(command),
        [{"command": command, "exit_code": 0, "duration_seconds": 0.2}],
        archive_entries=[],
        test_generation_expected=True,
    )
    assert report["test_execution"]["status"] == "passed"
    assert report["test_execution"]["generated_test_files"] == []


def test_insufficient_level_with_annotated_success_derives_passed(tmp_path):
    command = "python3 -m pytest scripts/local_ci/results/tests/test_local_ci_bridge.py -q"
    document = analysis(command)
    document["test_assessment"]["evidence_level"] = "insufficient"
    document["test_assessment"]["summary"] = [
        "执行桥接单测并通过，当前没有列出仍需补充的验证项。"
    ]
    document["residual_risks"] = []
    report = build(
        tmp_path,
        document,
        [{"command": command, "exit_code": 0, "duration_seconds": 0.2}],
        archive_entries=[],
        test_generation_expected=True,
    )
    assert report["test_execution"]["status"] == "passed"


def test_suggested_test_keeps_insufficient_with_successful_command(tmp_path):
    command = "python3 -m pytest scripts/local_ci/results/tests/test_local_ci_bridge.py -q"
    document = analysis(command)
    document["test_assessment"]["evidence_level"] = "insufficient"
    document["suggested_tests"] = [
        {
            "priority": "HIGH",
            "target": "scripts/local_ci/codex_ai/tests/test_local_ci_codex_container.sh",
            "description": "补跑容器契约以覆盖 runner、提示词和报告输出。",
        }
    ]
    report = build(
        tmp_path,
        document,
        [{"command": command, "exit_code": 0, "duration_seconds": 0.2}],
        archive_entries=[],
        test_generation_expected=True,
    )
    assert report["test_execution"]["status"] == "insufficient_evidence"


def test_unannotated_success_does_not_override_insufficient(tmp_path):
    command = "python3 -m pytest scripts/local_ci/results/tests/test_local_ci_bridge.py -q"
    document = analysis("not-an-executed-command")
    document["test_assessment"]["evidence_level"] = "insufficient"
    report = build(
        tmp_path,
        document,
        [{"command": command, "exit_code": 0, "duration_seconds": 0.2}],
        archive_entries=[],
        test_generation_expected=True,
    )
    assert report["test_execution"]["status"] == "insufficient_evidence"


def test_unmatched_semantic_command_is_ignored(tmp_path):
    document = analysis("not-an-executed-command")
    document["test_assessment"]["evidence_level"] = "not_needed"
    report = build(tmp_path, document, [], archive_entries=[])
    assert report["test_execution"]["commands"] == []
    assert report["test_execution"]["status"] == "not_run"


def test_unknown_file_id_is_rejected(tmp_path):
    document = analysis()
    document["findings"] = [
        {
            "severity": "MEDIUM",
            "category": "correctness",
            "file_id": "FILE-999",
            "line": "1",
            "code_role": "该行负责计算返回结果。",
            "title": "返回结果错误",
            "evidence": "该表达式会产生错误结果。",
            "impact": "调用方会收到错误结果。",
            "fix_direction": "修正该表达式并补充测试。",
        }
    ]
    with pytest.raises(ValueError, match="retained changed file"):
        build(tmp_path, document, [])


def test_invalid_changed_file_annotation_is_rejected(tmp_path):
    document = analysis()
    document["changed_files"][0]["file_id"] = "FILE-999"
    with pytest.raises(ValueError, match="does not cover the Git manifest"):
        build(tmp_path, document, [])


def test_missing_changed_file_annotation_is_rejected(tmp_path):
    document = analysis()
    document["changed_files"] = []
    with pytest.raises(ValueError, match="does not cover the Git manifest"):
        build(tmp_path, document, [])


def test_missing_behavior_category_is_rejected(tmp_path):
    document = analysis()
    del document["behavior_coverage"]["integration"]
    with pytest.raises(ValueError, match="behavior_coverage has invalid keys"):
        build(tmp_path, document, [])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("evidence_level", "maybe", "test_assessment.evidence_level is invalid"),
        ("command_classification", "maybe", "failure_classification is invalid"),
    ],
)
def test_invalid_semantic_enum_is_rejected(tmp_path, field, value, message):
    document = analysis()
    if field == "evidence_level":
        document["test_assessment"]["evidence_level"] = value
    else:
        document["test_assessment"]["commands"][0]["failure_classification"] = value
    with pytest.raises(ValueError, match=message):
        build(tmp_path, document, [])


def test_semantic_summary_overflow_and_duplicates_are_rejected(tmp_path):
    document = analysis()
    document["test_assessment"]["summary"] = ["重复说明。", "重复说明。"]
    with pytest.raises(ValueError, match="must not contain duplicates"):
        build(tmp_path, document, [])

    document = analysis()
    document["change_request_assessment"]["evidence"] = [
        f"第 {index} 条判断依据。" for index in range(9)
    ]
    with pytest.raises(ValueError, match="at most 8 items"):
        build(tmp_path, document, [])


def test_workspace_archive_only_reports_test_paths(tmp_path):
    report = build(
        tmp_path,
        analysis(),
        [
            {
                "command": "python3 -m pytest generated_tests/test_generated.py",
                "exit_code": 0,
                "duration_seconds": 0.2,
            }
        ],
        archive_entries=[
            ("generated_tests/test_generated.py", b"def test_x(): pass\n", "file"),
            ("python/triton_anchor/runtime.py", b"changed = True\n", "file"),
            ("diagnostics/notes.txt", b"notes\n", "file"),
        ],
    )
    assert report["test_execution"]["generated_test_files"] == [
        "generated_tests/test_generated.py"
    ]


def test_english_and_missing_noncritical_fields_are_normalized(tmp_path):
    document = analysis()
    document["summary"] = "English-only summary."
    document["merge_recommendation"] = ""
    document["change_request_assessment"]["evidence"] = []
    document["test_assessment"]["summary"] = []
    report = build(
        tmp_path,
        document,
        [
            {
                "command": "python3 -m pytest generated_tests/test_generated.py",
                "exit_code": 0,
                "duration_seconds": 0.2,
            }
        ],
    )
    assert report["summary"].startswith("Codex 原始说明：")
    assert "确定性 CI" in report["merge_recommendation"]
    assert report["change_request_assessment"]["evidence"]
    assert report["test_execution"]["summary"]


def test_unreported_ledger_command_cannot_result_in_not_run(tmp_path):
    document = analysis("command-not-present-in-ledger")
    document["test_assessment"]["evidence_level"] = "not_needed"
    actual = "python3 -m pytest generated_tests/test_generated.py"
    report = build(
        tmp_path,
        document,
        [{"command": actual, "exit_code": 0, "duration_seconds": 0.2}],
    )
    assert report["test_execution"]["commands"][0]["command"] == actual
    assert report["test_execution"]["status"] == "passed"


def test_omitted_semantic_commands_do_not_hide_failure_ledger(tmp_path):
    document = analysis()
    document["test_assessment"]["commands"] = []
    document["test_assessment"]["evidence_level"] = "not_needed"
    report = build(
        tmp_path,
        document,
        [
            {
                "command": "python3 -m pytest generated_tests/test_generated.py",
                "exit_code": 1,
                "duration_seconds": 0.2,
            }
        ],
    )
    assert len(report["test_execution"]["commands"]) == 1
    assert report["test_execution"]["status"] == "insufficient_evidence"


@pytest.mark.parametrize(
    "entry",
    [
        ("../escape.py", b"x", "file"),
        ("generated_tests/link.py", b"", "symlink"),
    ],
)
def test_generated_archive_rejects_escape_and_symlink(tmp_path, entry):
    with pytest.raises(ValueError):
        build(tmp_path, analysis(), [], archive_entries=[entry])
