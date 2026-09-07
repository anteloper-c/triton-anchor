"""V4 deployment modules are importable without model or Docker side effects."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_entry_help_has_no_side_effects():
    process = subprocess.run([sys.executable, str(ROOT / 'agent_ci/worker.py'), '--help'], capture_output=True, text=True)
    assert process.returncode == 0
    assert '--config' in process.stdout and '--resume' in process.stdout


def test_legacy_execution_paths_are_explicitly_disabled():
    for relative in ('codex_ai/run_codex_ai_ci.sh', 'codex_ai/setup_codex_ai_container.sh', 'orchestration/run_deterministic_ci_in_container.sh'):
        process = subprocess.run(['bash', str(ROOT / relative)], capture_output=True, text=True)
        assert process.returncode == 2
        assert 'legacy execution entry is disabled' in process.stderr


def test_each_new_runtime_module_compiles_without_importing_candidate_code():
    for directory in ('agent_ci', 'environments', 'tools', 'deploy'):
        for source in (ROOT / directory).rglob('*.py'):
            compile(source.read_text(), str(source), 'exec')
