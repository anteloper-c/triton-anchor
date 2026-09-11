"""Task lifecycle and retryable result publication state."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .protocol import ContractError, atomic_json, canonical, current_key


class Journal:
    """Host-only state. One worker process owns the lock; its threads serialize here."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.runs = self.root / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)
        self.guard = threading.RLock()

    @staticmethod
    def _component(value):
        if (
            not isinstance(value, str)
            or not value
            or Path(value).name != value
            or value in {".", ".."}
        ):
            raise ContractError("Invalid task/run path component")
        return value

    def run_dir(self, task_id, run_id=None):
        parent = self.runs / self._component(task_id)
        if run_id:
            return parent / self._component(run_id)
        candidates = sorted(parent.glob("*/state.json"))
        if not candidates:
            raise ContractError("Unknown task")
        return candidates[-1].parent

    def _state(self, task_id):
        return json.loads((self.run_dir(task_id) / "state.json").read_text())

    def _write(self, task_id, state):
        state["updated"] = time.time()
        atomic_json(self.run_dir(task_id, state["run_id"]) / "state.json", state)

    def _new(self, task):
        run_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        directory = self.run_dir(task["task_id"], run_id)
        for name in ("logs", "artifacts"):
            (directory / name).mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "task.json", task)
        atomic_json(
            directory / "state.json",
            {
                "task_id": task["task_id"],
                "run_id": run_id,
                "phase": "preparing",
                "updated": time.time(),
                "detail": {},
                "events": [],
                "delivery": None,
            },
        )
        return self.task(task["task_id"])

    def register(self, task):
        with self.guard:
            parent = self.runs / self._component(task["task_id"])
            if any(parent.glob("*/state.json")):
                row = self.task(task["task_id"])
                if json.loads(row["manifest"]) != task:
                    raise ContractError("Immutable task manifest changed")
                return row
            return self._new(task)

    def task(self, task_id):
        with self.guard:
            state = self._state(task_id)
            task = json.loads((self.run_dir(task_id) / "task.json").read_text())
            return {
                "task_id": task_id,
                "subject": current_key(task),
                "manifest": canonical(task).decode(),
                "run_id": state["run_id"],
                "phase": state["phase"],
                "updated": state["updated"],
                "detail": canonical(state.get("detail", {})).decode(),
            }

    def tasks(self, *, active=True):
        with self.guard:
            rows = [
                self.task(path.name)
                for path in self.runs.iterdir()
                if path.is_dir() and any(path.glob("*/state.json"))
            ]
            return sorted(
                [r for r in rows if not active or r["phase"] != "published"],
                key=lambda r: r["updated"],
            )

    def phase(self, task_id, phase, detail=None):
        if phase not in {
            "preparing",
            "running",
            "sealing",
            "publish_pending",
            "published",
        }:
            raise ContractError("Unknown run phase: " + phase)
        with self.guard:
            state = self._state(task_id)
            state.update(phase=phase, detail=detail or {})
            self._write(task_id, state)

    def event(self, task_id, kind, detail):
        with self.guard:
            try:
                state = self._state(task_id)
            except ContractError:
                return  # Invalid/unregistered remote input has no task state.
            state["events"] = (
                state.get("events", [])
                + [{"at": time.time(), "kind": kind, "detail": detail}]
            )[-100:]
            self._write(task_id, state)

    def queue_result(self, task_id, path, result_digest):
        with self.guard:
            state = self._state(task_id)
            saved = state.get("delivery")
            if saved and saved["digest"] != result_digest:
                raise ContractError("A sealed result cannot be rewritten")
            state["delivery"] = saved or {
                "payload_path": str(path),
                "digest": result_digest,
                "attempts": 0,
                "published": None,
            }
            state["phase"] = (
                "published" if state["delivery"]["published"] else "publish_pending"
            )
            self._write(task_id, state)

    def delivery(self, task_id):
        with self.guard:
            return self._state(task_id).get("delivery")

    @staticmethod
    def result_status(delivery):
        try:
            data = Path(delivery["payload_path"]).read_bytes()
            return (
                json.loads(data).get("status")
                if hashlib.sha256(data).hexdigest() == delivery["digest"]
                else None
            )
        except (OSError, ValueError, TypeError):
            return None

    def published(self, task_id):
        with self.guard:
            state = self._state(task_id)
            if not state.get("delivery"):
                raise ContractError("Cannot complete delivery without a sealed result")
            state["delivery"]["published"] = (
                state["delivery"].get("published") or time.time()
            )
            state.update(
                phase="published",
                detail={
                    "completion_boundary": "gitee_upload",
                    "result_status": self.result_status(state["delivery"]),
                },
            )
            self._write(task_id, state)

    def publication_failure(self, task_id):
        with self.guard:
            state = self._state(task_id)
            state["delivery"]["attempts"] += 1
            self._write(task_id, state)
            return state["delivery"]["attempts"]

    def restart(self, task_id):
        """Abandon an unsealed environment after Worker restart; never reuse its installs."""
        with self.guard:
            state = self._state(task_id)
            if state.get("delivery"):
                return self.task(task_id)
            state["abandoned"] = True
            state["detail"] = {"reason": "worker_restart", "verification": "incomplete"}
            self._write(task_id, state)
            return self._new(json.loads(self.task(task_id)["manifest"]))

    def resume(self, task_id):
        with self.guard:
            state = self._state(task_id)
            delivery = state.get("delivery")
            if delivery and delivery["published"] is None:
                self.phase(task_id, "publish_pending", {"reason": "retry_saved_upload"})
                return
            if delivery and self.result_status(delivery) != "infra_error":
                raise ContractError(
                    "Only infrastructure results may be explicitly rerun"
                )
            self._new(json.loads(self.task(task_id)["manifest"]))
