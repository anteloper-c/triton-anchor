"""Task scheduling and coverage over a single runner and observed execution log."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import secrets
import shutil
import socketserver
import threading
import time
import uuid
from pathlib import Path

from .mcp_server import SCHEMAS, schema, validate_arguments, validate_value
from .policy import TOOLS
from .protocol import ContractError, RESULT_SCHEMA, atomic_json, within, scope_covers
from .delivery import seal_artifacts, EXECUTIONS_SCHEMA
from tools.basic_tools.runner import (
    dependencies as tool_dependencies,
    parameter_names,
    DEFAULT_BUILD_JOBS,
    plan,
)
from tools.basic_tools.evidence import evaluate

ARCHITECTURE_RULES = {
    "abi-isolation": "python/triton_anchor/adapters/base.py",
    "anchor-ir-tracks": "python/triton_anchor/anchor_ir.py",
    "mandatory-pipeline": "python/triton_anchor/pipeline.py",
    "plugin-compatibility": "python/triton_anchor/extensions/base.py",
}


class Supervisor:
    def __init__(
        self,
        task,
        policy,
        journal,
        executor,
        run_dir,
        *,
        changes=None,
        before_seal=None,
    ):
        self.task, self.policy, self.journal, self.executor = (
            task,
            policy,
            journal,
            executor,
        )
        self.executor.journal = journal
        self.redact = lambda value: value
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.changes = changes or []
        self.cancelled = threading.Event()
        self.control_available = threading.Event()
        self.control_available.set()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.futures = {}
        self.guard = threading.RLock()
        self.closed = False
        self.sealing_started = False
        self.before_seal = before_seal
        self.native = {}

    def recover(self):
        # Only an Agent restart in the same live Worker may reuse this run.
        for r in self.journal.executions(self.task["task_id"]):
            if r["status"] in {"running", "queued"}:
                self.executor.stop(r["execution_id"])
                r.update(status="infra_error", reason="interrupted_execution")
                self.journal.execution(
                    self.task["task_id"], r["tool_id"], r["variant"], r
                )

    def dependencies(self, tool_id, custom=None):
        if custom:
            return []
        config = (
            self.executor.config.get("profiles", {})
            .get(
                self.executor.generation.get(
                    "profile_branch", self.task.get("target_branch")
                ),
                {},
            )
            .get("tools", {})
        )
        return tool_dependencies(tool_id, config)

    def fresh(self, tool_id, variant="candidate", seen=None):
        seen = set(seen or ())
        if tool_id in seen:
            return False
        seen.add(tool_id)
        r = self.journal.latest(self.task["task_id"], tool_id, variant)
        if (
            not r
            or r["status"] != "pass"
            or r.get("reuse_invalidated")
            or not r.get("original_subject", False)
        ):
            return False
        if (
            r.get("environment_fingerprint")
            != self.executor.generation["environment_fingerprint"]
        ):
            return False
        if r.get("workspace_generation") != self.executor.generation["generation"]:
            return False
        for dep in self.dependencies(tool_id):
            latest = self.journal.latest(self.task["task_id"], dep, variant)
            if (
                not self.fresh(dep, variant, seen)
                or r.get("dependency_executions", {}).get(dep) != latest["execution_id"]
            ):
                return False
        required = (
            self.policy.get("required_parameters", {}).get(tool_id, {})
            if variant == "candidate"
            else {}
        )
        if not scope_covers(r.get("scope", {}), required):
            return False
        # Changes after verification invalidate current combination, but remain history.
        if tool_id in {
            "frontend_tests",
            "frontend_smoke",
            "backend_tests",
            "backend_smoke",
            "flaggems",
            "compile_time",
            "pass_profile",
            "ir_serialization",
        } and hasattr(self.executor, "current_identity"):
            current = self.executor.current_identity(variant)
            if not current.get("original") or r.get(
                "installation_identity"
            ) != current.get("installation_identity"):
                return False
        return True

    @staticmethod
    def public_record(record):
        return {
            k: v
            for k, v in record.items()
            if k not in {"artifact_dir", "log_path", "private_event_log"}
        }

    def context(self):
        return {
            "task": self.task,
            "policy": self.policy,
            "changes": self.changes,
            "checks": [
                self.public_record(r)
                for r in self.journal.executions(self.task["task_id"])
            ],
            "native_executions": [self.public_record(r) for r in self.native.values()],
            "reviews": self.journal.reviews(self.task["task_id"]),
            "architecture_rules": ARCHITECTURE_RULES,
            "tools": list(TOOLS),
            "diagnostics": self.executor.diagnostic_context(),
            "cancelled": self.cancelled.is_set(),
            "control_channel_available": self.control_available.is_set(),
            "closed": self.closed,
        }

    def start_check(
        self, tool_id, reason, *, parameters=None, variant="candidate", force=False
    ):
        validate_arguments(
            "start_check",
            {
                "tool_id": tool_id,
                "reason": reason,
                "parameters": parameters or {},
                "variant": variant,
                "force": force,
            },
        )
        parameters = {
            **self.policy.get("required_parameters", {}).get(tool_id, {}),
            **(parameters or {}),
        }
        # Runner owns validation and parameter catalogue; MCP has no duplicate copy.
        plan(
            tool_id,
            self.executor.plan_context("0" * 32, variant, parameters),
            parameters,
        )
        return self._start(tool_id, reason, parameters, variant, force)

    def _start(self, tool_id, reason, parameters, variant, force=False, custom=None):
        with self.guard:
            if self.closed or self.sealing_started or self.cancelled.is_set():
                raise ContractError("Task has ended")
            if not self.control_available.is_set():
                raise ContractError("Gitee current state unavailable; pause new stages")
            if not custom and tool_id not in self.policy["capabilities"]:
                raise ContractError("Capability not deployed in this profile")
            previous = self.journal.latest(self.task["task_id"], tool_id, variant)
            if previous and not force and previous.get("parameters", {}) == parameters:
                if previous["status"] in {"queued", "running"} or self.fresh(
                    tool_id, variant
                ):
                    return self.public_record(previous)
            for dep in self.dependencies(tool_id, custom):
                if not self.fresh(dep, variant):
                    raise ContractError("Dependency " + dep + " must pass first")
            ident = uuid.uuid4().hex
            record = {
                "execution_id": ident,
                "status": "queued",
                "reason": reason,
                "parameters": parameters,
                "execution_kind": "custom" if custom else "builtin",
                "tool_id": tool_id,
                "variant": variant,
            }
            self.journal.execution(self.task["task_id"], tool_id, variant, record)
            self.futures[ident] = self.pool.submit(
                self._run, tool_id, ident, variant, parameters, reason, custom
            )
            return record

    def _run(self, tool_id, ident, variant, parameters, reason, custom):
        deps = self.dependencies(tool_id, custom)
        with self.guard:
            valid = all(self.fresh(dep, variant) for dep in deps)
            dependency_executions = {
                dep: self.journal.latest(self.task["task_id"], dep, variant)[
                    "execution_id"
                ]
                for dep in deps
            }
        if not valid or not self.control_available.is_set():
            result = {
                "execution_id": ident,
                "status": "infra_error",
                "reason": "Dependencies or Gitee validity changed while queued",
            }
        else:
            self.journal.execution(
                self.task["task_id"],
                tool_id,
                variant,
                {"execution_id": ident, "status": "running", "parameters": parameters},
            )
            try:
                result = self.executor.run(
                    tool_id, ident, variant, parameters, self.cancelled, custom
                )
            except Exception as exc:
                result = {
                    "execution_id": ident,
                    "status": "infra_error",
                    "reason": str(exc),
                    "exit_code": None,
                }
        result.update(
            selection_reason=reason,
            parameters=parameters,
            dependency_executions=dependency_executions,
            execution_kind="custom" if custom else "builtin",
            required=variant == "candidate"
            and tool_id in self.policy["required_checks"],
        )
        self.journal.execution(self.task["task_id"], tool_id, variant, result)
        if (
            result.get("reason") == "oom"
            and not custom
            and "jobs" in parameter_names(tool_id)
            and not self.cancelled.is_set()
            and self.control_available.is_set()
        ):
            retry = uuid.uuid4().hex
            retry_params = {
                **parameters,
                "jobs": max(
                    1,
                    parameters.get("jobs", DEFAULT_BUILD_JOBS) // 2,
                ),
            }
            rerun = self.executor.run(
                tool_id, retry, variant, retry_params, self.cancelled, custom
            )
            rerun.update(
                retry_of=ident,
                parameters=retry_params,
                dependency_executions=dependency_executions,
                execution_kind=result["execution_kind"],
                required=result["required"],
                selection_reason="Retry once with fewer build jobs after OOM",
            )
            self.journal.execution(self.task["task_id"], tool_id, variant, rerun)
            result = rerun
        return result

    def poll_check(self, execution_id, wait_seconds=0):
        future = self.futures.get(execution_id)
        if future:
            try:
                return self.public_record(future.result(timeout=min(30, wait_seconds)))
            except concurrent.futures.TimeoutError:
                pass
        for r in self.journal.executions(self.task["task_id"]):
            if r["execution_id"] == execution_id:
                return self.public_record(r)
        raise ContractError("Execution does not belong to this run")

    def cancel_check(self, execution_id):
        self.poll_check(execution_id)
        return self.executor.stop(execution_id)

    def read_file(self, path, variant="candidate", start_line=1, max_lines=200):
        file = within(self.executor.prepare(variant), path, must_exist=True)
        if ".git" in Path(path).parts or file.stat().st_size > 2 * 1024**2:
            raise ContractError("File too large or private metadata")
        lines = file.read_text(errors="replace").splitlines()
        return {
            "path": path,
            "variant": variant,
            "start_line": start_line,
            "content": "\n".join(
                f"{i + 1}: {line}"
                for i, line in enumerate(lines)
                if start_line - 1 <= i < start_line - 1 + max_lines
            ),
        }

    def read_artifact(self, execution_id, path="command.log", offset=0):
        for r in self.journal.executions(self.task["task_id"]):
            if r["execution_id"] != execution_id:
                continue
            target = (
                Path(r["log_path"])
                if path == "command.log"
                else within(Path(r["artifact_dir"]), path, must_exist=True)
            )
            with target.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(32768)
            return {
                "content": data.decode(errors="replace"),
                "next_offset": offset + len(data),
            }
        raise ContractError("Unknown execution artifact")

    def run_custom(
        self,
        name,
        content,
        language,
        reason,
        variant="candidate",
        source_only=False,
        mode="diagnostic",
        experiment_id=None,
    ):
        args = dict(
            name=name,
            content=content,
            language=language,
            reason=reason,
            variant=variant,
            source_only=source_only,
            mode=mode,
        )
        if experiment_id:
            args["experiment_id"] = experiment_id
        validate_arguments("run_custom", args)
        if Path(name).name != name or not name.endswith(
            ".py" if language == "python" else ".sh"
        ):
            raise ContractError("Use a plain script filename")
        custom = {k: v for k, v in args.items() if k not in {"reason", "variant"}}
        return self._start(
            "custom_" + hashlib.sha256(content.encode()).hexdigest()[:16],
            reason,
            {},
            variant,
            True,
            custom,
        )

    @staticmethod
    def _artifact_signature(path):
        info = path.stat()
        return [info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino]

    def _artifact_signatures(self, directory=None):
        root = self.run_dir / "artifacts"
        directory = Path(directory or root)
        values = {}
        for path in directory.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                values[path.relative_to(root).as_posix()] = self._artifact_signature(
                    path
                )
            except FileNotFoundError:
                continue
        return values

    def _changed_artifacts(self, before, directory=None):
        root = self.run_dir / "artifacts"
        result = {}
        for name, signature in self._artifact_signatures(directory).items():
            if before.get(name) == signature:
                continue
            path = root / name
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if self._artifact_signature(path) != signature:
                    continue
            except FileNotFoundError:
                continue
            result[name] = {
                "signature": signature,
                "sha256": digest,
                "size": signature[0],
            }
        return result

    def observe_native(self, event):
        item = event.get("item", {}) if isinstance(event, dict) else {}
        if item.get("type") != "command_execution" or not item.get("id"):
            return
        ident = hashlib.sha256(
            (str(self.run_dir) + str(item["id"])).encode()
        ).hexdigest()[:32]
        with self.guard:
            if self.closed or self.sealing_started:
                return
            if not hasattr(self, "_native_event_ids"):
                self._native_event_ids = {}
            ident = self._native_event_ids.get(item["id"], ident)
            record = self.native.get(ident)
            if (
                event.get("type") == "item.started"
                and record is not None
                and record["status"] != "running"
            ):
                ident = uuid.uuid4().hex
                record = None
            if event.get("type") == "item.started":
                self._native_event_ids[item["id"]] = ident
            if event.get("type") == "item.started" and record is None:
                identity = self.executor.current_identity("candidate")
                record = {
                    "execution_id": ident,
                    "tool_id": "native_command",
                    "variant": "candidate",
                    "execution_kind": "native",
                    "status": "running",
                    "started_at": time.time(),
                    "command": item.get("command", ""),
                    "cwd": "/task/candidate/checkout",
                    "subject_before": identity,
                    "environment_fingerprint": self.executor.generation[
                        "environment_fingerprint"
                    ],
                    "workspace_generation": self.executor.generation["generation"],
                }
                if not hasattr(self, "_native_artifacts_before"):
                    self._native_artifacts_before = {}
                self._native_artifacts_before[ident] = self._artifact_signatures()
                self.native[ident] = record
            if event.get("type") == "item.completed":
                if record is None or record["status"] != "running":
                    return  # Ignore duplicate completion or missing launch.
                code = item.get("exit_code")
                if type(code) is not int:
                    return
                log = self.run_dir / "logs" / (ident + ".log")
                log.write_text(item.get("aggregated_output", ""), encoding="utf-8")
                identity = self.executor.current_identity("candidate")
                before = self._native_artifacts_before.pop(ident, {})
                record.update(
                    exit_code=code,
                    finished_at=time.time(),
                    status="pass" if code == 0 else "fail",
                    log_path=str(log),
                    subject=identity,
                    installation_identity=identity["installation_identity"],
                    original_subject=identity["original"]
                    and record["subject_before"]["original"],
                    tested_sha=self.task["tested_sha"],
                    observed_artifacts=self._changed_artifacts(before),
                )
                self.native[ident] = record
                self.journal.execution(
                    self.task["task_id"], "native_command", "candidate", record
                )

    def interrupt_native(self, reason):
        """Close incomplete observations after the driver has stopped its commands."""
        with self.guard:
            for ident, record in self.native.items():
                if record["status"] != "running":
                    continue
                log = self.run_dir / "logs" / (ident + ".log")
                log.write_text(
                    "Native execution interrupted: " + reason + "\n", encoding="utf-8"
                )
                record.update(
                    status="cancelled" if "cancel" in reason.lower() else "infra_error",
                    finished_at=time.time(),
                    exit_code=None,
                    reason=reason,
                    log_path=str(log),
                    original_subject=False,
                    observed_artifacts={},
                )
                getattr(self, "_native_artifacts_before", {}).pop(ident, None)
                self.journal.execution(
                    self.task["task_id"], "native_command", "candidate", record
                )

    def record_check(
        self,
        execution_id,
        tool_id,
        artifact_path,
        reason,
        parameters=None,
        reports=None,
    ):
        with self.guard:
            if self.closed or self.sealing_started:
                raise ContractError("Task is sealed")
            original = next(
                (
                    r
                    for r in self.journal.executions(self.task["task_id"])
                    if r["execution_id"] == execution_id
                ),
                None,
            )
            if (
                not original
                or original.get("execution_kind") not in {"native", "custom"}
                or type(original.get("exit_code")) is not int
            ):
                raise ContractError("Link a completed observed native/custom execution")
            if tool_id not in self.policy["capabilities"]:
                raise ContractError("Unsupported capability")
            prefix = "/task/artifacts/"
            if not artifact_path.startswith(prefix):
                raise ContractError("Reports must be in task artifacts")
            directory = within(self.run_dir / "artifacts", artifact_path[len(prefix) :])
            if not directory.is_dir():
                raise ContractError("Expected report directory")
            parameters = parameters or {}
            context = self.executor.plan_context(
                execution_id, original["variant"], parameters
            )
            outcome = evaluate(
                tool_id,
                directory,
                original["exit_code"],
                context,
                {**parameters, "reports": reports or {}},
            )
            observed = original.get("observed_artifacts", {})
            if original.get("execution_kind") == "custom" and not observed:
                # The executor creates an empty per-execution directory and stops
                # its process group before export. Only that bounded output can
                # supply custom evidence; late files cannot gain credit.
                owned = Path(original.get("artifact_dir", "")).resolve()
                if not directory.resolve().is_relative_to(owned):
                    raise ContractError(
                        "Custom reports must belong to their execution output"
                    )
                observed = self._changed_artifacts({}, directory)
                lower = int(original.get("started_at", 0) * 1_000_000_000)
                upper = int(original.get("finished_at", 0) * 1_000_000_000)
                observed = {
                    name: value
                    for name, value in observed.items()
                    if lower <= value["signature"][1] <= upper
                }
            expected = {}
            for relative in outcome.get("evidence_files", []):
                file = within(directory, relative, must_exist=True)
                key = file.relative_to(self.run_dir / "artifacts").as_posix()
                proof = observed.get(key)
                if not proof or self._artifact_signature(file) != proof["signature"]:
                    raise ContractError(
                        "Report was not produced by the observed execution or changed afterward: "
                        + relative
                    )
                expected[relative] = proof
            deps = {}
            for dep in self.dependencies(tool_id):
                if not self.fresh(dep, original["variant"]):
                    raise ContractError("Dependency " + dep + " lacks current evidence")
                deps[dep] = self.journal.latest(
                    self.task["task_id"], dep, original["variant"]
                )["execution_id"]
            if (
                tool_id in {"frontend_install", "backend_install"}
                and outcome["status"] == "pass"
            ):
                build_tool = (
                    "frontend_build"
                    if tool_id == "frontend_install"
                    else "backend_build"
                )
                build = self.journal.latest(
                    self.task["task_id"], build_tool, original["variant"]
                )
                if outcome["details"]["installation"]["sha256"] != build.get(
                    "details", {}
                ).get("wheel", {}).get("sha256"):
                    raise ContractError(
                        "Native installation does not match its current build wheel"
                    )
            association_id = uuid.uuid4().hex
            snapshot = self.run_dir / "artifacts" / association_id / tool_id
            for file in directory.rglob("*"):
                if file.is_symlink():
                    raise ContractError(
                        "Native report snapshot cannot contain symlinks"
                    )
            shutil.copytree(directory, snapshot)
            # Evaluate the fixed copy and verify the exact report contents that
            # were observed at command completion, including the wheel bytes.
            for relative, proof in expected.items():
                file = snapshot / relative
                if (
                    file.stat().st_size != proof["size"]
                    or hashlib.sha256(file.read_bytes()).hexdigest() != proof["sha256"]
                ):
                    raise ContractError(
                        "Report content changed after observed completion: " + relative
                    )
            outcome = evaluate(
                tool_id,
                snapshot,
                original["exit_code"],
                context,
                {**parameters, "reports": reports or {}},
            )
            if set(outcome.get("evidence_files", [])) - set(expected):
                raise ContractError(
                    "Report set changed while snapshotting observed evidence"
                )
            if (
                tool_id in {"frontend_build", "backend_build"}
                and outcome["status"] == "pass"
            ):
                wheel_manifest = snapshot / (reports or {}).get("wheel", "wheel.json")
                value = json.loads(wheel_manifest.read_text())
                relative = (reports or {}).get(
                    "wheel_file", "wheels/" + Path(value["wheel"]).name
                )
                value["wheel"] = (
                    f"/task/artifacts/{association_id}/{tool_id}/" + relative
                )
                atomic_json(snapshot / "wheel.json", value)
                outcome.setdefault("details", {})["wheel"] = value
            if outcome["status"] == "pass":
                # Native report names are selectable, while later A stages read
                # the same canonical manifests as builtin executions.
                for name in ("environment", "installation", "backend_discovery"):
                    value = outcome.get("details", {}).get(name)
                    if value is not None:
                        atomic_json(snapshot / (name + ".json"), value)
            record = {
                **original,
                **outcome,
                "execution_id": association_id,
                "source_execution_id": original.get(
                    "source_execution_id", execution_id
                ),
                "record_type": "check_association",
                "tool_id": tool_id,
                "artifact_dir": str(snapshot),
                "parameters": parameters,
                "dependency_executions": deps,
                "selection_reason": reason,
                "subject": original.get("subject"),
                "original_subject": original.get("original_subject", False),
            }
            self.journal.execution(
                self.task["task_id"], tool_id, original["variant"], record
            )
            return self.public_record(record)

    def submit_review(self, kind, status, summary, evidence, findings=None):
        if self.closed or self.sealing_started:
            raise ContractError("Task is sealed")
        validate_arguments(
            "submit_review",
            dict(
                kind=kind,
                status=status,
                summary=summary,
                evidence=evidence,
                findings=findings or [],
            ),
        )
        if kind == "architecture" and status == "pass" and not evidence:
            raise ContractError("Architecture review requires contract evidence")
        for item in evidence:
            if item.get("path"):
                self.read_file(
                    item["path"],
                    item.get("variant", "candidate"),
                    item.get("line", 1),
                    1,
                )
        record = {
            "status": status,
            "summary": summary,
            "evidence": evidence,
            "findings": findings or [],
        }
        self.journal.review(self.task["task_id"], kind, record)
        return record

    def finding_blocker(self, finding):
        if (
            not isinstance(finding, dict)
            or not finding.get("summary")
            or not finding.get("path")
        ):
            return False
        changed = {c["path"] for c in self.changes} | {
            c.get("old_path") for c in self.changes
        }
        if finding["path"] not in changed:
            return False
        try:
            candidate = self.read_file(
                finding["path"], "candidate", finding.get("line", 1), 30
            )["content"]
            if finding.get("category") == "architecture":
                proof = finding.get("violation_evidence", {})
                rule_path = ARCHITECTURE_RULES.get(finding.get("rule_id"))
                if not rule_path:
                    return False
                rule = self.read_file(rule_path, "base", proof.get("rule_line", 1), 30)[
                    "content"
                ]
                return (
                    bool(proof.get("explanation"))
                    and bool(proof.get("rule_quote"))
                    and proof["rule_quote"] in rule
                    and bool(proof.get("code_quote"))
                    and proof["code_quote"] in candidate
                )
            if finding.get("severity", "").lower() not in {"high", "critical"}:
                return False
            records = [
                r
                for r in self.journal.executions(self.task["task_id"])
                if r["execution_id"] in finding.get("execution_ids", [])
            ]
            failed = [
                r
                for r in records
                if r["variant"] == "candidate"
                and r["status"] == "fail"
                and r.get("original_subject")
            ]
            base = [
                r for r in records if r["variant"] == "base" and r["status"] == "pass"
            ]
            if any(
                c.get("script_digest")
                and c["script_digest"] == b.get("script_digest")
                and c.get("environment_fingerprint") == b.get("environment_fingerprint")
                for c in failed
                for b in base
            ):
                return True
            proof = finding.get("invariant_evidence", {})
            if (
                failed
                and proof.get("reason_base_not_applicable")
                and proof.get("explanation")
                and proof.get("reference_path")
                and proof.get("quote")
            ):
                reference = self.read_file(
                    proof["reference_path"], "base", proof.get("line", 1), 40
                )["content"]
                return proof["quote"] in reference
        except (ContractError, OSError):
            return False
        return False

    def finish(self, summary=""):
        with self.guard:
            if self.closed:
                return json.loads((self.run_dir / "sealed/result.json").read_text())
            if any(not f.done() for f in self.futures.values()) or any(
                r["status"] == "running" for r in self.native.values()
            ):
                raise ContractError("Wait for commands to finish before sealing")
            self.sealing_started = True
            self.journal.phase(self.task["task_id"], "sealing")
            cleanup = self.before_seal() if self.before_seal else {"status": "pass"}
            records = self.journal.executions(self.task["task_id"])
            checks = []
            unfinished = []
            blockers = []
            performance = []
            latest = {(r["tool_id"], r["variant"]): r for r in records}
            if cleanup.get("status") != "pass":
                unfinished.append("cleanup: " + cleanup.get("reason", "unknown"))
            for tool_id in self.policy["required_checks"]:
                r = latest.get((tool_id, "candidate"))
                if not self.fresh(tool_id):
                    unfinished.append(tool_id)
                    if not r:
                        checks.append(
                            {
                                "tool_id": tool_id,
                                "variant": "candidate",
                                "status": "blocked_dependency",
                                "required": True,
                                "reason": "No completed evidence",
                            }
                        )
                    elif r["status"] == "fail" and r.get("original_subject"):
                        blockers.append(
                            {
                                "kind": "check_failure",
                                "tool_id": tool_id,
                                "execution_id": r["execution_id"],
                                "reason": r.get("reason", "failed"),
                            }
                        )
            for (tool_id, variant), r in latest.items():
                if tool_id not in TOOLS:
                    continue
                public = self.public_record(r)
                public["required"] = (
                    variant == "candidate" and tool_id in self.policy["required_checks"]
                )
                checks.append(public)
                if (
                    variant == "candidate"
                    and r["status"] == "fail"
                    and r.get("original_subject")
                    and not public["required"]
                ):
                    blockers.append(
                        {
                            "kind": "selected_check_failure",
                            "tool_id": tool_id,
                            "execution_id": r["execution_id"],
                        }
                    )
                if variant == "candidate" and r["status"] == "infra_error":
                    unfinished.append(tool_id)
                if tool_id in {"compile_time", "pass_profile", "ir_serialization"}:
                    performance.append(
                        {
                            "tool_id": tool_id,
                            "execution_id": r["execution_id"],
                            "details": r.get("details", {}),
                        }
                    )
            for tool_id in self.policy["capabilities"]:
                if (tool_id, "candidate") not in latest and tool_id not in self.policy[
                    "required_checks"
                ]:
                    checks.append(
                        {
                            "tool_id": tool_id,
                            "status": "not_selected",
                            "required": False,
                            "reason": "Not selected for this change",
                        }
                    )
            for tool_id in self.policy["not_applicable"]:
                checks.append(
                    {
                        "tool_id": tool_id,
                        "status": "not_applicable",
                        "required": False,
                        "reason": "Capability not deployed for profile",
                    }
                )
            reviews = self.journal.reviews(self.task["task_id"])
            findings = []
            for kind in self.policy["required_reviews"]:
                review = reviews.get(kind, {})
                if review.get("status") != "pass":
                    unfinished.append("review:" + kind)
                if kind == "pr_info" and review.get("status") == "fail":
                    blockers.append(
                        {"kind": "pr_information", "reason": review["summary"]}
                    )
            for review in reviews.values():
                for item in review.get("findings", []):
                    blocking = self.finding_blocker(item)
                    findings.append({**item, "blocking": blocking})
                    if blocking:
                        blockers.append(
                            {
                                "kind": item.get("category", "verified_high_risk"),
                                "finding": item,
                            }
                        )
            status = (
                "cancelled"
                if self.cancelled.is_set()
                else "fail"
                if blockers
                else "infra_error"
                if unfinished
                else "pass"
            )
            result = {
                "schema": RESULT_SCHEMA,
                "task": self.task,
                "run_id": self.journal.task(self.task["task_id"])["run_id"],
                "status": status,
                "summary": summary,
                "policy_version": self.policy["version"],
                "required_checks": self.policy["required_checks"],
                "policy": self.policy,
                "changes": self.changes,
                "checks": checks,
                "reviews": reviews,
                "findings": findings,
                "blockers": blockers,
                "performance": performance,
                "unfinished": sorted(set(unfinished)),
                "environment": {
                    k: self.executor.generation.get(k)
                    for k in (
                        "profile",
                        "generation",
                        "environment_fingerprint",
                        "backend_enabled",
                        "image_id",
                    )
                },
                "environment_cleanup": cleanup,
            }
            staged = self.run_dir / (".sealing-" + uuid.uuid4().hex)
            staged.mkdir()
            result["artifacts"] = seal_artifacts(staged, records, redact=self.redact)
            executions = []
            for r in records:
                executions.append(
                    {
                        **{
                            k: r.get(k)
                            for k in (
                                "execution_id",
                                "source_execution_id",
                                "record_type",
                                "tool_id",
                                "variant",
                                "status",
                                "exit_code",
                                "execution_kind",
                                "original_subject",
                                "started_at",
                                "finished_at",
                                "tested_sha",
                                "environment_fingerprint",
                                "scope",
                            )
                        },
                        "artifact_ids": [
                            a["artifact_id"]
                            for a in result["artifacts"]
                            if a["execution_id"] == r["execution_id"]
                        ],
                    }
                )
            atomic_json(
                staged / "execution-summary.json",
                {
                    "schema": EXECUTIONS_SCHEMA,
                    "task_id": self.task["task_id"],
                    "run_id": result["run_id"],
                    "executions": self.redact(executions),
                },
            )
            result["execution_summary_sha256"] = hashlib.sha256(
                (staged / "execution-summary.json").read_bytes()
            ).hexdigest()
            result = self.redact(result)
            atomic_json(staged / "result.json", result)
            sealed = self.run_dir / "sealed"
            os.replace(staged, sealed)
            descriptor = os.open(self.run_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.journal.queue_result(
                self.task["task_id"],
                sealed / "result.json",
                hashlib.sha256((sealed / "result.json").read_bytes()).hexdigest(),
            )
            self.closed = True
            return result

    def cancel(self, reason):
        self.cancelled.set()
        self.journal.event(self.task["task_id"], "cancel_requested", {"reason": reason})
        for r in self.journal.executions(self.task["task_id"]):
            if r["status"] in {"running", "queued"}:
                self.executor.stop(r["execution_id"])

    def close(self):
        self.pool.shutdown(wait=True)


class ToolService:
    """A bearer-scoped local socket; the MCP process has no journal write access."""

    METHODS = frozenset(SCHEMAS)

    def __init__(self, supervisor: Supervisor, socket_path: Path):
        self.supervisor = supervisor
        self.path, self.token = socket_path, secrets.token_hex(32)
        if (
            not socket_path.is_absolute()
            or socket_path.parent.resolve() != socket_path.parent
        ):
            raise ContractError(
                "RPC socket must use an absolute trusted directory without symlinks"
            )
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.parent.stat().st_uid != os.geteuid():
            raise ContractError(
                "RPC socket directory must belong to the worker account"
            )
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
                    validate_value(
                        request,
                        schema(
                            {
                                "token": {"type": "string"},
                                "method": {"type": "string"},
                                "arguments": {"type": "object"},
                            },
                            ["token", "method", "arguments"],
                        ),
                        "RPC request",
                    )
                    if not secrets.compare_digest(
                        str(request.get("token", "")), service.token
                    ):
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
                self.wfile.write(
                    json.dumps(response, ensure_ascii=False).encode() + b"\n"
                )

        self.socket_dir_fd = None
        bind_path = str(socket_path)
        if len(os.fsencode(bind_path)) >= 104:
            self.socket_dir_fd = os.open(
                socket_path.parent, os.O_RDONLY | os.O_DIRECTORY
            )
            bind_path = f"/proc/self/fd/{self.socket_dir_fd}/{socket_path.name}"
        self.server = socketserver.ThreadingUnixStreamServer(bind_path, Handler)
        self.server.daemon_threads = True
        socket_path.chmod(
            0o666
        )  # Authentication is the per-task unguessable token, not ambient uid.
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.path.unlink(missing_ok=True)
        if self.socket_dir_fd is not None:
            os.close(self.socket_dir_fd)
