"""Native CLI observations are private; shared report validation decides coverage."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_ci.native_audit import NativeAudit


class NativeAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "native-exploration.jsonl"
        self.event_log = self.path.with_name("codex-events.jsonl")
        self.identity = {
            "task_id": "a" * 64,
            "tested_sha": "b" * 40,
            "attempt_id": "attempt-1",
            "environment_fingerprint": {"image": "sha256:abc"},
        }

    def records(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    @staticmethod
    def event(event_type="item.completed", **item):
        return {
            "type": event_type,
            "item": {"id": "cmd_1", "type": "command_execution", **item},
        }

    def test_incremental_commands_are_visible_and_completion_is_durable(self):
        with NativeAudit(self.path, self.identity, self.event_log) as audit:
            audit.ingest(
                self.event(
                    "item.started", command="python reproduce.py", status="in_progress"
                )
            )
            self.assertEqual(len(self.records()), 1)
            audit.ingest(
                self.event(
                    "item.updated",
                    command="python reproduce.py",
                    aggregated_output="starting\n",
                )
            )
            with mock.patch("agent_ci.native_audit.os.fsync", wraps=os.fsync) as sync:
                audit.ingest(
                    self.event(
                        command="python reproduce.py",
                        aggregated_output="assertion failed\n",
                        exit_code=1,
                        status="failed",
                    )
                )
                sync.assert_called_once()
        records = self.records()
        self.assertEqual(
            [row["event_type"] for row in records],
            ["item.started", "item.updated", "item.completed"],
        )
        self.assertEqual(records[-1]["exit_code"], 1)
        self.assertEqual(records[-1]["aggregated_output"], "assertion failed\n")
        self.assertEqual(records[-1]["native_status"], "failed")
        for record in records:
            for field, value in self.identity.items():
                self.assertEqual(record[field], value)
            self.assertEqual(record["source_event_log"], str(self.event_log))
            self.assertEqual(record["evidence_kind"], "native_observation")
            self.assertNotIn("counts_as_check", record)

    def test_file_changes_and_forged_check_fields_never_become_formal_results(self):
        with NativeAudit(self.path, self.identity, self.event_log) as audit:
            audit.ingest(
                self.event(
                    type="file_change",
                    changes=[
                        {
                            "path": "/task/candidate/checkout/probe.py",
                            "kind": "add",
                            "diff": "+assert 1 == 1",
                        }
                    ],
                    status="completed",
                    counts_as_check=True,
                    evidence_kind="check",
                    tool_id="frontend_smoke",
                    success=True,
                )
            )
        record = self.records()[0]
        self.assertEqual(record["changes"][0]["diff"], "+assert 1 == 1")
        self.assertNotIn("counts_as_check", record)
        self.assertEqual(record["evidence_kind"], "native_observation")
        self.assertNotIn("tool_id", record)
        self.assertNotIn("success", record)
        self.assertNotIn("status", record)

    def test_redacts_embedded_secrets_recursively_before_writing(self):
        self.identity["environment_fingerprint"] = {
            "nested-secret": ["prefix-long-secret-suffix"]
        }
        with NativeAudit(
            self.path,
            self.identity,
            self.event_log,
            secrets=["long-secret", "secret", "", None],
        ) as audit:
            audit.ingest(
                self.event(command="echo long-secret", aggregated_output="secret")
            )
            audit.ingest(
                self.event(
                    type="file_change",
                    changes=[
                        {
                            "path": "/tmp/long-secret",
                            "kind": "add",
                            "diff": "+TOKEN=secret",
                        }
                    ],
                )
            )
        serialized = self.path.read_text()
        self.assertNotIn("secret", serialized)
        self.assertNotIn("long-", serialized)
        self.assertEqual(self.records()[0]["command"], "echo [REDACTED]")
        self.assertEqual(self.records()[1]["changes"][0]["diff"], "+TOKEN=[REDACTED]")
        self.assertEqual(
            self.records()[0]["environment_fingerprint"],
            {"nested-[REDACTED]": ["prefix-[REDACTED]-suffix"]},
        )

    def test_malformed_and_non_native_events_are_ignored(self):
        events = [
            None,
            [],
            "x",
            {},
            {"type": []},
            self.event(type={}),
            {"type": "thread.started", "thread_id": "abc"},
            {"type": "item.completed", "item": []},
            self.event(id=None),
            self.event(id=""),
            self.event(type="agent_message", text="All checks passed"),
            self.event(command=["rm", "x"]),
            self.event(aggregated_output={"result": "green"}),
            self.event(exit_code=True),
            self.event(type="file_change", changes={}),
            self.event(type="file_change", changes=[{"path": "x"}]),
            self.event("turn.completed", command="echo OK"),
        ]
        with NativeAudit(self.path, self.identity, self.event_log) as audit:
            for event in events:
                audit.ingest(event)
        self.assertEqual(self.records(), [])

    def test_restart_appends_and_restores_private_mode(self):
        with NativeAudit(self.path, self.identity, self.event_log) as audit:
            audit.ingest(self.event(command="echo first", exit_code=0))
        self.path.chmod(0o644)
        identity = {**self.identity, "attempt_id": "attempt-2"}
        with NativeAudit(self.path, identity, self.event_log) as audit:
            audit.ingest(self.event(command="echo second", exit_code=0))
        self.assertEqual(
            [row["attempt_id"] for row in self.records()], ["attempt-1", "attempt-2"]
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_identity_is_frozen_and_unicode_paths_roundtrip(self):
        with NativeAudit(self.path, self.identity, self.event_log) as audit:
            self.identity["environment_fingerprint"]["image"] = "changed"
            audit.ingest(
                self.event(
                    type="file_change",
                    changes=[{"path": "/tmp/测试-\udcff.py", "kind": "add"}],
                )
            )
        self.assertEqual(
            self.records()[0]["environment_fingerprint"], {"image": "sha256:abc"}
        )
        self.assertEqual(self.records()[0]["changes"][0]["path"], "/tmp/测试-\udcff.py")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "Linux host file protections")
    def test_refuses_a_symlink_instead_of_changing_its_target(self):
        target = self.path.with_name("target")
        target.write_text("private-other-data")
        target.chmod(0o644)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            NativeAudit(self.path, self.identity, self.event_log)
        self.assertEqual(target.read_text(), "private-other-data")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
