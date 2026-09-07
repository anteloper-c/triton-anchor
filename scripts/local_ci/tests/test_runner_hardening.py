"""Behavioral guards replacing v3 fixed-runner string assertions."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_ci.policy import minimum_checks, TOOLS
from agent_ci.protocol import ContractError, within
from agent_ci.relay import GitRelay


def test_prompt_and_schema_changes_require_control_contracts():
    for path in ('scripts/local_ci/ai_ci_program.md', 'scripts/local_ci/agent_ci/schemas/result.schema.json'):
        assert 'contract_tests' in minimum_checks([{'path': path}], backend_enabled=False)['required_checks']


def test_execution_and_environment_changes_require_all_available_tools():
    for path in ('scripts/local_ci/tools/run_tool.py', 'scripts/local_ci/agent_ci/executor.py', 'triton/cmake/llvm-hash.txt'):
        assert minimum_checks([{'path': path}], backend_enabled=True)['required_checks'] == list(TOOLS)


def test_frontend_profiles_never_claim_backend_capabilities():
    policy = minimum_checks([{'path': 'csrc/changed.cpp'}], backend_enabled=False)
    assert policy['required_checks'] == list(TOOLS[:4])
    assert set(policy['not_applicable']) == set(TOOLS[4:])


def test_rename_and_executable_document_cannot_reduce_checks():
    for change in ({'path': 'docs/a.md', 'old_path': 'csrc/a.cpp'}, {'path': 'docs/a.md', 'mode': '100755'}):
        assert minimum_checks([change], backend_enabled=True)['required_checks'] == list(TOOLS)


def test_source_and_artifact_symlinks_cannot_escape_task(tmp_path):
    root = tmp_path / 'task'
    root.mkdir()
    (tmp_path / 'secret').write_text('private')
    (root / 'link').symlink_to(tmp_path / 'secret')
    with pytest.raises(ContractError):
        within(root, 'link', must_exist=True)


def test_relay_never_contacts_github_or_credential_embedded_urls(tmp_path):
    for url in ('https://github.com/RACE-org/triton-anchor', 'https://user:secret@gitee.com/a/b', 'http://gitee.com/a/b'):
        with pytest.raises(ContractError):
            GitRelay(url, tmp_path / 'relay')


def test_local_transports_require_explicit_simulation(tmp_path):
    with pytest.raises(ContractError):
        GitRelay(str(tmp_path / 'relay.git'), tmp_path / 'work')
