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
        assert "triton-anchor-codex-ai-report/v2" in text
        assert "CODEX_AI_CI_COMPLETE" in text
        assert "changed_files" in text
        assert "behavior_coverage" in text
