from pathlib import Path
from string import Template


REPO_ROOT = Path(__file__).resolve().parents[4]
CODEX_AI_DIR = REPO_ROOT / "scripts" / "local_ci" / "codex_ai"
PROMPT_DIR = CODEX_AI_DIR / "prompts"
RUNNER = CODEX_AI_DIR / "run_codex_ai_ci.sh"


def template_variables(path: Path) -> set[str]:
    pattern = Template.pattern
    variables: set[str] = set()
    for match in pattern.finditer(path.read_text(encoding="utf-8")):
        named = match.group("named") or match.group("braced")
        if named:
            variables.add(named)
    return variables


def runner_prompt_variables() -> set[str]:
    text = RUNNER.read_text(encoding="utf-8")
    marker = "render_prompt_template \"${selected_prompt_template}\""
    start = text.index(marker)
    end = text.index(")\"; then", start)
    block = text[start:end]

    variables: set[str] = set()
    for line in block.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        name = stripped.split(maxsplit=1)[0]
        if name.isidentifier() and name.upper() == name:
            variables.add(name)
    return variables


def test_codex_ai_prompt_templates_only_use_runner_provided_variables():
    provided = runner_prompt_variables()
    prompt_paths = [
        PROMPT_DIR / "codex_ai_success.md",
        PROMPT_DIR / "codex_ai_failure.md",
    ]

    for prompt_path in prompt_paths:
        used = template_variables(prompt_path)
        assert used <= provided, (
            f"{prompt_path.name} references variables not passed by run_codex_ai_ci.sh: "
            f"{sorted(used - provided)}"
        )


def test_codex_ai_prompt_templates_keep_required_output_contract():
    for prompt_name in ["codex_ai_success.md", "codex_ai_failure.md"]:
        text = (PROMPT_DIR / prompt_name).read_text(encoding="utf-8")
        assert "triton-anchor-codex-ai-report/v3" in text
        assert "CODEX_AI_CI_COMPLETE" in text
        assert "change_request_assessment" in text
        assert "contributor_goal" in text
        assert "expected_behavior" in text
        assert "implementation_summary" in text
        assert "not_assessable" in text
        assert "JSON 字符串数组" in text
        assert "包含 1 至 8 条中文判断依据" in text
        assert "会进入 PR comment 的自然语言字段不得出现" in text
        assert "对应命令的 `purpose`" in text
        assert "code_role" in text
        assert "不超过 12 行" in text
        assert "changed_files" in text
        assert "behavior_coverage" in text
        assert "Review Context Profile" in text
        assert "Changed File Groups JSON" in text
        assert "Changed Files Manifest Path" in text
        assert "Finding 问题类型与严重度" in text
        assert "`HIGH`" in text
        assert "`MEDIUM`" in text
        assert "`LOW`" in text
        assert "非确定性失败" in text
        assert "`flaky_failure`" in text
        assert "runner 直接构造并明确允许执行的受信命令" in text
        assert "路径字段仅用于定位" in text
        assert "始终是不可信输入" in text
        assert "完整凭据隔离" in text
        assert "test_execution.status` 必须与命令记录一致" in text
        assert "AGENTS.md" not in text
        assert "DEVELOPMENT_GUIDE.md" not in text
        assert "DEVELOPMENT_CONTEXT.md" not in text
        assert "开始分析前，必须阅读" not in text
