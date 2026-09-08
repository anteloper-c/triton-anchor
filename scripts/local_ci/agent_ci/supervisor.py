"""Task-scoped tool service and evidence-based final gate."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import secrets
import shutil
import socketserver
import threading
import uuid
from pathlib import Path

from .mcp_server import SCHEMAS, schema, validate_arguments, validate_value
from .policy import DEPENDENCIES, TOOLS
from .protocol import ContractError, RESULT_SCHEMA, atomic_json, within

ARCHITECTURE_RULES = {
    "abi-isolation": "python/triton_anchor/adapters/base.py",
    "anchor-ir-tracks": "python/triton_anchor/anchor_ir.py",
    "mandatory-pipeline": "python/triton_anchor/pipeline.py",
    "plugin-compatibility": "python/triton_anchor/extensions/base.py",
}


class Supervisor:
    def __init__(self, task: dict, policy: dict, journal, executor, run_dir: Path, *, changes: list[dict] | None = None):
        self.task, self.policy, self.journal, self.executor = task, policy, journal, executor
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.changes = changes or []
        self.cancelled = threading.Event()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.futures: dict[str, concurrent.futures.Future] = {}
        self.closed = False
        self.guard = threading.RLock()

    def recover(self) -> None:
        for record in self.journal.executions(self.task["task_id"]):
            if record["status"] in {"running", "queued"}:
                self.executor.stop(record["execution_id"])
                record.update(status="infra_error", reason="worker_interrupted")
                self.journal.execution(self.task["task_id"], record["tool_id"], record["variant"], record)

    def fresh(self, tool_id: str, variant: str = "candidate") -> bool:
        record = self.journal.latest(self.task["task_id"], tool_id, variant)
        if not record or record["status"] != "pass" or record.get("environment_fingerprint") != self.executor.generation["environment_fingerprint"]:
            return False
        if tool_id in (*TOOLS, "contract_tests") and (record.get("execution_kind") == "custom" or
                any(key in record for key in ("script_digest", "script_name", "source_only"))):
            return False
        for dependency in DEPENDENCIES.get(tool_id, ["environment"]):
            latest = self.journal.latest(self.task["task_id"], dependency, variant)
            if not self.fresh(dependency, variant) or record.get("dependency_executions", {}).get(dependency) != latest["execution_id"]:
                return False
        return True

    def context(self) -> dict:
        return {"task": self.task, "policy": self.policy, "changes": self.changes,
                "checks": [self.public_record(r) for r in self.journal.executions(self.task["task_id"])],
                "reviews": self.journal.reviews(self.task["task_id"]), "architecture_rules": ARCHITECTURE_RULES,
                "cancelled": self.cancelled.is_set(), "closed": self.closed}

    @staticmethod
    def public_record(record: dict) -> dict:
        return {k: v for k, v in record.items() if k != "artifact_dir"}

    def start_check(self, tool_id: str, reason: str, *, parameters: dict | None = None,
                    variant: str = "candidate", force: bool = False) -> dict:
        """Public built-in check entry; custom execution is never accepted here."""
        arguments = {"tool_id": tool_id, "reason": reason, "variant": variant, "force": force}
        if parameters is not None:
            arguments["parameters"] = parameters
        validate_arguments("start_check", arguments)
        return self._start_check(tool_id, reason, parameters=parameters, variant=variant, force=force)

    def _start_check(self, tool_id: str, reason: str, *, parameters: dict | None = None,
                     variant: str = "candidate", force: bool = False, custom: dict | None = None) -> dict:
        """Internal queue shared by built-ins and the validated run_custom path."""
        with self.guard:
            if self.closed or self.cancelled.is_set():
                raise ContractError("Task has finished or was cancelled")
            if not isinstance(reason, str) or not reason.strip():
                raise ContractError("Each check needs a selection reason")
            if variant not in {"candidate", "base"}:
                raise ContractError("Unknown variant")
            if custom is None and tool_id not in self.policy["capabilities"]:
                raise ContractError("Tool unavailable in this version environment")
            if custom is not None and tool_id != "custom_" + hashlib.sha256(custom["content"].encode()).hexdigest()[:16]:
                raise ContractError("Custom execution cannot use a built-in tool identity")
            config = getattr(self.executor, "config", {})
            parameters = parameters or {}
            if parameters.get("max_jobs", 1) > config.get("max_jobs", 8):
                raise ContractError("Parallelism exceeds the trusted budget")
            if parameters.get("timeout_seconds", 1) > config.get("tool_timeouts", {}).get(tool_id, 3600):
                raise ContractError("Tool deadline exceeds the trusted budget")
            previous = self.journal.latest(self.task["task_id"], tool_id, variant)
            if previous and (previous["status"] in {"running", "queued"} or self.fresh(tool_id, variant)) and not force and custom is None:
                return self.public_record(previous)
            dependencies = DEPENDENCIES.get(tool_id, ["environment"])
            for required in dependencies:
                result = self.journal.latest(self.task["task_id"], required, variant)
                if not result or not self.fresh(required, variant):
                    raise ContractError(f"Dependency {required} must pass first for {variant}")
            ident = uuid.uuid4().hex
            record = {"execution_id": ident, "status": "queued", "reason": reason,
                      "parameters": parameters, "execution_kind": "custom" if custom is not None else "builtin",
                      "required": tool_id in self.policy["required_checks"] and variant == "candidate"}
            self.journal.execution(self.task["task_id"], tool_id, variant, record)
            self.futures[ident] = self.pool.submit(self._run, tool_id, ident, variant, parameters or {}, reason, custom)
            return {**record, "tool_id": tool_id, "variant": variant}

    def _run(self, tool_id: str, ident: str, variant: str, parameters: dict, reason: str, custom: dict | None):
        dependency_names = DEPENDENCIES.get(tool_id, ["environment"])
        if custom and not custom.get("source_only"):
            dependency_names = ["backend_smoke_jit" if self.executor.generation["backend_enabled"] else "frontend_smoke"]
        dependencies = {key: self.journal.latest(self.task["task_id"], key, variant)["execution_id"] for key in dependency_names}
        self.journal.execution(self.task["task_id"], tool_id, variant, {"execution_id": ident, "status": "running", "reason": reason})
        result = self._invoke(tool_id, ident, variant, parameters, custom)
        result["selection_reason"] = reason
        result["dependency_executions"] = dependencies
        result["execution_kind"] = "custom" if custom is not None else "builtin"
        if custom:
            result["source_only"] = custom.get("source_only", False)
        result["required"] = tool_id in self.policy["required_checks"] and variant == "candidate"
        self.journal.execution(self.task["task_id"], tool_id, variant, result)
        if result.get("reason") == "oom" and not self.cancelled.is_set():
            retry = uuid.uuid4().hex
            params = {**parameters, "max_jobs": max(1, int(parameters.get("max_jobs", 8)) // 2)}
            self.journal.event(self.task["task_id"], "oom_retry", {"previous_execution_id": ident, "execution_id": retry, "parameters": params})
            self.journal.execution(self.task["task_id"], tool_id, variant, {"execution_id": retry, "status": "running", "retry_of": ident})
            result = self._invoke(tool_id, retry, variant, params, custom)
            result.update(retry_of=ident, selection_reason=reason, dependency_executions=dependencies, required=tool_id in self.policy["required_checks"] and variant == "candidate")
            result["execution_kind"] = "custom" if custom is not None else "builtin"
            if custom:
                result["source_only"] = custom.get("source_only", False)
            self.journal.execution(self.task["task_id"], tool_id, variant, result)
        return result

    def _invoke(self, tool_id, ident, variant, parameters, custom):
        try:
            return self.executor.run(tool_id, ident, variant, parameters, self.cancelled, custom)
        except Exception as exc:
            # Directory/process setup can fail before an executor creates its log.
            # Persist a terminal fact instead of leaving a completed future "running".
            return {"execution_id": ident, "tool_id": tool_id, "variant": variant,
                    "status": "infra_error", "exit_code": None, "reason": str(exc),
                    "environment_fingerprint": self.executor.generation["environment_fingerprint"],
                    "parameters": parameters}

    def poll_check(self, execution_id: str, wait_seconds: int = 0) -> dict:
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 30:
            raise ContractError("Poll waits must be between 0 and 30 seconds")
        future = self.futures.get(execution_id)
        if future:
            try:
                return self.public_record(future.result(timeout=wait_seconds))
            except concurrent.futures.TimeoutError:
                pass
        for record in self.journal.executions(self.task["task_id"]):
            if record["execution_id"] == execution_id:
                return self.public_record(record)
        raise ContractError("Execution does not belong to this task")

    def read_file(self, path: str, variant: str = "candidate", start_line: int = 1, max_lines: int = 200) -> dict:
        if type(start_line) is not int or type(max_lines) is not int or start_line < 1 or not 1 <= max_lines <= 500:
            raise ContractError("Invalid line range")
        checkout = self.executor.prepare(variant)
        file = within(checkout, path, must_exist=True)
        if ".git" in Path(path).parts or file.stat().st_size > 2 * 1024 * 1024:
            raise ContractError("File cannot be read through this interface")
        lines = file.read_text(errors="replace").splitlines()
        return {"path": path, "variant": variant, "start_line": start_line,
                "content": "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines) if start_line - 1 <= i < start_line - 1 + max_lines)}

    def read_artifact(self, execution_id: str, path: str = "execution.log", offset: int = 0) -> dict:
        for record in self.journal.executions(self.task["task_id"]):
            if record["execution_id"] == execution_id and record.get("artifact_dir"):
                artifact = within(Path(record["artifact_dir"]), path, must_exist=True)
                if type(offset) is not int or offset < 0:
                    raise ContractError("Invalid artifact offset")
                with artifact.open("rb") as stream:
                    stream.seek(offset)
                    data = stream.read(32768)
                return {"execution_id": execution_id, "path": path, "offset": offset,
                        "next_offset": offset + len(data), "content": data.decode(errors="replace")}
        raise ContractError("Unknown artifact")

    def run_custom(self, name: str, content: str, language: str, reason: str, variant: str = "candidate", source_only: bool = False) -> dict:
        validate_arguments("run_custom", {"name": name, "content": content, "language": language,
                                          "reason": reason, "variant": variant, "source_only": source_only})
        if language not in {"python", "bash"} or not isinstance(content, str) or not 1 <= len(content.encode()) <= 128 * 1024:
            raise ContractError("Invalid task-local script")
        if Path(name).name != name or not name.endswith(".py" if language == "python" else ".sh"):
            raise ContractError("Generated scripts require a plain filename and matching extension")
        if type(source_only) is not bool or (source_only and language != "python"):
            raise ContractError("Source-only reproduction must be Python without site packages")
        if not source_only:
            required = "backend_smoke_jit" if self.executor.generation["backend_enabled"] else "frontend_smoke"
            if not self.fresh(required, variant):
                raise ContractError(f"Runtime reproduction requires a verified {variant} installation: {required}")
        key = "custom_" + hashlib.sha256(content.encode()).hexdigest()[:16]
        return self._start_check(key, reason, variant=variant, force=True,
                                 custom={"name": name, "content": content, "language": language, "source_only": source_only})

    def submit_review(self, kind: str, status: str, summary: str, evidence: list, findings: list | None = None) -> dict:
        if self.closed or self.cancelled.is_set():
            raise ContractError("Task closed")
        if kind not in {"pr_info", "architecture", "specialized"} or status not in {"pass", "fail", "incomplete"}:
            raise ContractError("Invalid review")
        if not isinstance(summary, str) or not summary.strip() or not isinstance(evidence, list):
            raise ContractError("Review needs a summary and evidence list")
        if kind == "architecture" and status == "pass" and not evidence:
            raise ContractError("Architecture review requires explicit contract references, including no-impact reviews")
        for item in evidence:
            if not isinstance(item, dict):
                raise ContractError("Invalid review evidence")
            if item.get("rule_id") and item["rule_id"] not in ARCHITECTURE_RULES:
                raise ContractError("Unknown architecture rule")
            if item.get("path"):
                self.read_file(item["path"], item.get("variant", "candidate"), item.get("line", 1), 1)
        record = {"status": status, "summary": summary, "evidence": evidence, "findings": findings or []}
        self.journal.review(self.task["task_id"], kind, record)
        return record

    def finding_blocker(self, finding: dict) -> bool:
        if not isinstance(finding, dict) or not finding.get("summary") or not finding.get("path"):
            return False
        try:
            self.read_file(finding["path"], "candidate", finding.get("line", 1), 1)
        except (ContractError, OSError):
            return False
        changed = {item["path"] for item in self.changes} | {item.get("old_path") for item in self.changes}
        if finding["path"] not in changed:
            return False
        if finding.get("category") == "architecture":
            rule_path = ARCHITECTURE_RULES.get(finding.get("rule_id"))
            proof = finding.get("violation_evidence")
            if not rule_path or not isinstance(proof, dict):
                return False
            try:
                contract = self.read_file(rule_path, "base", proof.get("rule_line", 1), 30)["content"]
                candidate = self.read_file(finding["path"], "candidate", finding.get("line", 1), 30)["content"]
                return (isinstance(proof.get("rule_quote"), str) and len(proof["rule_quote"].strip()) >= 8
                        and proof["rule_quote"] in contract and isinstance(proof.get("code_quote"), str)
                        and len(proof["code_quote"].strip()) >= 4 and proof["code_quote"] in candidate
                        and bool(proof.get("explanation")))
            except (ContractError, OSError):
                return False
        if finding.get("severity", "").lower() not in {"high", "critical"}:
            return False
        ids = finding.get("execution_ids", [])
        selected = [r for r in self.journal.executions(self.task["task_id"]) if r["execution_id"] in ids]
        failed = [r for r in selected if r.get("variant") == "candidate" and r["status"] == "fail" and r.get("script_digest")]
        base = [r for r in selected if r.get("variant") == "base" and r["status"] == "pass" and r.get("script_digest")]
        # Two candidate failures, a passing base, identical script and environment.
        return any(sum(r["script_digest"] == b["script_digest"] and r["environment_fingerprint"] == b["environment_fingerprint"]
                       and r.get("source_only", False) == b.get("source_only", False)
                       for r in failed) >= 2 for b in base)

    def finish(self, summary: str = "") -> dict:
        with self.guard:
            if self.closed:
                return json.loads((self.run_dir / "published/result.json").read_text())
            if any(not future.done() for future in self.futures.values()):
                raise ContractError("Wait for running checks before sealing results")
            checks, unfinished, blockers, performance = [], [], [], []
            for tool_id in self.policy["required_checks"]:
                record = self.journal.latest(self.task["task_id"], tool_id)
                if not record or not self.fresh(tool_id):
                    unfinished.append(tool_id)
                    if not record:
                        checks.append({"tool_id": tool_id, "variant": "candidate", "status": "blocked_dependency",
                                       "required": True, "reason": "Required check has no execution evidence; inspect prerequisites and resume"})
                    if record and record["status"] == "fail":
                        blockers.append({"kind": "check_failure", "tool_id": tool_id, "execution_id": record["execution_id"], "reason": record.get("reason", "failed")})
            records = self.journal.executions(self.task["task_id"])
            latest = {(r["tool_id"], r["variant"]): r for r in records}
            for (tool_id, variant), record in latest.items():
                public = self.public_record(record)
                public["required"] = variant == "candidate" and tool_id in self.policy["required_checks"]
                checks.append(public)
                if variant == "candidate" and tool_id in {"compile_time", "pass_profile", "ir_serialization"}:
                    performance.append({"tool_id": tool_id, "execution_id": record["execution_id"], "details": record.get("details", {})})
                if variant == "candidate" and tool_id in TOOLS and record["status"] == "fail" and not public["required"]:
                    blockers.append({"kind": "selected_check_failure", "tool_id": tool_id, "execution_id": record["execution_id"]})
                if variant == "candidate" and tool_id in (*TOOLS, "contract_tests") and record["status"] in {"infra_error", "cancelled", "running", "queued"} and tool_id not in unfinished:
                    unfinished.append(tool_id)
            for tool_id in self.policy["not_applicable"]:
                checks.append({"tool_id": tool_id, "status": "not_applicable", "required": False, "reason": "backend_capability_unavailable"})
            for tool_id in self.policy["capabilities"]:
                if (tool_id, "candidate") not in latest and tool_id not in self.policy["required_checks"]:
                    checks.append({"tool_id": tool_id, "status": "not_selected", "required": False, "reason": "Not required by impact policy and not added by Codex"})
            reviews = self.journal.reviews(self.task["task_id"])
            findings = []
            for kind in self.policy["required_reviews"]:
                if kind not in reviews or reviews[kind]["status"] != "pass":
                    unfinished.append("review:" + kind)
                if kind == "pr_info" and reviews.get(kind, {}).get("status") == "fail":
                    blockers.append({"kind": "pr_information", "reason": reviews[kind]["summary"]})
            for review in reviews.values():
                for item in review.get("findings", []):
                    blocking = self.finding_blocker(item)
                    findings.append({**item, "blocking": blocking})
                    if blocking:
                        blockers.append({"kind": item.get("category", "verified_high_risk"), "finding": item})
            status = "cancelled" if self.cancelled.is_set() else "fail" if blockers else "infra_error" if unfinished else "pass"
            run_id = self.journal.task(self.task["task_id"])["run_id"]
            result = {"schema": RESULT_SCHEMA, "task": self.task, "run_id": run_id,
                      "status": status, "summary": summary, "policy_version": self.policy["version"],
                      "required_checks": self.policy["required_checks"], "checks": checks,
                      "policy": self.policy, "changes": self.changes,
                      "reviews": reviews, "findings": findings, "blockers": blockers,
                      "performance": performance, "unfinished": unfinished,
                      "environment": {k: self.executor.generation[k] for k in ("profile", "generation", "environment_fingerprint", "backend_enabled")},
                      "publication": {"status": "pending_upload", "completion_requires": ["gitee_upload"]}}
            published = self.run_dir / "published"
            published.mkdir(exist_ok=True)
            for record in records:
                source = Path(record.get("artifact_dir", ""))
                if not record.get("artifact_dir") or not source.is_dir():
                    continue
                target = published / "evidence" / record["execution_id"]
                for file in source.rglob("*"):
                    if file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(source.resolve()):
                        dest = target / file.relative_to(source)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(file, dest)
            atomic_json(published / "result.json", result)
            self.journal.queue_result(self.task["task_id"], published / "result.json", hashlib.sha256((published / "result.json").read_bytes()).hexdigest())
            self.closed = True
            return result

    def cancel(self, reason: str) -> None:
        self.cancelled.set()
        self.journal.event(self.task["task_id"], "cancel_requested", {"reason": reason})
        for record in self.journal.executions(self.task["task_id"]):
            if record["status"] == "running":
                self.executor.stop(record["execution_id"])

    def close(self):
        self.pool.shutdown(wait=True)


class ToolService:
    """A bearer-scoped local socket; the MCP process has no journal write access."""
    METHODS = frozenset(SCHEMAS)

    def __init__(self, supervisor: Supervisor, socket_path: Path):
        self.supervisor = supervisor
        self.path, self.token = socket_path, secrets.token_hex(32)
        if not socket_path.is_absolute() or socket_path.parent.resolve() != socket_path.parent:
            raise ContractError("RPC socket must use an absolute trusted directory without symlinks")
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.parent.stat().st_uid != os.geteuid():
            raise ContractError("RPC socket directory must belong to the worker account")
        socket_path.parent.chmod(0o711)
        if socket_path.exists():
            socket_path.unlink()
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    raw = self.rfile.readline(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise ContractError("Request too large")
                    request = json.loads(raw)
                    validate_value(request, schema({"token": {"type": "string"}, "method": {"type": "string"},
                                                    "arguments": {"type": "object"}}, ["token", "method", "arguments"]), "RPC request")
                    if not secrets.compare_digest(str(request.get("token", "")), service.token):
                        raise ContractError("Invalid task capability")
                    method = request.get("method")
                    if method not in ToolService.METHODS:
                        raise ContractError("Unknown task method")
                    arguments = request["arguments"]
                    validate_arguments(method, arguments)
                    result = getattr(service.supervisor, method)(**arguments)
                    response = {"result": result}
                except Exception as exc:
                    response = {"error": str(exc)}
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")

        self.server = socketserver.ThreadingUnixStreamServer(str(socket_path), Handler)
        self.server.daemon_threads = True
        socket_path.chmod(0o666)  # Authentication is the per-task unguessable token, not ambient uid.
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.path.unlink(missing_ok=True)
