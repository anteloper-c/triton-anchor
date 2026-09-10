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
CONTROL_BRANCH = "local-ci-control"
RESULTS_BRANCH = "local-ci-results"
REPOSITORY = "likehupochuan/triton-anchor"
IDENTITY_FIELDS = ("repository", "event_kind", "pr_number", "target_branch", "tested_sha",
                   "base_sha", "head_sha", "worker_revision_sha", "metadata_digest", "full")
MARKER = "<!-- triton-anchor-ci-v4 -->"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")


class GitHubAPIError(RuntimeError):
    """A diagnostic GitHub failure that never exposes response bodies or credentials."""

    def __init__(self, code: int, method: str, path: str, reason: str = ""):
        self.code = code
        endpoint = path.split("?", 1)[0].lstrip("/")
        detail = f": {reason}" if reason else ""
        super().__init__(f"GitHub API {method} {endpoint} failed with HTTP {code}{detail}")


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
    "summary": ("变更概述", "概述", "summary", "change summary"),
    "scope": ("影响范围", "改动范围", "scope", "change scope"),
    "validation": ("验证情况", "验证", "验证方式", "validation"),
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
    errors = []
    if not task["title"].strip() or task["title"].strip().lower() in {"wip", "todo", "tbd"}:
        errors.append("请填写能描述改动目的的 PR 标题。")
    placeholders = re.compile(r"^(?:todo|tbd|wip|待填写|待补充|请填写.*|\.\.\.|<.*>)$", re.I | re.S)
    for key in ("summary", "scope", "validation"):
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
        try:
            with urlopen(req, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as error:
            reason = re.sub(r"[\r\n]+", " ", str(error.reason or "")).strip()[:160]
            raise GitHubAPIError(error.code, method, path, reason) from None

    def optional(self, path: str):
        try:
            return self.request(path)
        except GitHubAPIError as error:
            if error.code == 404:
                return None
            raise

    def content(self, path: str, ref: str) -> bytes:
        data = self.request(f"contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise ValueError(f"Expected a vendored file at {path}; configure its trusted mirror before dispatch")
        return base64.b64decode(data["content"], validate=False)

    def status(self, task: dict, state: str, description: str, url: str = "") -> None:
        # The PR-head summary is the last write, after the tested-merge status.
        for sha, context in ((task["tested_sha"], "local-ci/sophgo-cmodel"),
                             (task["head_sha"], "local-ci/summary")):
            self.request(f"statuses/{sha}", "POST", {"state": state, "context": context,
                         "description": description[:140], "target_url": url})

    def status_matches(self, task: dict, state: str, description: str) -> bool:
        """Read the latest status for both contexts; no relay delivery records."""
        for sha, context in ((task["tested_sha"], "local-ci/sophgo-cmodel"),
                             (task["head_sha"], "local-ci/summary")):
            latest = None
            for page in range(1, 21):
                rows = self.request(f"commits/{sha}/statuses?per_page=100&page={page}")
                latest = next((row for row in rows if row.get("context") == context), None)
                if latest or len(rows) < 100:
                    break
            if not latest or latest.get("state") != state or latest.get("description") != description[:140]:
                return False
        return True

    def comment(self, task: dict, body: str) -> bool:
        if not task["pr_number"]:
            return False
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
                return True
        else:
            self.request(path, "POST", content)
            return True
        return False


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
        if pull.get("mergeable") is False:
            raise ValueError("PR cannot be merged cleanly")
        merge = pull.get("merge_commit_sha") or ""
        if not isinstance(merge, str) or not SHA.fullmatch(merge):
            raise ValueError("PR merge result is not ready; retry after GitHub finishes computing it")
        parents = gh.request(f"git/commits/{merge}")["parents"]
        if (len(parents) != 2 or parents[0]["sha"] != pull["base"]["sha"]
                or parents[1]["sha"] != head):
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
        merge = pull.get("merge_commit_sha") or ""
        return bool(pull.get("mergeable") is not False and SHA.fullmatch(merge)
                    and pull["base"].get("sha") == task["base_sha"] and merge == task["tested_sha"])
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


GITHUB_STATES = {"pass": "success", "fail": "failure", "infra_error": "error", "cancelled": "error"}


def publication_description(status: str, result_digest: str) -> str:
    # The digest binds task, run and all evidence without another delivery record.
    return f"Local CI: {status} (result {result_digest})"


def current_task(gh: GitHub, control: GitStore, task: dict) -> bool:
    pointer = control.get(f"current/{current_key(task)}.json")
    return bool(pointer and pointer.get("task_id") == task["task_id"]
                and not control.get(f"cancel/{task['task_id']}.json") and is_current(gh, task))


def retention_marker(path: Path, task: dict) -> dict:
    marker = json.loads(path.read_text())
    if (not RUN_ID.fullmatch(path.stem) or marker.get("task_id") != task["task_id"]
            or marker.get("run_id") != path.stem or marker.get("reason") != "retention_expired"
            or not DIGEST.fullmatch(str(marker.get("result_digest", "")))):
        raise ValueError("Invalid result retention marker")
    return marker


def latest_result(task: dict, results: GitStore) -> tuple[Path | None, dict | None]:
    """Expired latest runs never fall back to an older surviving result."""
    live = {path.parent.name: path for path in (results.root / "runs/v4" / task["task_id"]).glob("*/result.json")}
    expired = {path.stem: path for path in (results.root / "retention/v4" / task["task_id"]).glob("*.json")}
    names = sorted(set(live) | set(expired), reverse=True)
    if not names:
        return None, None
    run_id = names[0]
    if run_id in expired:
        return None, retention_marker(expired[run_id], task)
    return live[run_id], None


def read_result(path: Path, task: dict, results: GitStore) -> tuple[dict, str]:
    raw = path.read_bytes()
    result = validate_result(json.loads(raw), task)
    if path.parent.name != result["run_id"]:
        raise ValueError("Result path/run id mismatch")
    if result["status"] == "pass":
        minimum = trusted_minimum(task, results)
        if not set(minimum["required_checks"]) <= set(result["required_checks"]):
            raise ValueError("Result omitted trusted minimum checks")
    return result, hashlib.sha256(raw).hexdigest()


def publication_error(gh: GitHub, control: GitStore, task: dict) -> None:
    # Failed transports may also prevent this best-effort error status.
    try:
        control.refresh()
        if current_task(gh, control, task):
            gh.status(task, "error", "Local CI result validation/publication failed; receiver will retry")
    except (ValueError, OSError, RuntimeError):
        pass


def collect_results(gh: GitHub, control: GitStore, results: GitStore, dashboard: Path) -> list[dict]:
    """Write current status/comment and stage the dashboard, without a return channel."""
    rows, published = [], []
    for current in sorted((control.root / "current").glob("*.json")):
        pointer = json.loads(current.read_text())
        task = control.get(f"tasks/{pointer['task_id']}.json")
        validate_task(task)
        active = current_task(gh, control, task)
        row = {"task": task, "status": "pending" if active else "cancelled", "result": None}
        try:
            path, expired = latest_result(task, results)
            if expired:
                row.update(status="expired" if active else "cancelled", retention=expired)
            elif path:
                result, result_digest = read_result(path, task, results)
                row.update(status=result["status"] if active else "cancelled", result=result)
                if active and current_task(gh, control, task):
                    state = GITHUB_STATES[result["status"]]
                    description = publication_description(result["status"], result_digest)
                    unchanged = gh.status_matches(task, state, description)
                    if not unchanged:
                        gh.status(task, state, description)
                    control.refresh()
                    if not current_task(gh, control, task):
                        row["status"] = "cancelled"
                        rows.append(row)
                        continue
                    # Even if a previous status succeeded, a failed/deleted comment
                    # still needs repair. Identical comment bodies perform no write.
                    comment_changed = gh.comment(task, result_comment(result))
                    if not unchanged or comment_changed:
                        published.append({"task_id": task["task_id"], "run_id": result["run_id"],
                                          "tested_sha": task["tested_sha"], "result_digest": result_digest, "status": result["status"]})
        except (ValueError, OSError, RuntimeError) as error:
            row.update(status="infra_error", receiver_error=type(error).__name__,
                       receiver_message="结果校验或 GitHub 发布未完成；保留证据并重试接收，不重跑构建。")
            if active:
                publication_error(gh, control, task)
        rows.append(row)
    dashboard.mkdir(parents=True, exist_ok=True)
    (dashboard / "v4-tasks.json").write_bytes(canonical({"schema": "triton-anchor-dashboard/v4", "generated_at": now(), "tasks": rows}) + b"\n")
    output("receiver_errors", sum(bool(row.get("receiver_error")) for row in rows))
    return published


def monitor_tasks(control: GitStore, results: GitStore) -> list[dict]:
    """Only tasks without uploaded valid results can be overdue in the queue."""
    records = []
    for current in sorted((control.root / "current").glob("*.json")):
        task = control.get(f"tasks/{json.loads(current.read_text())['task_id']}.json")
        validate_task(task)
        if control.get(f"cancel/{task['task_id']}.json"):
            continue
        if any(retention_marker(path, task) for path in (results.root / "retention/v4" / task["task_id"]).glob("*.json")):
            continue
        uploaded = False
        for path in (results.root / "runs/v4" / task["task_id"]).glob("*/result.json"):
            try:
                read_result(path, task, results)
                uploaded = True
                break
            except (ValueError, OSError, RuntimeError):
                continue
        if not uploaded:
            records.append({"task_id": task["task_id"], "worker_id": "receiver", "status": "queued", "created_at": task["captured_at"]})
    return records


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
    parser.add_argument("command", choices=("prepare", "info", "card", "approval", "enqueue", "cancel", "collect", "api", "security", "sarif"))
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
                    Path("monitor-tasks.json").write_bytes(canonical(monitor_tasks(control, results)) + b"\n")
                    collect_results(gh, control, results, args.dashboard)
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
        safe_error = (str(error) if isinstance(error, (ValueError, GitHubAPIError))
                      else "inspect the stage logs and transport configuration")
        print(f"CI v4 control failed: {type(error).__name__}: {safe_error}", file=sys.stderr)
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
                        reason = str(error) if isinstance(error, (ValueError, GitHubAPIError)) else type(error).__name__
                        client.comment(context, f"## CI 准备或投递未完成\n\n{html.escape(reason)}\n\n请查看本次工作流证据并修复对应检查或中转配置，然后重试：{link}")
            except (ValueError, OSError, RuntimeError):
                print("PR failure notification could not be delivered; the workflow remains failed.", file=sys.stderr)
        raise SystemExit(1)
