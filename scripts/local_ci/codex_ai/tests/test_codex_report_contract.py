import importlib.util
from pathlib import Path

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
) -> dict[str, object]:
    return {
        "id": identifier,
        "command": command_text or f"python -m pytest test_{identifier.lower()}.py",
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
