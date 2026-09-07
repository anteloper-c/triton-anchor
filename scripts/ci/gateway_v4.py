#!/usr/bin/env python3
"""Trusted GitHub/Gitee control plane. Candidate text is data, never commands.

The pure contract functions and file/Git transports are also used by the offline
integration suite. Production network operations are restricted to our fork.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

TASK_SCHEMA = "triton-anchor-local-ci-task/v4"
RESULT_SCHEMA = "triton-anchor-local-ci/v4"
RECEIPT_SCHEMA = "triton-anchor-local-ci-receipt/v4"
CONTROL_BRANCH = "local-ci-control"
RESULTS_BRANCH = "local-ci-results"
REPOSITORY = "likehupochuan/triton-anchor"
IDENTITY_FIELDS = ("repository", "event_kind", "pr_number", "target_branch", "tested_sha",
                   "base_sha", "head_sha", "worker_revision_sha", "metadata_digest", "full")
MARKER = "<!-- triton-anchor-ci-v4 -->"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def metadata_digest(task: dict) -> str:
    return digest({key: sorted(task[key]) if key == "labels" else task[key]
                   for key in ("title", "description", "labels", "state", "draft")})


def current_key(task: dict) -> str:
    subject = f"pr:{task['pr_number']}" if task["pr_number"] else f"branch:{task['target_branch']}"
    return hashlib.sha256(f"{task['repository']}:{subject}".encode()).hexdigest()


def validate_task(task: dict) -> dict:
    if not isinstance(task, dict) or task.get("schema") != TASK_SCHEMA:
        raise ValueError("Unsupported task schema")
    for key in ("tested_sha", "base_sha", "head_sha", "worker_revision_sha", "llvm_hash"):
        if not isinstance(task.get(key), str) or not SHA.fullmatch(task[key]):
            raise ValueError(f"Invalid task {key}")
    for key in ("repository", "target_branch", "title", "description", "state", "captured_at"):
        if not isinstance(task.get(key), str):
            raise ValueError(f"Invalid task {key}")
    if task["event_kind"] not in {"pull_request", "push", "manual"}:
        raise ValueError("Invalid event kind")
    if task["target_branch"] == "CI_dev_forPR":
        raise ValueError("CI_dev_forPR is excluded from this deployment")
    if type(task.get("pr_number")) is not int or task["pr_number"] < 0:
        raise ValueError("Invalid PR number")
    if (task["event_kind"] == "pull_request") != bool(task["pr_number"]):
        raise ValueError("Event kind and PR number disagree")
    if type(task.get("draft")) is not bool or type(task.get("full")) is not bool:
        raise ValueError("Invalid boolean task field")
    if not isinstance(task.get("labels"), list) or not all(isinstance(x, str) for x in task["labels"]):
        raise ValueError("Invalid labels")
    for key in ("task_ref", "base_task_ref", "head_task_ref"):
        value = task.get(key, "")
        if not isinstance(value, str) or not value.startswith("ci/") or any(c in value for c in "\n\r\x00"):
            raise ValueError(f"Invalid {key}")
        if subprocess.run(["git", "check-ref-format", f"refs/heads/{value}"], capture_output=True).returncode:
            raise ValueError(f"Invalid {key}")
    if task.get("metadata_digest") != metadata_digest(task):
        raise ValueError("Task metadata digest mismatch")
    if task.get("task_id") != digest({key: task[key] for key in IDENTITY_FIELDS}):
        raise ValueError("Task identity mismatch")
    return task


FIELD_NAMES = {
    "types": ("类型", "type", "types", "change type"),
    "purpose": ("目的", "purpose", "motivation"),
    "scope": ("改动范围", "scope", "change scope"),
    "validation": ("验证", "验证方式", "validation"),
    "reproduction": ("复现", "reproduction", "repro"),
    "expected_actual": ("实际预期", "实际与预期", "expected and actual", "expected_actual"),
    "behavior": ("行为变化", "behavior", "behavior changes"),
    "compatibility": ("兼容性", "compatibility"),
    "baseline": ("基线", "baseline"),
    "measurement": ("测量方法", "measurement"),
    "expected_change": ("预期变化", "expected change", "expected_change"),
    "versions": ("前后版本", "versions"),
    "sources": ("来源", "sources"),
    "environment": ("环境影响", "environment"),
    "recovery": ("恢复", "恢复方式", "recovery", "rollback"),
    "ci_impact": ("触发权限协议影响", "ci impact", "ci_impact"),
    "subject": ("修改对象", "subject"),
    "consistency": ("一致性", "consistency"),
    "coverage": ("覆盖行为", "coverage"),
    "execution": ("执行", "execution"),
}
TYPE_FIELDS = {
    "fix": ("reproduction", "expected_actual"),
    "feat": ("behavior", "compatibility"),
    "refactor": ("behavior", "compatibility"),
    "perf": ("baseline", "measurement", "expected_change"),
    "build": ("versions", "sources", "environment", "recovery"),
    "ci": ("ci_impact", "recovery"),
    "docs": ("subject", "consistency"),
    "test": ("coverage", "execution"),
}


def pr_fields(description: str) -> dict[str, str]:
    fields: dict[str, list[str]] = {}
    current = ""
    aliases = {alias: key for key, values in FIELD_NAMES.items() for alias in values}
    for line in description.splitlines():
        marker = re.search(r"<!--\s*field:([a-z_]+)\s*-->", line)
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if marker:
            current = marker[1] if marker[1] in FIELD_NAMES else ""
        elif heading:
            pieces = re.split(r"\s*[/|／]\s*", heading[1].lower())
            current = next((aliases[p] for p in pieces if p in aliases), "")
        elif current:
            fields.setdefault(current, []).append(line)
    return {key: re.sub(r"<!--.*?-->", "", "\n".join(lines), flags=re.S).strip()
            for key, lines in fields.items()}


def validate_pr_info(task: dict) -> list[str]:
    if not task["pr_number"]:
        return []
    fields = pr_fields(task["description"])
    text = fields.get("types", "").lower()
    if "[" in text:
        types = re.findall(r"\[[xX]\]\s*(fix|feat|refactor|perf|build|ci|docs|test)\b", text)
    else:
        types = [x for x in re.split(r"[^a-z]+", text) if x in TYPE_FIELDS]
    required = {"purpose", "scope", "validation"}
    for kind in types:
        required.update(TYPE_FIELDS[kind])
    errors = []
    if not task["title"].strip() or task["title"].strip().lower() in {"wip", "todo", "test", "update", "更新"}:
        errors.append("请填写能描述改动目的的 PR 标题。")
    if not types:
        errors.append("请在 types 中选择至少一个类型：fix/feat/refactor/perf/build/ci/docs/test。")
    placeholders = re.compile(r"^(?:todo|tbd|待填写|待补充|请填写.*|\.\.\.|<.*>)$", re.I | re.S)
    for key in sorted(required):
        value = fields.get(key, "").strip()
        if not value or placeholders.fullmatch(value):
            errors.append(f"请补充 {FIELD_NAMES[key][0]}（field:{key}）。")
    return errors


class GitHub:
    def __init__(self, repository: str, api_url: str | None = None, token: str | None = None):
        self.repository = repository
        self.api_url = (api_url or os.getenv("GITHUB_API_URL", "https://api.github.com")).rstrip("/")
        parsed = urlparse(self.api_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            if self.api_url != "https://api.github.com" or repository != REPOSITORY:
                raise ValueError("Production GitHub repository/API is outside the allowlist")
        self.token = token if token is not None else os.getenv("GH_TOKEN", "")

    def request(self, path: str, method: str = "GET", data: dict | None = None):
        req = Request(f"{self.api_url}/repos/{self.repository}/{path.lstrip('/')}",
                      data=canonical(data) if data is not None else None, method=method,
                      headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                               "Content-Type": "application/json", "User-Agent": "triton-anchor-ci-v4"})
        with urlopen(req, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None

    def optional(self, path: str):
        try:
            return self.request(path)
        except HTTPError as error:
            if error.code == 404:
                return None
            raise

    def content(self, path: str, ref: str) -> bytes:
        data = self.request(f"contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise ValueError(f"Expected a vendored file at {path}; configure its trusted mirror before dispatch")
        return base64.b64decode(data["content"], validate=False)

    def status(self, task: dict, state: str, description: str, url: str = "") -> None:
        for sha, context in {(task["tested_sha"], "local-ci/sophgo-cmodel"),
                             (task["head_sha"], "local-ci/summary")}:
            self.request(f"statuses/{sha}", "POST", {"state": state, "context": context,
                         "description": description[:140], "target_url": url})

    def comment(self, task: dict, body: str) -> None:
        if not task["pr_number"]:
            return
        path = f"issues/{task['pr_number']}/comments"
        comments = []
        for page in range(1, 21):
            rows = self.request(f"{path}?per_page=100&page={page}")
            comments.extend(rows)
            if len(rows) < 100:
                break
        existing = next((row for row in comments if row.get("user", {}).get("type") == "Bot"
                         and str(row.get("body", "")).startswith(MARKER)), None)
        content = {"body": f"{MARKER}\n{body}"[:60000]}
        if existing:
            if existing.get("body") != content["body"]:
                self.request(f"issues/comments/{existing['id']}", "PATCH", content)
        else:
            self.request(path, "POST", content)


def prepare_task(gh: GitHub, worker_sha: str, pr_number: int = 0, branch: str = "",
                 requested_sha: str = "", full: bool = False, event_kind: str = "push") -> dict:
    if not SHA.fullmatch(worker_sha):
        raise ValueError("Invalid trusted worker revision")
    if pr_number:
        pull = gh.request(f"pulls/{pr_number}")
        if pull["state"] != "open" or pull["draft"]:
            raise ValueError("PR is closed or draft")
        head = pull["head"]["sha"]
        if requested_sha and head != requested_sha:
            raise ValueError("PR changed after the routing event")
        merge = gh.request(f"git/ref/pull/{pr_number}/merge")["object"]["sha"]
        parents = gh.request(f"git/commits/{merge}")["parents"]
        if len(parents) != 2 or parents[1]["sha"] != head:
            raise ValueError("Merge parents do not match the PR")
        base = parents[0]["sha"]
        branch = pull["base"]["ref"]
        description, title = pull.get("body") or "", pull["title"]
        labels = sorted(row["name"] for row in pull.get("labels", []))
        event_kind = "pull_request"
        ref = f"ci/pr-{pr_number}/{pull['head']['ref']}"
        base_ref, head_ref = f"ci/base/pr-{pr_number}/{pull['head']['ref']}", f"ci/head/pr-{pr_number}/{pull['head']['ref']}"
        external = pull["head"]["repo"]["full_name"] != gh.repository
    else:
        head = gh.request(f"branches/{quote(branch, safe='')}")["commit"]["sha"]
        if requested_sha and requested_sha != head:
            raise ValueError("Branch changed after routing")
        merge = head
        parents = gh.request(f"git/commits/{head}").get("parents", [])
        base = parents[0]["sha"] if parents else head
        title, description, labels = f"Branch {branch}", "Trusted branch task", []
        ref = f"ci/{'full' if full else 'push'}/{branch}"
        base_ref, head_ref = f"ci/base/push/{branch}", f"ci/head/push/{branch}"
        external = False
    task = dict(schema=TASK_SCHEMA, repository=gh.repository, event_kind=event_kind, pr_number=pr_number,
                task_ref=ref, base_task_ref=base_ref, head_task_ref=head_ref, tested_sha=merge,
                base_sha=base, head_sha=head, worker_revision_sha=worker_sha, target_branch=branch,
                title=title, description=description, labels=labels, state="open", draft=False,
                captured_at=now(), llvm_hash=gh.content("triton/cmake/llvm-hash.txt", merge).decode().strip(),
                full=full, external_fork=external)
    task["metadata_digest"] = metadata_digest(task)
    task["task_id"] = digest({key: task[key] for key in IDENTITY_FIELDS})
    return validate_task(task)


def is_current(gh: GitHub, task: dict) -> bool:
    if task["repository"] != gh.repository:
        raise ValueError("Task repository differs from the receiver repository")
    if task["pr_number"]:
        pull = gh.request(f"pulls/{task['pr_number']}")
        live = {"title": pull["title"], "description": pull.get("body") or "",
                "labels": [row["name"] for row in pull.get("labels", [])],
                "state": pull["state"], "draft": pull["draft"]}
        if (pull["head"]["sha"] != task["head_sha"] or pull["base"]["ref"] != task["target_branch"]
                or live["state"] != "open" or live["draft"] or metadata_digest(live) != task["metadata_digest"]):
            return False
        merge = gh.optional(f"git/ref/pull/{task['pr_number']}/merge")
        return bool(merge and merge["object"]["sha"] == task["tested_sha"])
    return gh.request(f"branches/{quote(task['target_branch'], safe='')}")["commit"]["sha"] == task["head_sha"]


def validate_approval_environment(gh: GitHub) -> None:
    environment = gh.request("environments/local-ci-fork-approval")
    if not isinstance(environment, dict) or not any(
        rule.get("type") == "required_reviewers" and isinstance(rule.get("reviewers"), list) and rule["reviewers"]
        for rule in environment.get("protection_rules", []) if isinstance(rule, dict)
    ):
        raise ValueError("local-ci-fork-approval must have non-empty required reviewers; configure the existing environment before allowing external fork Local CI")


class GitStore:
    """A temporary clone with optimistic non-force commits, never a user checkout."""
    def __init__(self, url: str, branch: str):
        parsed = urlparse(url)
        if not parsed.scheme and not Path(url).exists():
            raise ValueError("An unqualified transport must be an existing local test repository")
        if parsed.scheme and parsed.scheme != "file" and (parsed.scheme != "https" or parsed.hostname != "gitee.com"
                                                        or parsed.username or parsed.password):
            raise ValueError("Gitee transport accepts HTTPS gitee.com or local test repositories")
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-v4-transport-")
        self.root = Path(self.temporary.name) / "repo"
        self.branch = branch
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        askpass = Path(self.temporary.name) / "askpass.sh"
        askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" "$GITEE_USERNAME" ;; *) printf "%s\\n" "$GITEE_TOKEN" ;; esac\n')
        askpass.chmod(0o700)
        self.env["GIT_ASKPASS"] = str(askpass)
        self.root.mkdir()
        self.run("init")
        self.run("remote", "add", "origin", url)
        self.run("config", "user.name", "triton-anchor-ci")
        self.run("config", "user.email", "ci@example.invalid")
        self.refresh()

    def run(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.root, env=self.env,
                                text=True, capture_output=True)
        if check and result.returncode:
            raise RuntimeError(f"Git {args[0]} failed (exit {result.returncode}); check transport/authentication")
        return result.stdout.strip()

    def refresh(self) -> None:
        if self.run("ls-remote", "--heads", "origin", f"refs/heads/{self.branch}"):
            self.run("fetch", "--depth=1", "origin", f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}")
            self.run("checkout", "-B", self.branch, f"refs/remotes/origin/{self.branch}")
        elif self.run("rev-parse", "--verify", "HEAD", check=False):
            self.run("checkout", "--orphan", f"init-{self.branch}-{time.time_ns()}")
            self.run("rm", "-rf", "--ignore-unmatch", ".")
        else:
            self.run("symbolic-ref", "HEAD", f"refs/heads/{self.branch}")

    def get(self, path: str):
        location = self.root / path
        return json.loads(location.read_text()) if location.is_file() else None

    def put(self, documents: dict[str, dict], immutable: tuple[str, ...] = ()) -> None:
        for attempt in range(3):
            if attempt:
                self.refresh()
            for name, document in documents.items():
                location = self.root / name
                if location.resolve().is_relative_to(self.root.resolve()) is False:
                    raise ValueError("Unsafe control path")
                old = self.get(name)
                if name in immutable and old is not None and old != document:
                    raise ValueError(f"Immutable record differs: {name}")
                location.parent.mkdir(parents=True, exist_ok=True)
                location.write_bytes(canonical(document) + b"\n")
            self.run("add", "--", *documents)
            if not self.run("diff", "--cached", "--name-only"):
                return
            self.run("commit", "-m", "ci: update v4 control records")
            push = subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{self.branch}"], cwd=self.root,
                                  env=self.env, capture_output=True)
            if push.returncode == 0:
                return
        raise RuntimeError("Gitee control publication failed after three attempts")

    def close(self) -> None:
        self.temporary.cleanup()


def enqueue(task: dict, gh: GitHub, control: GitStore, source: Path) -> None:
    validate_task(task)
    if validate_pr_info(task) or not is_current(gh, task):
        raise ValueError("PR information or task freshness no longer permits dispatch")
    checked_out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source).decode().strip()
    if checked_out != task["tested_sha"]:
        raise ValueError("Dispatcher did not check out the frozen tested SHA")
    # All git operations are against the already fetched likehupochuan checkout.
    refs = [("tested_sha", "task_ref"), ("base_sha", "base_task_ref"), ("head_sha", "head_task_ref")]
    remote = control.run("remote", "get-url", "origin")
    for sha_key, ref_key in refs:
        subprocess.run(["git", "push", "--force", remote, f"{task[sha_key]}:refs/heads/{task[ref_key]}"],
                       cwd=source, env=control.env, check=True, capture_output=True)
    if not is_current(gh, task):
        raise ValueError("Task changed while publishing code refs")
    key = f"current/{current_key(task)}.json"
    previous = control.get(key)
    documents = {f"tasks/{task['task_id']}.json": task, key: {
        "task_id": task["task_id"], "repository": task["repository"], "pr_number": task["pr_number"],
        "target_branch": task["target_branch"], "tested_sha": task["tested_sha"], "updated_at": now()}}
    if previous and previous["task_id"] != task["task_id"]:
        documents[f"cancel/{previous['task_id']}.json"] = {
            "task_id": previous["task_id"], "reason": "superseded", "superseded_by": task["task_id"], "created_at": now()}
    # On an identical retry preserve the original immutable capture timestamp.
    old = control.get(f"tasks/{task['task_id']}.json")
    if old:
        validate_task(old)
        documents[f"tasks/{task['task_id']}.json"] = old
    control.put(documents, (f"tasks/{task['task_id']}.json",))
    gh.status(task, "pending", "Local CI: task published to Gitee")


def cancel_obsolete(gh: GitHub, control: GitStore, pr_number: int = 0) -> int:
    count = 0
    for path in sorted((control.root / "current").glob("*.json")):
        row = json.loads(path.read_text())
        task = control.get(f"tasks/{row['task_id']}.json")
        if not task or (pr_number and task["pr_number"] != pr_number):
            continue
        validate_task(task)
        if not is_current(gh, task):
            name = f"cancel/{task['task_id']}.json"
            cancellation = control.get(name)
            if not cancellation:
                cancellation = {"task_id": task["task_id"], "reason": "PR/branch lifecycle or metadata changed", "created_at": now()}
                control.put({name: cancellation})
                count += 1
            # A newer dispatched task owns the current status/comment. Never let
            # an old cancellation overwrite its result (including same-head edits).
            control.refresh()
            pointer = control.get(f"current/{current_key(task)}.json")
            if pointer and pointer["task_id"] == task["task_id"] and not cancellation.get("github_notified"):
                gh.status(task, "error", "Local CI cancelled: PR/branch changed, closed or became draft")
                gh.comment(task, f"## Local CI 旧任务已取消\n\n任务 `{task['task_id']}`，被测提交 `{task['tested_sha']}`。\n\nPR/分支的提交、目标、信息或状态已变化，本地 worker 已收到停止通知；此结果不能作为当前通过结果。若 PR 仍需验证，请查看对应新任务或从 Gateway 重新请求。")
                cancellation["github_notified"] = True
                control.put({name: cancellation})
    return count


def validate_result(result: dict, expected_task: dict) -> dict:
    validate_task(expected_task)
    if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA:
        raise ValueError("Unsupported result schema")
    task = validate_task(result.get("task"))
    if task != expected_task:
        raise ValueError("Result task differs from immutable dispatched task")
    if not isinstance(result.get("run_id"), str) or not RUN_ID.fullmatch(result["run_id"]):
        raise ValueError("Invalid run id")
    if result.get("status") not in {"pass", "fail", "infra_error", "cancelled"}:
        raise ValueError("Invalid result status")
    if not isinstance(result.get("checks"), list) or not isinstance(result.get("required_checks"), list):
        raise ValueError("Missing check selection/evidence")
    checks = {}
    executions = set()
    for check in result["checks"]:
        if not isinstance(check, dict) or not isinstance(check.get("tool_id"), str):
            raise ValueError("Invalid or duplicate check")
        identity = (check["tool_id"], check.get("variant", "candidate"))
        if identity in executions:
            raise ValueError("Duplicate check/variant")
        executions.add(identity)
        if identity[1] == "candidate":
            checks[check["tool_id"]] = check
        if check.get("status") not in {"pass", "fail", "infra_error", "cancelled", "not_applicable", "not_selected", "blocked_dependency"}:
            raise ValueError("Invalid check status")
        if check["status"] != "pass" and not check.get("reason"):
            raise ValueError("Non-passing check requires a reason")
    if result["status"] == "pass":
        if result.get("blockers") or result.get("unfinished"):
            raise ValueError("Passing result contains blockers/unfinished work")
        for key in result["required_checks"]:
            if checks.get(key, {}).get("status") != "pass":
                raise ValueError(f"Required check did not pass: {key}")
        for name in ("pr_info", "architecture"):
            review = result.get("reviews", {}).get(name, {})
            if review.get("status") != "pass" or (name == "architecture" and not review.get("evidence")):
                raise ValueError(f"Mandatory review has no passing evidence: {name}")
        if "environment" not in result["required_checks"]:
            raise ValueError("Environment check is mandatory for every task")
    return result


def trusted_minimum(task: dict, source: GitStore) -> dict:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local_ci"))
    from agent_ci.policy import changed_files, minimum_checks
    for sha in {task["base_sha"], task["tested_sha"]}:
        source.run("fetch", "--depth=1", "origin", sha)
    changes = changed_files(source.root, task["base_sha"], task["tested_sha"])
    if not changes:
        changes = [{"path": "<branch-validation>", "status": "M"}]
    versions = [source.run("show", f"{sha}:triton/python/triton/__init__.py", check=False)
                for sha in (task["base_sha"], task["tested_sha"])]
    if not all(versions):
        raise ValueError("Cannot verify Triton version for the result minimum")
    backend = any(re.search(r"__version__\s*=\s*['\"]3\.0(?:\.|['\"])", text) for text in versions)
    return minimum_checks(changes, backend_enabled=backend, full=task["full"])


def result_comment(result: dict) -> str:
    task = result["task"]
    safe = lambda value: html.escape(str(value)).replace("@", "＠").replace("`", "'")
    lines = [f"## Local CI · {safe(result['status'])}",
             f"被测提交：`{task['tested_sha']}` · task `{task['task_id'][:12]}` · run `{result['run_id']}`", "",
             "| 检查 | 结果 | 原因 |", "| --- | --- | --- |"]
    for check in result["checks"]:
        lines.append(f"| {safe(check['tool_id'])} | {safe(check['status'])} | {safe(check.get('reason', '')).replace('|', '/')} |")
    for key, title in (("blockers", "阻塞与下一步"), ("findings", "审查证据"), ("performance", "性能变化"), ("unfinished", "未完成")):
        lines.extend(["", f"### {title}", "", safe(json.dumps(result.get(key, []), ensure_ascii=False))[:9000]])
    return "\n".join(lines)


def collect_results(gh: GitHub, control: GitStore, results: GitStore, dashboard: Path) -> list[dict]:
    """Writeback status/comment and stage dashboard; ACK only after Pages succeeds."""
    rows, pending = [], []
    for current in sorted((control.root / "current").glob("*.json")):
        pointer = json.loads(current.read_text())
        task = control.get(f"tasks/{pointer['task_id']}.json")
        validate_task(task)
        active = is_current(gh, task) and not control.get(f"cancel/{task['task_id']}.json")
        row = {"task": task, "status": "pending" if active else "cancelled", "result": None}
        candidates = sorted((results.root / "runs/v4" / task["task_id"]).glob("*/result.json"), reverse=True)
        if candidates:
            try:
                path = candidates[0]
                raw = path.read_bytes()
                result = validate_result(json.loads(raw), task)
                if result["status"] == "pass":
                    minimum = trusted_minimum(task, results)
                    if not set(minimum["required_checks"]) <= set(result["required_checks"]):
                        raise ValueError("Result omitted trusted minimum checks")
                if path.parent.name != result["run_id"]:
                    raise ValueError("Result path/run id mismatch")
                row.update(status=result["status"] if active else "cancelled", result=result)
                receipt_path = f"receipts/{task['task_id']}/{result['run_id']}.json"
                if active and not control.get(receipt_path) and is_current(gh, task):
                    state = {"pass": "success", "fail": "failure", "infra_error": "error", "cancelled": "error"}[result["status"]]
                    gh.status(task, state, f"Local CI: {result['status']} (merge {task['tested_sha'][:12]})")
                    if not is_current(gh, task):
                        row["status"] = "cancelled"
                        rows.append(row)
                        continue
                    gh.comment(task, result_comment(result))
                    pending.append({"schema": RECEIPT_SCHEMA, "task_id": task["task_id"], "run_id": result["run_id"],
                                    "tested_sha": task["tested_sha"], "result_digest": hashlib.sha256(raw).hexdigest(),
                                    "status": "complete", "github_status": True, "comment": True, "dashboard": True})
            except (ValueError, OSError, RuntimeError) as error:
                row.update(status="infra_error", receiver_error=type(error).__name__,
                           receiver_message="结果校验或 GitHub 回写未完成；保留证据并重试接收，不重跑构建。")
                # One broken result must not starve other tasks or hide the dashboard.
                if active and is_current(gh, task):
                    try:
                        gh.status(task, "error", "Local CI result validation/writeback failed; receiver will retry")
                    except (ValueError, OSError, RuntimeError):
                        pass
        rows.append(row)
    dashboard.mkdir(parents=True, exist_ok=True)
    (dashboard / "v4-tasks.json").write_bytes(canonical({"schema": "triton-anchor-dashboard/v4", "generated_at": now(), "tasks": rows}) + b"\n")
    output("receiver_errors", sum(bool(row.get("receiver_error")) for row in rows))
    return pending


def monitor_receipts(control: GitStore, results: GitStore, state_file: Path) -> list[dict]:
    previous = json.loads(state_file.read_text()) if state_file.is_file() else {}
    seen, records = {}, []
    for current in sorted((control.root / "current").glob("*.json")):
        task = control.get(f"tasks/{json.loads(current.read_text())['task_id']}.json")
        validate_task(task)
        if control.get(f"cancel/{task['task_id']}.json"):
            continue
        candidates = sorted((results.root / "runs/v4" / task["task_id"]).glob("*/result.json"), reverse=True)
        record = {"task_id": task["task_id"], "worker_id": "receiver", "status": "queued", "created_at": task["captured_at"]}
        if candidates:
            run_id = candidates[0].parent.name
            if not RUN_ID.fullmatch(run_id):
                continue
            key = f"{task['task_id']}/{run_id}"
            if control.get(f"receipts/{key}.json"):
                continue
            seen[key] = previous.get(key, now())
            record.update(status="published", run_id=run_id, published_at=seen[key])
        records.append(record)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_bytes(canonical(seen) + b"\n")
    return records


def acknowledge(gh: GitHub, control: GitStore, results: GitStore, receipts: list[dict]) -> None:
    for receipt in receipts:
        if not DIGEST.fullmatch(str(receipt.get("task_id", ""))) or not RUN_ID.fullmatch(str(receipt.get("run_id", ""))):
            raise ValueError("Invalid receipt task/run identity")
        task = control.get(f"tasks/{receipt['task_id']}.json")
        validate_task(task)
        if receipt.get("tested_sha") != task["tested_sha"]:
            raise ValueError("Receipt tested SHA differs from the task")
        pointer = control.get(f"current/{current_key(task)}.json")
        if (not pointer or pointer["task_id"] != task["task_id"] or
                control.get(f"cancel/{task['task_id']}.json") or not is_current(gh, task)):
            continue
        path = results.root / "runs/v4" / task["task_id"] / receipt["run_id"] / "result.json"
        raw = path.read_bytes()
        validate_result(json.loads(raw), task)
        if hashlib.sha256(raw).hexdigest() != receipt["result_digest"]:
            raise ValueError("Result changed before acknowledgement")
        if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("status") != "complete" or any(receipt.get(k) is not True for k in ("github_status", "comment", "dashboard")):
            raise ValueError("Incomplete receipt")
        name = f"receipts/{task['task_id']}/{receipt['run_id']}.json"
        control.put({name: receipt}, (name,))


def output(key: str, value: object) -> None:
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


def load_task(path: Path, expected_digest: str = "") -> dict:
    task = validate_task(json.loads(path.read_text()))
    if expected_digest and digest(task) != expected_digest:
        raise ValueError("Task artifact differs from the trusted prepare job output")
    return task


def security_diff(source: Path, base: str, tested: str) -> int:
    import importlib.util
    from dataclasses import asdict
    import sys
    spec = importlib.util.spec_from_file_location("trusted_security", Path(__file__).with_name("scan_pr_security.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    command = ["git", "-C", str(source), "diff", "--no-ext-diff", "--no-textconv", "--no-renames"]
    names = subprocess.check_output([*command, "--name-only", "-z", base, tested, "--"]).decode().split("\0")
    files = []
    for name in filter(None, names):
        patch = subprocess.check_output([*command, "--unified=3", base, tested, "--", name]).decode("utf-8", "replace")
        exists = subprocess.run(["git", "-C", str(source), "cat-file", "-e", f"{tested}:{name}"], capture_output=True).returncode == 0
        files.append({"filename": name, "status": "modified" if exists else "removed",
                      "patch": None if "Binary files " in patch else patch})
    blocking, warnings = module.scan(files)
    module.print_findings(blocking + warnings)
    module.append_summary("block", blocking)
    module.append_summary("warn", warnings)
    Path("security-result.json").write_bytes(canonical({"blocking": [asdict(x) for x in blocking],
                                                       "warnings": [asdict(x) for x in warnings]}) + b"\n")
    return int(bool(blocking))


def sarif_failures(root: Path) -> list[dict]:
    findings = []
    files = list(root.rglob("*.sarif"))
    if not files:
        raise ValueError("CodeQL produced no SARIF evidence")
    for path in files:
        document = json.loads(path.read_text())
        for run in document.get("runs", []):
            rules = {row["id"]: row for row in run.get("tool", {}).get("driver", {}).get("rules", [])}
            for result in run.get("results", []):
                rule = rules.get(result.get("ruleId"), {})
                severity = rule.get("properties", {}).get("security-severity")
                if (severity is not None and float(severity) >= 7) or result.get("level") == "error":
                    findings.append(result)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "info", "card", "approval", "enqueue", "cancel", "collect", "ack", "api", "security", "sarif"))
    parser.add_argument("--task", type=Path, default=Path("task.json"))
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", REPOSITORY))
    parser.add_argument("--worker-sha", default=os.getenv("WORKER_SHA", ""))
    parser.add_argument("--pr", type=int, default=int(os.getenv("PR_NUMBER") or 0))
    parser.add_argument("--branch", default=os.getenv("SOURCE_BRANCH", ""))
    parser.add_argument("--sha", default=os.getenv("REQUESTED_SHA", ""))
    parser.add_argument("--full", action="store_true", default=os.getenv("FULL", "false") == "true")
    parser.add_argument("--event-kind", choices=("push", "manual"), default=os.getenv("EVENT_KIND", "push"))
    parser.add_argument("--source", type=Path, default=Path("candidate"))
    parser.add_argument("--base", type=Path, default=Path("base"))
    parser.add_argument("--stages", default=os.getenv("STAGES", "{}"))
    parser.add_argument("--dashboard", type=Path, default=Path("_site/data"))
    parser.add_argument("--receipts", type=Path, default=Path("pending-receipts.json"))
    args = parser.parse_args()
    gh = GitHub(args.repository)
    if args.command == "prepare":
        task = prepare_task(gh, args.worker_sha, args.pr, args.branch, args.sha, args.full, args.event_kind)
        args.task.write_bytes(canonical(task) + b"\n")
        for key in ("task_id", "tested_sha", "head_sha", "base_sha", "external_fork"):
            output(key, task[key])
        output("task_digest", digest(task))
        return 0
    if args.command == "sarif":
        failures = sarif_failures(args.source)
        print(f"CodeQL high/critical or error findings: {len(failures)}")
        return int(bool(failures))
    if args.command in {"info", "card", "approval", "enqueue", "api", "security"}:
        task = load_task(args.task, os.getenv("EXPECTED_TASK_DIGEST", ""))
    if args.command == "approval":
        validate_approval_environment(gh)
        if not is_current(gh, task):
            raise ValueError("PR changed while waiting for approval")
        return 0
    if args.command == "security":
        return security_diff(args.source, task["base_sha"], task["tested_sha"])
    if args.command == "info":
        errors = validate_pr_info(task)
        if errors:
            gh.status(task, "failure", "PR information is incomplete; see PR comment")
            gh.comment(task, "## PR 信息需要补充\n\n" + "\n".join(f"- {x}" for x in errors))
        return int(bool(errors))
    if args.command == "card":
        stages = json.loads(args.stages)
        eligible = all(stages.get(key) == "success" for key in ("prepare", "basic", "api", "security"))
        approval_error = ""
        if eligible and task.get("external_fork"):
            try:
                validate_approval_environment(gh)
            except (ValueError, OSError) as error:
                eligible = False
                approval_error = str(error) if isinstance(error, ValueError) else "Cannot verify required reviewers on local-ci-fork-approval; check repository environment configuration."
        body = "## Local CI 前置检查与审批\n\n" + "\n".join(f"- {key}: {value}" for key, value in stages.items())
        body += f"\n\n被测提交 `{task['tested_sha']}`；目标 `{html.escape(task['target_branch'])}`。\n"
        body += "\n通过后由服务器 Codex 根据 PR 意图选择任务，并执行最低必检、架构审查和必要验证。\n"
        if approval_error:
            body += "\n人工审批配置未通过：" + html.escape(approval_error) + "\n"
        body += "\n外部 fork：请在本次 workflow 的 local-ci-fork-approval environment 审批。" if task.get("external_fork") and eligible else "\n请修复失败检查后更新 PR。" if not eligible else "\n前置检查通过，准备进入 Local CI。"
        gh.comment(task, body)
        gh.status(task, "pending" if eligible else "failure", "Awaiting Local CI/approval" if eligible else "Preflight failed; see PR comment")
        output("eligible", eligible)
        return 0
    if args.command == "api":
        import importlib.util
        checker = Path(__file__).resolve().parents[1] / "api_contract/check_public_api.py"
        spec = importlib.util.spec_from_file_location("api_checker", checker)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        base_scope = args.base / "api_contract/public_api.json"
        scope = base_scope if base_scope.is_file() else Path(__file__).resolve().parents[2] / "api_contract/public_api.json"
        candidate_scope = args.source / "api_contract/public_api.json" if base_scope.is_file() else None
        result = module.run_check(args.base, args.source, scope, candidate_scope)
        Path("api-result.json").write_bytes(canonical(result) + b"\n")
        Path("api-report.md").write_text(module._markdown(result))
        return int(result["status"] != "compatible")
    url = os.getenv("GITEE_RESULTS_REPO_URL", "")
    if not url.startswith("https://gitee.com/"):
        raise ValueError("Configure GITEE_RESULTS_REPO_URL with the actual HTTPS Gitee repository")
    control = GitStore(url, CONTROL_BRANCH)
    try:
        if args.command == "cancel":
            cancel_obsolete(gh, control, args.pr)
        elif args.command == "enqueue":
            enqueue(task, gh, control, args.source)
        else:
            results = GitStore(url, RESULTS_BRANCH)
            try:
                if args.command == "collect":
                    cancel_obsolete(gh, control)
                    records = monitor_receipts(control, results, Path("monitor-state/receipt-age.json"))
                    Path("monitor-receipts.json").write_bytes(canonical(records) + b"\n")
                    receipts = collect_results(gh, control, results, args.dashboard)
                    args.receipts.write_bytes(canonical(receipts) + b"\n")
                elif args.command == "ack":
                    acknowledge(gh, control, results, json.loads(args.receipts.read_text()))
            finally:
                results.close()
    finally:
        control.close()
    return 0


if __name__ == "__main__":
    import sys
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"CI v4 control failed: {type(error).__name__}: {error if isinstance(error, ValueError) else 'inspect the stage logs and transport configuration'}", file=sys.stderr)
        # Errors before task.json exists still need a visible PR response.
        if len(sys.argv) > 1 and sys.argv[1] in {"prepare", "approval", "enqueue"} and os.getenv("GH_TOKEN"):
            try:
                pr = int(os.getenv("PR_NUMBER") or 0)
                client = GitHub(os.getenv("GITHUB_REPOSITORY", REPOSITORY))
                if pr:
                    pull = client.request(f"pulls/{pr}")
                    expected = os.getenv("REQUESTED_SHA") or pull["head"]["sha"]
                    if pull["state"] == "open" and not pull["draft"] and expected == pull["head"]["sha"]:
                        context = {"head_sha": expected, "tested_sha": expected, "pr_number": pr}
                        client.status(context, "error", "CI preparation/publication failed; see PR comment")
                        run_id = os.getenv("GITHUB_RUN_ID", "")
                        link = f"https://github.com/{client.repository}/actions/runs/{run_id}" if run_id.isdigit() else ""
                        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
                        client.comment(context, f"## CI 准备或投递未完成\n\n{html.escape(reason)}\n\n请查看本次工作流证据并修复对应检查或中转配置，然后重试：{link}")
            except (ValueError, OSError, RuntimeError):
                print("PR failure notification could not be delivered; the workflow remains failed.", file=sys.stderr)
        raise SystemExit(1)
