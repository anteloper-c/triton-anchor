import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "codex_jsonl_evidence.py"
SPEC = importlib.util.spec_from_file_location("codex_jsonl_evidence", MODULE_PATH)
EVIDENCE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(EVIDENCE)


def test_extracts_exit_code_duration_and_normalized_shell_command(tmp_path):
    log_path = tmp_path / "codex.log"
    output_path = tmp_path / "ledger.json"
    events = [
        {
            "type": "item.started",
            "_runner_recorded_at_seconds": 10.0,
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "/bin/bash -lc 'python3 -m pytest test_one.py'",
            },
        },
        {
            "type": "item.completed",
            "_runner_recorded_at_seconds": 10.25,
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "/bin/bash -lc 'python3 -m pytest test_one.py'",
                "exit_code": 0,
            },
        },
    ]
    log_path.write_text(
        "noise\n" + "\n".join(json.dumps(item) for item in events) + "\n",
        encoding="utf-8",
    )
    assert EVIDENCE.extract(log_path, output_path) == 0
    assert json.loads(output_path.read_text(encoding="utf-8")) == [
        {
            "command": "python3 -m pytest test_one.py",
            "exit_code": 0,
            "duration_seconds": 0.25,
        }
    ]


def test_incomplete_command_event_is_not_fabricated(tmp_path):
    log_path = tmp_path / "codex.log"
    output_path = tmp_path / "ledger.json"
    log_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "pytest"},
            }
        ),
        encoding="utf-8",
    )
    EVIDENCE.extract(log_path, output_path)
    assert json.loads(output_path.read_text(encoding="utf-8")) == []


def test_has_event_requires_a_top_level_event_type(tmp_path):
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "item.completed", "output": '"turn.completed"'}),
                json.dumps({"message": {"type": "turn.completed"}}),
            ]
        ),
        encoding="utf-8",
    )
    assert not EVIDENCE.has_event(log_path, "turn.completed")
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write("\n" + json.dumps({"type": "turn.completed"}) + "\n")
    assert EVIDENCE.has_event(log_path, "turn.completed")
