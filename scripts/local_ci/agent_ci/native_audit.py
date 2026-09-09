"""Private index of Codex CLI native exploration events, never CI check facts.

The CLI JSON stream describes commands and file changes it reports. This is an
execution aid, not an exhaustive operating-system audit or proof that a formal
check ran. The unindexed stream remains in the separate private event log.
"""
from __future__ import annotations

import copy
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


IDENTITY_FIELDS = ("task_id", "tested_sha", "attempt_id", "environment_fingerprint")
EVENT_TYPES = {"item.started", "item.updated", "item.completed"}
ITEM_TYPES = {"command_execution", "file_change"}


class NativeAudit:
    def __init__(self, path: Path, identity: dict, event_log: Path,
                 secrets: Iterable[str] = ()):
        self.path = Path(path)
        self.identity = {name: copy.deepcopy(identity[name]) for name in IDENTITY_FIELDS}
        self.event_log = str(event_log)
        self.secrets = sorted({value for value in secrets if isinstance(value, str) and value},
                              key=len, reverse=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(self.path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Native audit must be a regular private file")
            os.fchmod(descriptor, 0o600)
            self.stream = os.fdopen(descriptor, "a", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        return value

    def ingest(self, event: dict) -> None:
        if (not isinstance(event, dict) or not isinstance(event.get("type"), str)
                or event["type"] not in EVENT_TYPES):
            return
        item = event.get("item")
        if (not isinstance(item, dict) or not isinstance(item.get("type"), str)
                or item["type"] not in ITEM_TYPES):
            return
        if not isinstance(item.get("id"), str) or not item["id"]:
            return
        native = {"item_id": item["id"], "native_type": item["type"]}
        # Whitelist native observations: model-supplied fields such as tool_id,
        # success or counts_as_check must never acquire formal result semantics.
        if isinstance(item.get("status"), str):
            native["native_status"] = item["status"]
        if item["type"] == "command_execution":
            for field in ("command", "aggregated_output"):
                if field in item:
                    if not isinstance(item[field], str):
                        return
                    native[field] = item[field]
            if "exit_code" in item:
                if item["exit_code"] is not None and type(item["exit_code"]) is not int:
                    return
                native["exit_code"] = item["exit_code"]
        else:
            changes = item.get("changes", [])
            if not isinstance(changes, list):
                return
            native["changes"] = []
            for change in changes:
                if (not isinstance(change, dict) or not isinstance(change.get("path"), str)
                        or not isinstance(change.get("kind"), str)):
                    return
                native["changes"].append({key: change[key] for key in ("path", "kind", "diff", "old_path")
                                          if isinstance(change.get(key), str)})
        record = {"schema": "triton-anchor-native-exploration/v1",
                  **self.identity, "source_event_log": self.event_log,
                  "recorded_at": datetime.now(timezone.utc).isoformat(),
                  "evidence_kind": "exploration", "counts_as_check": False,
                  "event_type": event["type"], **native}
        self.stream.write(json.dumps(self._redact(record), ensure_ascii=True,
                                     separators=(",", ":")) + "\n")
        self.stream.flush()
        if event["type"] == "item.completed":
            os.fsync(self.stream.fileno())

    def close(self) -> None:
        if not self.stream.closed:
            try:
                self.stream.flush()
                os.fsync(self.stream.fileno())
            finally:
                self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
