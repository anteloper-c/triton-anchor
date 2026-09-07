"""Codex remains task-scoped during publication recovery; builds stay sealed."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .protocol import ContractError, within


class PublicationSupervisor:
    def __init__(self, task: dict, journal, run_dir: Path, reason: str):
        self.task, self.journal, self.run_dir, self.reason = task, journal, Path(run_dir), reason
        self.cancelled = threading.Event()
        self.closed = True

    def context(self):
        box = self.journal.outbox(self.task["task_id"])
        return {"task": self.task, "phase": "publication_recovery", "publication_error": self.reason,
                "result": json.loads(Path(box["payload_path"]).read_text()),
                "publication": {key: box[key] for key in ("digest", "attempts", "published", "receipt")},
                "instructions": "Build evidence is sealed. Read existing logs, diagnose the relay/receipt failure, and request retry_publication; do not rebuild."}

    def read_artifact(self, execution_id: str, path: str = "execution.log", offset: int = 0):
        records = self.journal.executions(self.task["task_id"])
        record = next((r for r in records if r["execution_id"] == execution_id), None)
        if not record or not record.get("artifact_dir") or type(offset) is not int or offset < 0:
            raise ContractError("Invalid task evidence")
        file = within(self.run_dir / "published/evidence" / execution_id, path, must_exist=True)
        with file.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(32768)
        return {"content": data.decode(errors="replace"), "next_offset": offset + len(data)}

    def retry_publication(self, reason: str):
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError("Recovery needs an explanation")
        self.journal.event(self.task["task_id"], "publication_retry_requested", {"reason": reason})
        return {"queued": True, "builds_restarted": False}

    def finish(self, summary=""):
        self.journal.event(self.task["task_id"], "publication_review", {"summary": summary})
        return {"phase": "awaiting_publication_or_receipt", "complete": False}

    def __getattr__(self, name):
        if name in {"start_check", "run_custom", "submit_review", "read_file", "poll_check"}:
            def sealed(**kwargs):
                raise ContractError("Build/test phase is sealed; only publication recovery is allowed")
            return sealed
        raise AttributeError(name)
