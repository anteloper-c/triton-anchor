#!/usr/bin/env python3
"""Mirror public upstream pull requests into this repository for trusted CI."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

API_ROOT = "https://api.github.com"
DEFAULT_UPSTREAM_REPOSITORY = "RACE-org/triton-anchor"
DEFAULT_ALLOWED_BASE_REFS = ("main", "triton_v3.0", "anchorbase_dev", "CI_dev")
MIRROR_BRANCH_PREFIX = "review/race-pr-"
MARKER_RE = re.compile(r"<!-- upstream-pr-mirror:RACE-org/triton-anchor#([0-9]+) -->")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class MirrorError(RuntimeError):
    """Raised when an upstream PR cannot be mirrored safely."""


class ApiError(MirrorError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API HTTP {status}: {message[:500]}")
        self.status = status


@dataclass(frozen=True)
class UpstreamPullRequest:
    number: int
    title: str
    body: str
    state: str
    html_url: str
    author: str
    base_ref: str
    api_base_sha: str
    head_ref: str
    head_sha: str
    head_repository: str

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "UpstreamPullRequest":
        base = payload.get("base")
        head = payload.get("head")
        user = payload.get("user")
        head_repo = head.get("repo") if isinstance(head, dict) else None
        values = {
            "number": payload.get("number"),
            "title": payload.get("title"),
            "state": payload.get("state"),
            "html_url": payload.get("html_url"),
            "author": user.get("login") if isinstance(user, dict) else None,
            "base_ref": base.get("ref") if isinstance(base, dict) else None,
            "api_base_sha": base.get("sha") if isinstance(base, dict) else None,
            "head_ref": head.get("ref") if isinstance(head, dict) else None,
            "head_sha": head.get("sha") if isinstance(head, dict) else None,
            "head_repository": (
                head_repo.get("full_name") if isinstance(head_repo, dict) else None
            ),
        }
        if not isinstance(values["number"], int) or values["number"] < 1:
            raise MirrorError("upstream PR number is invalid")
        for key in (
            "title",
            "state",
            "html_url",
            "author",
            "base_ref",
            "api_base_sha",
            "head_ref",
            "head_sha",
            "head_repository",
        ):
            if not isinstance(values[key], str) or not values[key]:
                raise MirrorError(f"upstream PR field is missing or invalid: {key}")
        if not SHA_RE.fullmatch(values["api_base_sha"]):
            raise MirrorError("upstream PR base SHA is invalid")
        if not SHA_RE.fullmatch(values["head_sha"]):
            raise MirrorError("upstream PR head SHA is invalid")
        body = payload.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise MirrorError("upstream PR body is invalid")
        return cls(body=body, **values)


def mirror_branch_names(number: int) -> Tuple[str, str]:
    if number < 1:
        raise MirrorError("PR number must be positive")
    prefix = f"{MIRROR_BRANCH_PREFIX}{number}"
    return f"{prefix}/base", f"{prefix}/head"


def mirror_marker(number: int) -> str:
    return f"<!-- upstream-pr-mirror:{DEFAULT_UPSTREAM_REPOSITORY}#{number} -->"


def mirrored_upstream_number(payload: Dict[str, Any]) -> Optional[int]:
    body = payload.get("body")
    if not isinstance(body, str):
        return None
    match = MARKER_RE.search(body)
    return int(match.group(1)) if match else None


def parse_allowed_base_refs(value: str) -> Optional[Tuple[str, ...]]:
    normalized = value.strip()
    if not normalized:
        return DEFAULT_ALLOWED_BASE_REFS
    if normalized in {"*", "all", "ALL"}:
        return None
    refs: List[str] = []
    for item in re.split(r"[\s,]+", normalized):
        ref = item.strip()
        if not ref:
            continue
        if ref.startswith("-") or ".." in ref or ref.endswith(".lock"):
            raise MirrorError(f"invalid allowed base ref: {ref}")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", ref):
            raise MirrorError(f"invalid allowed base ref: {ref}")
        if ref not in refs:
            refs.append(ref)
    if not refs:
        return DEFAULT_ALLOWED_BASE_REFS
    return tuple(refs)


def format_allowed_base_refs(allowed: Optional[Tuple[str, ...]]) -> str:
    if allowed is None:
        return "all"
    return ", ".join(f"`{ref}`" for ref in allowed)


def mirror_title(pr: UpstreamPullRequest) -> str:
    prefix = f"[RACE-org #{pr.number}] "
    return (prefix + pr.title).replace("\r", " ").replace("\n", " ")[:256]


def mirror_body(pr: UpstreamPullRequest, live_base_sha: str) -> str:
    original_body = pr.body.strip()
    if len(original_body) > 30000:
        original_body = original_body[:30000] + "\n\n[原始描述已截断]"
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    description = original_body or "_原 PR 未提供描述。_"
    return (
        f"{mirror_marker(pr.number)}\n\n"
        "> 此 PR 由 fork 自动镜像，用于运行受信任的 AI/Local CI 审核。"
        "审核结果仅发布在本镜像 PR。\n\n"
        f"- 原 PR：[{DEFAULT_UPSTREAM_REPOSITORY}#{pr.number}]({pr.html_url})\n"
        f"- 作者：`{pr.author}`\n"
        f"- 目标分支：`{pr.base_ref}`\n"
        f"- 镜像 base SHA：`{live_base_sha}`\n"
        f"- 原 API base SHA：`{pr.api_base_sha}`\n"
        f"- 来源：`{pr.head_repository}:{pr.head_ref}`\n"
        f"- head SHA：`{pr.head_sha}`\n"
        f"- 镜像更新时间：`{captured_at}`\n\n"
        "## 原 PR 描述\n\n"
        f"{description}\n"
    )


def api_request(
    method: str,
    path: str,
    token: str = "",
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, str]] = None,
) -> Any:
    url = f"{API_ROOT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "triton-anchor-upstream-mirror",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise ApiError(exc.code, raw) from exc


class GitHubClient:
    def __init__(
        self, upstream_repository: str, mirror_repository: str, token: str
    ) -> None:
        if not REPOSITORY_RE.fullmatch(upstream_repository):
            raise MirrorError("invalid upstream repository")
        if not REPOSITORY_RE.fullmatch(mirror_repository):
            raise MirrorError("invalid mirror repository")
        self.upstream_repository = upstream_repository
        self.mirror_repository = mirror_repository
        self.mirror_owner = mirror_repository.split("/", 1)[0]
        self.token = token

    def upstream_pr(self, number: int) -> UpstreamPullRequest:
        payload = api_request(
            "GET", f"/repos/{self.upstream_repository}/pulls/{number}"
        )
        if not isinstance(payload, dict):
            raise MirrorError(f"invalid upstream PR response: {number}")
        return UpstreamPullRequest.from_payload(payload)

    def open_upstream_prs(self) -> List[UpstreamPullRequest]:
        result: List[UpstreamPullRequest] = []
        page = 1
        while True:
            payload = api_request(
                "GET",
                f"/repos/{self.upstream_repository}/pulls",
                params={"state": "open", "per_page": "100", "page": str(page)},
            )
            if not isinstance(payload, list):
                raise MirrorError("invalid upstream PR list response")
            result.extend(
                UpstreamPullRequest.from_payload(item)
                for item in payload
                if isinstance(item, dict)
            )
            if len(payload) < 100:
                return result
            page += 1

    def find_mirror_pr(self, upstream_number: int) -> Optional[Dict[str, Any]]:
        _, head_branch = mirror_branch_names(upstream_number)
        payload = api_request(
            "GET",
            f"/repos/{self.mirror_repository}/pulls",
            token=self.token,
            params={
                "state": "all",
                "head": f"{self.mirror_owner}:{head_branch}",
                "per_page": "100",
            },
        )
        if not isinstance(payload, list):
            raise MirrorError("invalid mirror PR list response")
        candidates = [
            item
            for item in payload
            if isinstance(item, dict)
            and mirrored_upstream_number(item) == upstream_number
        ]
        candidates.sort(key=lambda item: item.get("state") != "open")
        return candidates[0] if candidates else None

    def open_mirror_prs(self) -> List[Dict[str, Any]]:
        payload = api_request(
            "GET",
            f"/repos/{self.mirror_repository}/pulls",
            token=self.token,
            params={"state": "open", "per_page": "100"},
        )
        if not isinstance(payload, list):
            raise MirrorError("invalid open mirror PR response")
        return [
            item
            for item in payload
            if isinstance(item, dict) and mirrored_upstream_number(item) is not None
        ]

    def create_mirror_pr(
        self, pr: UpstreamPullRequest, live_base_sha: str
    ) -> Dict[str, Any]:
        base_branch, head_branch = mirror_branch_names(pr.number)
        payload = api_request(
            "POST",
            f"/repos/{self.mirror_repository}/pulls",
            token=self.token,
            payload={
                "title": mirror_title(pr),
                "head": head_branch,
                "base": base_branch,
                "body": mirror_body(pr, live_base_sha),
            },
        )
        if not isinstance(payload, dict):
            raise MirrorError("invalid mirror PR creation response")
        return payload

    def update_mirror_pr(
        self, mirror_number: int, pr: UpstreamPullRequest, live_base_sha: str
    ) -> None:
        base_branch, _ = mirror_branch_names(pr.number)
        api_request(
            "PATCH",
            f"/repos/{self.mirror_repository}/pulls/{mirror_number}",
            token=self.token,
            payload={
                "title": mirror_title(pr),
                "base": base_branch,
                "body": mirror_body(pr, live_base_sha),
                "state": "open",
            },
        )

    def close_mirror_pr(self, payload: Dict[str, Any], reason: str) -> None:
        number = payload.get("number")
        if not isinstance(number, int):
            raise MirrorError("mirror PR number is invalid")
        api_request(
            "POST",
            f"/repos/{self.mirror_repository}/issues/{number}/comments",
            token=self.token,
            payload={"body": reason},
        )
        api_request(
            "PATCH",
            f"/repos/{self.mirror_repository}/pulls/{number}",
            token=self.token,
            payload={"state": "closed"},
        )

    def delete_branch(self, branch: str) -> None:
        encoded = urllib.parse.quote(f"heads/{branch}", safe="/")
        try:
            api_request(
                "DELETE",
                f"/repos/{self.mirror_repository}/git/refs/{encoded}",
                token=self.token,
            )
        except ApiError as exc:
            if exc.status != 404:
                raise


class GitRunner:
    def __init__(self, upstream_repository: str, mirror_remote: str = "origin") -> None:
        self.upstream_url = f"https://github.com/{upstream_repository}.git"
        self.mirror_remote = mirror_remote

    @staticmethod
    def run(
        args: Sequence[str], check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            raise MirrorError(
                f"git {' '.join(args)} failed ({result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result

    def fetch_exact(self, pr: UpstreamPullRequest) -> Tuple[str, str]:
        self.run(["check-ref-format", f"refs/heads/{pr.base_ref}"])
        base_local = f"refs/mirror/upstream/pr-{pr.number}/base"
        head_local = f"refs/mirror/upstream/pr-{pr.number}/head"
        self.run(
            [
                "fetch",
                "--no-tags",
                "--force",
                self.upstream_url,
                f"+refs/heads/{pr.base_ref}:{base_local}",
                f"+refs/pull/{pr.number}/head:{head_local}",
            ]
        )
        base_sha = self.run(["rev-parse", base_local]).stdout.strip()
        head_sha = self.run(["rev-parse", head_local]).stdout.strip()
        if not SHA_RE.fullmatch(base_sha) or not SHA_RE.fullmatch(head_sha):
            raise MirrorError("fetched upstream refs did not resolve to commit SHAs")
        return base_sha, head_sha

    def mergeable(self, base_sha: str, head_sha: str) -> Tuple[bool, str]:
        result = self.run(
            ["merge-tree", "--write-tree", base_sha, head_sha], check=False
        )
        output = "\n".join(
            line for line in (result.stdout + result.stderr).splitlines()[1:] if line
        )
        return result.returncode == 0, output[:4000]

    def has_changes(self, base_sha: str, head_sha: str) -> bool:
        result = self.run(
            ["diff", "--quiet", f"{base_sha}...{head_sha}", "--"], check=False
        )
        if result.returncode not in (0, 1):
            raise MirrorError(f"git diff failed: {result.stderr.strip()}")
        return result.returncode == 1

    def remote_sha(self, branch: str) -> str:
        result = self.run(["ls-remote", self.mirror_remote, f"refs/heads/{branch}"])
        line = result.stdout.strip()
        if not line:
            return ""
        sha = line.split()[0]
        if not SHA_RE.fullmatch(sha):
            raise MirrorError(f"invalid remote SHA for {branch}")
        return sha

    def push_refs(self, refs: Iterable[Tuple[str, str]]) -> bool:
        changes: List[Tuple[str, str, str]] = []
        for branch, sha in refs:
            old_sha = self.remote_sha(branch)
            if old_sha != sha:
                changes.append((branch, sha, old_sha))
        if not changes:
            return False
        args = ["push", "--atomic", self.mirror_remote]
        for branch, _, old_sha in changes:
            args.append(f"--force-with-lease=refs/heads/{branch}:{old_sha}")
        args.extend(f"{sha}:refs/heads/{branch}" for branch, sha, _ in changes)
        self.run(args)
        return True


class MirrorService:
    def __init__(
        self,
        github: GitHubClient,
        git: GitRunner,
        dry_run: bool = False,
        allowed_base_refs: Optional[Tuple[str, ...]] = DEFAULT_ALLOWED_BASE_REFS,
    ) -> None:
        self.github = github
        self.git = git
        self.dry_run = dry_run
        self.allowed_base_refs = allowed_base_refs

    def base_ref_allowed(self, pr: UpstreamPullRequest) -> bool:
        return self.allowed_base_refs is None or pr.base_ref in self.allowed_base_refs

    def skip_pr_for_base_ref(self, pr: UpstreamPullRequest) -> str:
        existing = self.github.find_mirror_pr(pr.number)
        if existing and existing.get("state") == "open" and not self.dry_run:
            reason = (
                f"自动镜像暂停：上游 PR #{pr.number} 的目标分支 `{pr.base_ref}` "
                f"当前不在 fork CI 分支白名单（{format_allowed_base_refs(self.allowed_base_refs)}）。"
                "恢复白名单后镜像任务会自动重新打开本 PR。"
            )
            self.github.close_mirror_pr(existing, reason)
        print(
            f"PR #{pr.number}: skipped; base={pr.base_ref}; "
            f"allowed={format_allowed_base_refs(self.allowed_base_refs)}"
        )
        return "skipped_base_ref"

    def sync_pr(self, pr: UpstreamPullRequest) -> str:
        if not self.base_ref_allowed(pr):
            return self.skip_pr_for_base_ref(pr)

        base_sha, head_sha = self.git.fetch_exact(pr)
        if head_sha != pr.head_sha:
            raise MirrorError(
                f"PR #{pr.number} head moved during mirroring: "
                f"API={pr.head_sha}, fetched={head_sha}"
            )
        existing = self.github.find_mirror_pr(pr.number)
        mergeable, conflict_summary = self.git.mergeable(base_sha, head_sha)
        if not mergeable:
            if existing and existing.get("state") == "open" and not self.dry_run:
                reason = (
                    f"自动镜像暂停：上游 PR #{pr.number} 与最新 `{pr.base_ref}` "
                    "存在合并冲突。冲突解决后镜像任务会自动重新打开本 PR。"
                )
                self.github.close_mirror_pr(existing, reason)
            print(f"::warning::PR #{pr.number} is not mergeable: {conflict_summary}")
            return "conflict"
        if not self.git.has_changes(base_sha, head_sha):
            if existing and existing.get("state") == "open" and not self.dry_run:
                self.github.close_mirror_pr(
                    existing,
                    f"自动镜像关闭：上游 PR #{pr.number} 已不包含相对目标分支的变更。",
                )
            return "no_changes"

        base_branch, head_branch = mirror_branch_names(pr.number)
        pushed = False
        if not self.dry_run:
            pushed = self.git.push_refs(
                ((base_branch, base_sha), (head_branch, head_sha))
            )
            if existing:
                mirror_number = existing.get("number")
                if not isinstance(mirror_number, int):
                    raise MirrorError("existing mirror PR number is invalid")
                self.github.update_mirror_pr(mirror_number, pr, base_sha)
            else:
                self.github.create_mirror_pr(pr, base_sha)
        action = (
            "would_sync" if self.dry_run else ("updated" if pushed else "unchanged")
        )
        print(f"PR #{pr.number}: {action}; base={base_sha[:12]} head={head_sha[:12]}")
        return action

    def close_upstream_pr(self, upstream_number: int, reason: str) -> None:
        existing = self.github.find_mirror_pr(upstream_number)
        if not existing:
            return
        if existing.get("state") == "open" and not self.dry_run:
            self.github.close_mirror_pr(existing, reason)
        if not self.dry_run:
            for branch in mirror_branch_names(upstream_number):
                self.github.delete_branch(branch)

    def reconcile_closed(self, open_numbers: Iterable[int]) -> None:
        active = set(open_numbers)
        for mirror_pr in self.github.open_mirror_prs():
            upstream_number = mirrored_upstream_number(mirror_pr)
            if upstream_number is None or upstream_number in active:
                continue
            upstream = self.github.upstream_pr(upstream_number)
            if upstream.state == "open":
                continue
            self.close_upstream_pr(
                upstream_number,
                f"自动镜像关闭：上游 PR #{upstream_number} 已{upstream.state}。",
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-repository", default=DEFAULT_UPSTREAM_REPOSITORY)
    parser.add_argument(
        "--mirror-repository", default=os.getenv("GITHUB_REPOSITORY", "")
    )
    parser.add_argument("--pr-number", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allowed-base-refs",
        default=os.getenv(
            "MIRROR_UPSTREAM_ALLOWED_BASE_REFS",
            ",".join(DEFAULT_ALLOWED_BASE_REFS),
        ),
        help=(
            "Comma or whitespace separated upstream base refs to mirror; "
            "use '*' or 'all' for every branch."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.getenv("FORK_AUTOMATION_TOKEN", "")
    if not args.dry_run and not token:
        print("FORK_AUTOMATION_TOKEN is required", file=sys.stderr)
        return 2
    try:
        allowed_base_refs = parse_allowed_base_refs(args.allowed_base_refs)
        github = GitHubClient(args.upstream_repository, args.mirror_repository, token)
        service = MirrorService(
            github,
            GitRunner(args.upstream_repository),
            args.dry_run,
            allowed_base_refs,
        )
        if args.pr_number:
            pr = github.upstream_pr(args.pr_number)
            if pr.state == "open":
                service.sync_pr(pr)
            else:
                service.close_upstream_pr(
                    pr.number, f"自动镜像关闭：上游 PR #{pr.number} 已{pr.state}。"
                )
            return 0

        pull_requests = github.open_upstream_prs()
        failures = 0
        for pr in pull_requests:
            try:
                service.sync_pr(pr)
            except MirrorError as exc:
                print(f"::error title=Mirror PR #{pr.number}::{exc}", file=sys.stderr)
                failures += 1
        if not args.dry_run:
            service.reconcile_closed(pr.number for pr in pull_requests)
        if failures:
            print(f"{failures} upstream PR mirror operation(s) failed", file=sys.stderr)
            return 1
        return 0
    except MirrorError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
