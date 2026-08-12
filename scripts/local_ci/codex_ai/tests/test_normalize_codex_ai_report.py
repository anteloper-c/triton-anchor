import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "normalize_codex_ai_report.py"
SPEC = importlib.util.spec_from_file_location("normalize_codex_ai_report", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
NORMALIZER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NORMALIZER)


def test_normalizes_executed_command_conservatively():
    document = {
        "verdict": "PASS",
        "findings": [],
        "test_execution": {
            "status": "not_run",
            "summary": "本次没有执行测试。",
            "commands": [{"status": "passed"}],
        },
    }

    changes = NORMALIZER.normalize_document(document)

    assert changes
    assert document["test_execution"]["status"] == "insufficient_evidence"
    assert document["verdict"] == "WARNING"
    assert NORMALIZER.NORMALIZATION_NOTE in document["test_execution"]["summary"]


def test_preserves_not_run_when_every_command_is_not_executed():
    document = {
        "verdict": "PASS",
        "findings": [],
        "test_execution": {
            "status": "not_run",
            "summary": "计划命令未执行。",
            "commands": [{"status": "not_executed"}],
        },
    }

    assert NORMALIZER.normalize_document(document) == []
    assert document["test_execution"]["status"] == "not_run"
    assert document["verdict"] == "PASS"


def test_preserves_high_finding_verdict():
    document = {
        "verdict": "FAIL",
        "findings": [{"severity": "HIGH"}],
        "test_execution": {
            "status": "not_run",
            "summary": ["执行了诊断命令。"],
            "commands": [{"status": "infrastructure_failure"}],
        },
    }

    NORMALIZER.normalize_document(document)

    assert document["test_execution"]["status"] == "insufficient_evidence"
    assert document["verdict"] == "FAIL"


def test_normalized_document_passes_strict_renderer():
    renderer_path = MODULE_PATH.with_name("render_codex_ai_report.py")
    renderer_spec = importlib.util.spec_from_file_location(
        "render_codex_ai_report_for_normalizer_test", renderer_path
    )
    assert renderer_spec is not None and renderer_spec.loader is not None
    renderer = importlib.util.module_from_spec(renderer_spec)
    renderer_spec.loader.exec_module(renderer)

    behavior = {
        "scope": "检查当前改动涉及的代码路径。",
        "strategy": "结合代码差异和已有证据进行检查。",
        "result": "当前证据不足以形成完整测试结论。",
    }
    document = {
        "verdict": "PASS",
        "summary": "完成代码差异检查，但结构化测试状态存在机械矛盾。",
        "merge_recommendation": "建议结合确定性 Local CI 结果决定是否合入。",
        "change_request_assessment": {
            "status": "not_applicable",
            "contributor_goal": "当前任务不是 PR，贡献者目标不适用。",
            "expected_behavior": "当前任务不是 PR，预期行为不适用。",
            "implementation_summary": "已检查当前提交中的代码差异。",
            "evidence": ["代码差异检查命令已经执行。"],
        },
        "changed_files": [],
        "behavior_coverage": {
            "normal": dict(behavior),
            "boundary": dict(behavior),
            "error": dict(behavior),
            "compatibility": dict(behavior),
            "integration": dict(behavior),
        },
        "findings": [],
        "suggested_tests": [],
        "residual_risks": ["尚未执行覆盖产品行为的定向测试。"],
        "test_execution": {
            "status": "not_run",
            "summary": ["执行了代码检查命令。"],
            "generated_test_files": [],
            "commands": [
                {
                    "id": "RUN-001",
                    "command": "git diff --check",
                    "purpose": "代码差异检查",
                    "exit_code": 0,
                    "duration_seconds": 0.1,
                    "status": "passed",
                    "evidence": "代码差异检查命令执行成功。",
                }
            ],
        },
        "completion_marker": "CODEX_AI_CI_COMPLETE",
    }

    NORMALIZER.normalize_document(document)

    assert document["test_execution"]["status"] == "insufficient_evidence"
    assert document["verdict"] == "WARNING"
    assert renderer.validate_report(document, []) == document
