import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


CODEX_AI_DIR = Path(__file__).resolve().parents[1]
RENDERER_PATH = CODEX_AI_DIR / "render_codex_ai_report.py"
SPEC = importlib.util.spec_from_file_location("render_codex_ai_report", RENDERER_PATH)
RENDERER = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(RENDERER)


def command(
    identifier: str,
    status: str,
    exit_code: int,
    command_text: str | None = None,
    purpose: str = "相关功能定向测试",
) -> dict[str, object]:
    return {
        "id": identifier,
        "command": command_text or f"python -m pytest test_{identifier.lower()}.py",
        "purpose": purpose,
        "exit_code": exit_code,
        "duration_seconds": 0.1,
        "status": status,
        "evidence": "命令执行结果已记录。",
    }


def report(
    execution_status: str,
    commands: list[dict[str, object]],
    *,
    verdict: str,
) -> dict[str, object]:
    behavior = {
        "scope": "检查当前变更涉及的路径。",
        "strategy": "结合静态代码和命令证据检查。",
        "result": "未发现额外问题。",
    }
    return {
        "verdict": verdict,
        "summary": "完成代码差异审查和验证证据检查。",
        "merge_recommendation": "根据当前证据决定是否合入。",
        "change_request_assessment": {
            "status": "implemented",
            "contributor_goal": "贡献者希望修复当前代码路径中的问题。",
            "expected_behavior": "修改后相关路径应按声明正常工作。",
            "implementation_summary": "代码差异实现了声明目标。",
            "evidence": [
                "代码差异覆盖了贡献者声明的主要路径。",
                "定向验证结果支持当前实现情况判断。",
            ],
        },
        "changed_files": [],
        "behavior_coverage": {
            name: dict(behavior)
            for name in (
                "normal",
                "boundary",
                "error",
                "compatibility",
                "integration",
            )
        },
        "findings": [],
        "suggested_tests": [],
        "residual_risks": [],
        "test_execution": {
            "status": execution_status,
            "summary": "测试执行状态与命令证据保持一致。",
            "generated_test_files": [],
            "commands": commands,
        },
        "completion_marker": "CODEX_AI_CI_COMPLETE",
    }


@pytest.mark.parametrize(
    ("execution_status", "commands", "verdict"),
    [
        ("passed", [command("RUN-001", "passed", 0)], "PASS"),
        ("not_run", [], "PASS"),
        ("insufficient_evidence", [], "WARNING"),
        (
            "stable_failure",
            [
                command("RUN-001", "stable_failure", 1, "python -m pytest test_a.py"),
                command("RUN-002", "stable_failure", 1, "python -m pytest test_a.py"),
            ],
            "WARNING",
        ),
        (
            "flaky_failure",
            [
                command("RUN-001", "flaky_failure", 1, "python -m pytest test_a.py"),
                command("RUN-002", "passed", 0, "python -m pytest test_a.py"),
            ],
            "WARNING",
        ),
        (
            "infrastructure_failure",
            [command("RUN-001", "infrastructure_failure", 2)],
            "WARNING",
        ),
        ("test_generation_error", [], "WARNING"),
    ],
)
def test_valid_test_execution_status_matrix(execution_status, commands, verdict):
    document = report(execution_status, commands, verdict=verdict)
    assert RENDERER.validate_report(document, []) == document


@pytest.mark.parametrize(
    "document",
    [
        report("passed", [], verdict="PASS"),
        report("passed", [command("RUN-001", "passed", 1)], verdict="PASS"),
        report("not_run", [command("RUN-001", "passed", 0)], verdict="PASS"),
        report(
            "stable_failure",
            [command("RUN-001", "stable_failure", 1)],
            verdict="WARNING",
        ),
        report(
            "flaky_failure",
            [command("RUN-001", "flaky_failure", 1)],
            verdict="WARNING",
        ),
        report("insufficient_evidence", [], verdict="PASS"),
    ],
)
def test_rejects_inconsistent_test_execution(document):
    with pytest.raises(ValueError):
        RENDERER.validate_report(document, [])


def test_rejects_unknown_change_request_assessment_status():
    document = report("not_run", [], verdict="PASS")
    document["change_request_assessment"]["status"] = "unknown"
    with pytest.raises(ValueError, match="change_request_assessment.status"):
        RENDERER.validate_report(document, [])


def test_legacy_string_change_request_evidence_remains_supported():
    document = report("not_run", [], verdict="PASS")
    document["change_request_assessment"]["evidence"] = "旧报告只有一条中文判断依据。"

    assert RENDERER.validate_report(document, []) == document
    comment = RENDERER.render_comment(
        document,
        SimpleNamespace(constraint_status="pass", constraint_reason="约束检查通过。"),
    )
    assert "- 判断依据：\n  - 旧报告只有一条中文判断依据。" in comment


@pytest.mark.parametrize(
    "evidence",
    [[], ["重复依据。", "重复依据。"], ["English only"]],
)
def test_rejects_invalid_change_request_evidence(evidence):
    document = report("not_run", [], verdict="PASS")
    document["change_request_assessment"]["evidence"] = evidence

    with pytest.raises(ValueError, match="change_request_assessment.evidence"):
        RENDERER.validate_report(document, [])


@pytest.mark.parametrize(
    "summary",
    [[], ["重复说明。", "重复说明。"], ["English only"]],
)
def test_rejects_invalid_test_execution_summary(summary):
    document = report("not_run", [], verdict="PASS")
    document["test_execution"]["summary"] = summary

    with pytest.raises(ValueError, match="test_execution.summary"):
        RENDERER.validate_report(document, [])


def test_comment_explains_goal_implementation_and_non_blocking_role():
    document = report(
        "passed",
        [
            command(
                "RUN-001", "passed", 0, purpose="主要代码路径定向测试"
            )
        ],
        verdict="PASS",
    )
    document["change_request_assessment"]["evidence"] = [
        "代码差异覆盖了声明的正常路径。",
        "RUN-001 支持该判断。",
    ]
    document["test_execution"]["summary"] = [
        "RUN-001 已通过。",
        "验证覆盖了主要代码路径。",
    ]
    comment = RENDERER.render_comment(
        document,
        SimpleNamespace(
            constraint_status="pass",
            constraint_reason="约束检查通过。",
            local_ci_status=0,
        ),
    )

    assert "## Codex AI 自动审查" in comment
    assert "Codex AI 自动审查仅供参考" in comment
    assert "合入门禁" in comment
    assert "### 审查摘要" in comment
    assert "本地确定性 CI 检查：已通过" in comment
    assert "### 贡献者目标与实现情况" in comment
    assert "贡献者目标：贡献者希望修复当前代码路径中的问题" in comment
    assert "预期效果：修改后相关路径应按声明正常工作" in comment
    assert "当前实现情况：代码差异实现了声明目标" in comment
    assert "- 判断依据：\n  - 代码差异覆盖了声明的正常路径。" in comment
    assert "  - 主要代码路径定向测试支持该判断。" in comment
    assert "- 说明：\n  - 主要代码路径定向测试已通过。" in comment
    assert "  - 验证覆盖了主要代码路径。" in comment
    assert "补充验证结果：**所执行的验证命令均通过**" in comment
    assert "RUN-001" not in comment
    assert "Local CI" not in comment
    assert "Codex AI CI" not in comment
    assert "### 验证情况" in comment


def test_finding_location_is_checked_against_changed_checkout_file(tmp_path):
    source_file = tmp_path / "module.py"
    source_file.write_text(
        "def read_cache(version):\n"
        "    return cache[version]\n",
        encoding="utf-8",
    )
    document = report("not_run", [], verdict="WARNING")
    document["changed_files"] = [
        {
            "path": "module.py",
            "change_type": "modified",
            "summary": "调整缓存读取逻辑。",
            "impact": "影响版本对应的缓存返回值。",
            "validation_strategy": "通过静态检查确认当前调用路径。",
        }
    ]
    document["findings"] = [
        {
            "id": "AI-001",
            "severity": "MEDIUM",
            "category": "correctness",
            "file": "module.py",
            "line": "2",
            "code_role": "该行负责根据版本键读取对应缓存值。",
            "title": "缓存读取没有处理缺失版本键",
            "evidence": "代码直接索引缓存，缺失键会抛出异常。",
            "impact": "调用方在边界输入下可能无法获得预期结果。",
            "fix_direction": "在读取前处理缺失版本键并补充回归测试。",
        }
    ]
    expected_files = [{"path": "module.py", "change_type": "modified"}]

    assert RENDERER.validate_report(document, expected_files, tmp_path) == document

    document["findings"][0]["line"] = "3"
    with pytest.raises(ValueError, match="outside"):
        RENDERER.validate_report(document, expected_files, tmp_path)
