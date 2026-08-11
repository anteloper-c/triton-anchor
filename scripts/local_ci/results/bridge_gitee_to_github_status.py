#!/usr/bin/env python3
"""Bridge Gitee local-ci results back to GitHub commit statuses."""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SHARED_SCRIPT_DIR = Path(__file__).resolve().parents[1] / "shared"
if str(SHARED_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPT_DIR))
from finding_locations import (  # noqa: E402
    normalized_repository_path,
    parse_finding_line_range,
)
from result_paths import (  # noqa: E402
    gitee_blob_url,
    gitee_tree_url,
    result_commit_dir,
)


RESULT_NOT_READY_EXIT_CODE = 3
RESULT_FAILED_EXIT_CODE = 10
CODEX_COMMENT_MARKER = "<!-- triton-anchor-codex-ai-comment -->"
CODEX_COMMENT_SHA_MARKER_PREFIX = "triton-anchor-codex-ai-comment-sha"
FINDING_ID_RE = re.compile(r"^AI-[0-9]{3}$")
GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
INTERNAL_COMMENT_ID_RE = re.compile(
    r"\b(AI|TEST|RUN)-0*([1-9][0-9]*)\b[ \t]*",
    re.IGNORECASE,
)
PUBLIC_COMMENT_ID_TEMPLATES = {
    "AI": "问题 {number}",
    "TEST": "建议测试 {number}",
    "RUN": "相关验证",
}
INTERNAL_COMMENT_TERM_REPLACEMENTS = (
    (
        re.compile(r"\bCodex AI (?:CI|代码审查)\b[ \t]*", re.IGNORECASE),
        "Codex AI 自动审查",
    ),
    (
        re.compile(r"(?:确定性[ \t]+)?\bLocal CI\b[ \t]*", re.IGNORECASE),
        "本地确定性 CI 检查",
    ),
    (
        re.compile(r"(?<!本地)确定性 CI(?![ \t]*检查)[ \t]*"),
        "本地确定性 CI 检查",
    ),
)
CODEX_EXECUTION_STATUS_LABELS = {
    "pass": "完成",
    "fail": "未完成",
    "skipped": "未运行",
    "not_reported": "未报告",
}
CODEX_VERDICT_LABELS = {
    "pass": "通过",
    "warning": "警告",
    "fail": "失败",
    "not_run": "未运行",
    "unknown": "未知",
}
CODEX_TEST_STATUS_LABELS = {
    "not_run": "未运行",
    "passed": "通过",
    "stable_failure": "存在可稳定复现的失败",
    "flaky_failure": "存在非确定性失败",
    "infrastructure_failure": "基础设施失败",
    "test_generation_error": "测试生成失败",
    "insufficient_evidence": "证据不足",
    "not_reported": "未报告",
}
CODEX_FAILURE_REASON_LABELS = {
    "codex_cli_unavailable": "Codex AI 自动审查工具在当前环境中不可用",
    "credential_validation_failed": "Codex 审查凭据校验未通过",
    "prompt_render_failed": "Codex 审查输入准备失败",
    "timeout": "Codex 自动审查执行超时",
    "missing_completion_marker": "Codex 自动审查没有完整结束",
    "missing_turn_completed": "Codex 自动审查没有完整结束",
    "no_command_executed": "Codex 自动审查没有获得可核验的补充验证结果",
    "schema_validation_failed": "Codex 审查结果格式校验未通过",
    "invalid_finding_location": "Codex 问题定位信息校验未通过",
    "container_setup_failed": "Codex 审查运行环境启动失败",
    "checkout_or_diff_failed": "Codex 审查代码或差异准备失败",
    "prerequisite_failed": "Codex 审查运行环境缺少必要组件",
    "codex_execution_failed": "Codex 自动审查执行异常",
}

REPORTABLE_STAGES = (
    ("frontend_smoke", "frontend_smoke_status", "frontend-smoke", "Frontend smoke"),
    ("backend_smoke_jit", "backend_smoke_jit_status", "backend-smoke-jit", "Backend smoke and JIT"),
    ("flaggems", "flaggems_status", "flaggems", "FlagGems"),
    ("compile_time", "compile_time_status", "compile-time", "Compile-time performance"),
    ("pass_profile", "pass_profile_status", "pass-profile", "Pass profiling"),
    ("ir_serialization", "ir_serialization_status", "ir-serialization", "IR serialization"),
)


@dataclass(frozen=True)
class Target:
    source_branch: str
    task_ref: str
    sha: str
    label: str
    source_repository: str = ""


@dataclass(frozen=True)
class FindingLocation:
    identifier: str
    file: str
    line: str


@dataclass(frozen=True)
class CodexAIResult:
    execution_status: str
    verdict: str
    test_status: str
    analysis_mode: str
    constraint_status: str
    constraint_reason: str
    failure_reason: str
    comment_markdown: str
    report_url: str
    finding_locations: tuple[FindingLocation, ...] = ()
    failure_code: str = ""
    validation_purposes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PublishManifest:
    status: str
    target_sha: str
    tested_sha: str
    run_id: str
    missing_expected_files: tuple[str, ...]
    fallback: bool


@dataclass(frozen=True)
class LocalCIResult:
    exit_code: int | None
    target_url: str
    run_id: str
    compile_time_status: str
    pass_profile_status: str
    ir_serialization_status: str
    stage_statuses: dict[str, str]
    codex_ai: CodexAIResult
    publish_manifest: PublishManifest | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gitee-owner", required=True)
    parser.add_argument("--gitee-repo", required=True)
    parser.add_argument("--gitee-results-branch", default="local-ci-results")
    parser.add_argument("--gitee-web-url", required=True)
    parser.add_argument("--source-branch", default="jiwang-delivery-ci")
    parser.add_argument("--reconcile-source-branches", default="")
    parser.add_argument("--task-ref", default="")
    parser.add_argument("--sha", default="")
    parser.add_argument("--context", default="local-ci/sophgo-cmodel")
    parser.add_argument("--mode", choices=("single", "reconcile"), default="single")
    parser.add_argument("--set-pending", action="store_true")
    parser.add_argument("--max-prs", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=int, default=0)
    parser.add_argument("--poll-interval-seconds", type=int, default=0)
    parser.add_argument("--require-result", action="store_true")
    parser.add_argument("--exit-with-result", action="store_true")
    return parser.parse_args()


def request_json(url: str, method: str = "GET", token: str = "", data: dict | None = None) -> tuple[int, object | None, str]:
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(response_body) if response_body else None, response_body
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(response_body) if response_body else None
        except json.JSONDecodeError:
            parsed = None
        return exc.code, parsed, response_body


def gitee_content(owner: str, repo: str, path: str, ref: str, token: str) -> str | None:
    quoted_owner = urllib.parse.quote(owner, safe="")
    quoted_repo = urllib.parse.quote(repo, safe="")
    quoted_path = urllib.parse.quote(path, safe="/")
    params = {"ref": ref}
    if token:
        params["access_token"] = token
    query = urllib.parse.urlencode(params)
    url = f"https://gitee.com/api/v5/repos/{quoted_owner}/{quoted_repo}/contents/{quoted_path}?{query}"
    status, payload, raw = request_json(url)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"Gitee content request failed: HTTP {status}: {raw[:500]}")
    if isinstance(payload, list):
        if not payload:
            return None
        raise RuntimeError(f"Gitee content response is a directory listing, not a file object: {raw[:500]}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Gitee content response is not a file object: {raw[:500]}")

    content = payload.get("content")
    encoding = payload.get("encoding")
    if not isinstance(content, str):
        raise RuntimeError(f"Gitee content response has no content field: {raw[:500]}")
    if encoding == "base64":
        return base64.b64decode(content).decode("utf-8", errors="replace")
    return content


def parse_summary_status(summary: str) -> int | None:
    for line in summary.splitlines():
        if line.startswith("status:"):
            value = line.split(":", 1)[1].strip()
            try:
                return int(value)
            except ValueError:
                return None
    return None


def parse_summary_value(summary: str, key: str) -> str:
    prefix = f"{key}:"
    for line in summary.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def parse_publish_manifest(manifest_json: str, target: Target, run_id: str) -> PublishManifest | None:
    if not manifest_json:
        return None
    try:
        document = json.loads(manifest_json)
    except json.JSONDecodeError:
        print("Gitee local CI publish manifest is not valid JSON.", file=sys.stderr)
        return None
    if not isinstance(document, dict):
        print("Gitee local CI publish manifest root is not an object.", file=sys.stderr)
        return None
    if document.get("schema") != "triton-anchor-local-ci-publish-manifest/v1":
        print("Gitee local CI publish manifest schema is unsupported.", file=sys.stderr)
        return None
    status = document.get("status")
    manifest_target_sha = document.get("target_sha")
    tested_sha = document.get("tested_sha")
    manifest_run_id = document.get("run_id")
    missing = document.get("missing_expected_files", [])
    fallback = document.get("fallback", False)
    if not all(isinstance(value, str) for value in (status, manifest_target_sha, tested_sha, manifest_run_id)):
        print("Gitee local CI publish manifest is missing required string fields.", file=sys.stderr)
        return None
    if manifest_target_sha != target.sha or tested_sha != target.sha or manifest_run_id != run_id:
        print(
            "Gitee local CI publish manifest does not match requested SHA/run; "
            f"target={manifest_target_sha}, tested={tested_sha}, run={manifest_run_id}.",
            file=sys.stderr,
        )
        return None
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        print("Gitee local CI publish manifest has invalid missing_expected_files.", file=sys.stderr)
        missing_files: tuple[str, ...] = ()
    else:
        missing_files = tuple(missing)
    return PublishManifest(status, manifest_target_sha, tested_sha, manifest_run_id, missing_files, bool(fallback))


def finding_locations_from_report(report_json: str) -> tuple[FindingLocation, ...]:
    try:
        document = json.loads(report_json)
    except json.JSONDecodeError:
        return ()
    if not isinstance(document, dict):
        return ()
    findings = document.get("findings")
    if not isinstance(findings, list):
        return ()

    locations: list[FindingLocation] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        identifier = finding.get("id")
        file_name = finding.get("file")
        line = finding.get("line")
        if not all(isinstance(value, str) for value in (identifier, file_name, line)):
            continue
        if (
            not FINDING_ID_RE.fullmatch(identifier)
            or parse_finding_line_range(line) is None
        ):
            continue
        if normalized_repository_path(file_name) is None:
            continue
        locations.append(FindingLocation(identifier, file_name, line))
    return tuple(locations)


def validation_purposes_from_report(report_json: str) -> tuple[tuple[str, str], ...]:
    try:
        document = json.loads(report_json)
    except json.JSONDecodeError:
        return ()
    if not isinstance(document, dict):
        return ()
    test_execution = document.get("test_execution")
    if not isinstance(test_execution, dict):
        return ()
    commands = test_execution.get("commands")
    if not isinstance(commands, list):
        return ()

    purposes: list[tuple[str, str]] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        identifier = command.get("id")
        purpose = command.get("purpose")
        if not isinstance(identifier, str) or not isinstance(purpose, str):
            continue
        purpose = purpose.strip()
        if re.fullmatch(r"RUN-[0-9]{3}", identifier) and purpose:
            purposes.append((identifier, purpose))
    return tuple(purposes)


def github_api_url(path: str, params: dict[str, str] | None = None) -> str:
    url = f"https://api.github.com{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return url


def github_repo() -> str:
    return os.environ["GITHUB_REPOSITORY"]


def github_token() -> str:
    return os.environ["GITHUB_TOKEN"]


def github_status_url(sha: str) -> str:
    return github_api_url(f"/repos/{github_repo()}/statuses/{sha}")


def post_github_status(sha: str, state: str, context: str, description: str, target_url: str = "") -> None:
    payload = {
        "state": state,
        "context": context,
        "description": description[:140],
    }
    if target_url:
        payload["target_url"] = target_url

    status, _, raw = request_json(github_status_url(sha), method="POST", token=github_token(), data=payload)
    if status not in (200, 201):
        raise RuntimeError(f"GitHub status update failed: HTTP {status}: {raw[:500]}")


def get_github_json(path: str, params: dict[str, str] | None = None) -> object | None:
    status, payload, raw = request_json(github_api_url(path, params), token=github_token())
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"GitHub API request failed: HTTP {status}: {raw[:500]}")
    return payload


def github_branch_head(branch: str) -> str | None:
    quoted_branch = urllib.parse.quote(branch, safe="")
    payload = get_github_json(f"/repos/{github_repo()}/branches/{quoted_branch}")
    if not isinstance(payload, dict):
        return None
    commit = payload.get("commit")
    if not isinstance(commit, dict):
        return None
    sha = commit.get("sha")
    return sha if isinstance(sha, str) else None


def list_open_pr_targets(limit: int) -> list[Target]:
    targets: list[Target] = []
    page = 1
    per_page = min(max(limit, 1), 100)
    while len(targets) < limit:
        payload = get_github_json(
            f"/repos/{github_repo()}/pulls",
            {"state": "open", "per_page": str(per_page), "page": str(page)},
        )
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if len(targets) >= limit:
                break
            if not isinstance(item, dict):
                continue
            head = item.get("head")
            if not isinstance(head, dict):
                continue
            repo = head.get("repo")
            head_repo = repo.get("full_name") if isinstance(repo, dict) else ""
            if not isinstance(head_repo, str):
                head_repo = ""
            branch = head.get("ref")
            head_sha = head.get("sha")
            merge_sha = item.get("merge_commit_sha")
            number = item.get("number")
            if (
                isinstance(branch, str)
                and isinstance(head_sha, str)
                and isinstance(merge_sha, str)
                and re.fullmatch(r"[0-9a-f]{40}", merge_sha)
                and isinstance(number, int)
            ):
                source_label = f"{head_repo}:{branch}" if head_repo and head_repo != github_repo() else branch
                targets.append(
                    Target(
                        branch,
                        f"ci/pr-{number}/{branch}",
                        merge_sha,
                        f"PR #{number} {source_label}",
                        github_repo(),
                    )
                )
        if len(payload) < per_page:
            break
        page += 1
    return targets


def gitee_result_url(web_url: str, results_branch: str, rel_dir: str) -> str:
    return gitee_tree_url(web_url, results_branch, rel_dir)


def gitee_file_url(web_url: str, results_branch: str, rel_path: str) -> str:
    return gitee_blob_url(web_url, results_branch, rel_path)


def read_local_ci_result(args: argparse.Namespace, target: Target, gitee_token: str) -> LocalCIResult | None:
    commit_dir = result_commit_dir(target.task_ref, target.sha).as_posix()
    latest_path = f"{commit_dir}/latest.txt"
    run_id_text = gitee_content(
        args.gitee_owner,
        args.gitee_repo,
        latest_path,
        args.gitee_results_branch,
        gitee_token,
    )
    if not run_id_text:
        print(f"No Gitee local CI result yet for {target.label} ({target.task_ref}): {latest_path}")
        return None

    run_id = run_id_text.strip().splitlines()[0]
    rel_dir = f"{commit_dir}/{run_id}"
    manifest_text = gitee_content(
        args.gitee_owner,
        args.gitee_repo,
        f"{rel_dir}/publish-manifest.json",
        args.gitee_results_branch,
        gitee_token,
    ) or ""
    publish_manifest = parse_publish_manifest(manifest_text, target, run_id)
    if manifest_text and publish_manifest is None:
        print(f"Gitee local CI publish manifest is invalid for {target.label}; leaving pending.")
        return None

    summary_path = f"{rel_dir}/delivery-summary.txt"
    summary = gitee_content(
        args.gitee_owner,
        args.gitee_repo,
        summary_path,
        args.gitee_results_branch,
        gitee_token,
    )
    if not summary:
        print(f"Gitee local CI run exists but summary is missing for {target.label}: {summary_path}")
        return None
    summary_target_sha = parse_summary_value(summary, "target_sha") or parse_summary_value(summary, "tested_sha")
    summary_run_id = parse_summary_value(summary, "run_id")
    if summary_target_sha and summary_target_sha != target.sha:
        print(
            f"Gitee local CI summary SHA mismatch for {target.label}: {summary_target_sha} != {target.sha}",
            file=sys.stderr,
        )
        return None
    if summary_run_id and summary_run_id != run_id:
        print(
            f"Gitee local CI summary run_id mismatch for {target.label}: {summary_run_id} != {run_id}",
            file=sys.stderr,
        )
        return None

    stage_statuses = {
        stage_id: parse_summary_value(summary, summary_key)
        for stage_id, summary_key, _, _ in REPORTABLE_STAGES
    }
    result_json_text = gitee_content(
        args.gitee_owner,
        args.gitee_repo,
        f"{rel_dir}/result.json",
        args.gitee_results_branch,
        gitee_token,
    ) or ""
    codex_summary = gitee_content(
        args.gitee_owner,
        args.gitee_repo,
        f"{rel_dir}/codex-ai-ci-summary.txt",
        args.gitee_results_branch,
        gitee_token,
    ) or ""
    codex_comment = gitee_content(
        args.gitee_owner,
        args.gitee_repo,
        f"{rel_dir}/codex-ai-comment.md",
        args.gitee_results_branch,
        gitee_token,
    ) or ""
    codex_report = gitee_content(
        args.gitee_owner,
        args.gitee_repo,
        f"{rel_dir}/codex-ai-report.json",
        args.gitee_results_branch,
        gitee_token,
    ) or ""

    codex_document: dict[str, object] = {}
    parse_failure = ""
    if result_json_text:
        try:
            candidate = json.loads(result_json_text)
            if isinstance(candidate, dict):
                codex_document = candidate
                result_target_sha = candidate.get("target_sha") or candidate.get("tested_sha") or candidate.get("sha")
                if isinstance(result_target_sha, str) and result_target_sha and result_target_sha != target.sha:
                    print(
                        f"Gitee local CI result.json SHA mismatch for {target.label}: {result_target_sha} != {target.sha}",
                        file=sys.stderr,
                    )
                    return None
            else:
                parse_failure = "result.json 的根节点不是 JSON 对象。"
        except json.JSONDecodeError:
            parse_failure = "result.json 不是有效的 JSON。"

    def document_string(key: str, default: str) -> str:
        value = codex_document.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else default

    execution_status = document_string("codex_ai_ci_status", "not_reported")
    verdict = document_string("codex_ai_ci_verdict", "UNKNOWN")
    test_status = document_string("codex_ai_test_status", "UNKNOWN")
    failure_code = document_string("codex_ai_failure_code", "")
    analysis_mode = document_string("codex_ai_ci_mode", "not_run")
    constraint_status = "not_reported"
    constraint_reason = "未找到 Codex AI 约束校验结果。"
    failure_reason = parse_failure

    if codex_summary:
        execution_status = parse_summary_value(codex_summary, "status") or execution_status
        verdict = parse_summary_value(codex_summary, "report_verdict") or verdict
        test_status = (
            parse_summary_value(codex_summary, "test_execution_status") or test_status
        )
        analysis_mode = parse_summary_value(codex_summary, "analysis_mode") or analysis_mode
        constraint_status = (
            parse_summary_value(codex_summary, "constraint_status")
            or constraint_status
        )
        constraint_reason = (
            parse_summary_value(codex_summary, "constraint_reason")
            or constraint_reason
        )
        failure_code = parse_summary_value(codex_summary, "failure_code") or failure_code
        failure_reason = (
            parse_summary_value(codex_summary, "failure_reason") or failure_reason
        )

    if execution_status == "skipped":
        if constraint_status == "not_reported":
            constraint_status = "not_applicable"
            constraint_reason = "本次任务未运行 Codex AI CI，约束校验不适用。"
        failure_reason = failure_reason or "本次任务未运行 Codex AI CI。"
    elif execution_status == "not_reported":
        failure_reason = failure_reason or "未找到 Codex AI CI 结果。"

    has_report = bool(codex_summary or codex_comment or codex_report) or execution_status in {"pass", "fail"}
    report_url = (
        gitee_file_url(
            args.gitee_web_url,
            args.gitee_results_branch,
            f"{rel_dir}/codex-ai-report.md",
        )
        if has_report
        else ""
    )
    codex_ai = CodexAIResult(
        execution_status,
        verdict,
        test_status,
        analysis_mode,
        constraint_status,
        constraint_reason,
        failure_reason,
        codex_comment.strip(),
        report_url,
        finding_locations_from_report(codex_report),
        failure_code,
        validation_purposes_from_report(codex_report),
    )
    return LocalCIResult(
        parse_summary_status(summary),
        gitee_result_url(args.gitee_web_url, args.gitee_results_branch, rel_dir),
        run_id,
        stage_statuses["compile_time"],
        stage_statuses["pass_profile"],
        stage_statuses["ir_serialization"],
        stage_statuses,
        codex_ai,
        publish_manifest,
    )


def stage_github_state(status: str) -> str | None:
    normalized = status.strip().lower()
    if normalized in {"pass", "success", "warning"}:
        return "success"
    if normalized in {"fail", "failure", "error", "timeout", "aborted"}:
        return "failure"
    return None


def public_comment_identifier(
    match: re.Match[str], identifier_descriptions: dict[str, str]
) -> str:
    number = int(match.group(2))
    identifier = f"{match.group(1).upper()}-{number:03d}"
    if identifier in identifier_descriptions:
        return identifier_descriptions[identifier]
    template = PUBLIC_COMMENT_ID_TEMPLATES[match.group(1).upper()]
    return template.format(number=number)


def public_comment_text(
    text: str, validation_purposes: tuple[tuple[str, str], ...] = ()
) -> str:
    identifier_descriptions = dict(validation_purposes)
    public_text = INTERNAL_COMMENT_ID_RE.sub(
        lambda match: public_comment_identifier(match, identifier_descriptions),
        text,
    )
    for pattern, replacement in INTERNAL_COMMENT_TERM_REPLACEMENTS:
        public_text = pattern.sub(replacement, public_text)
    return public_text


def public_status_label(value: str, labels: dict[str, str]) -> str:
    return labels.get(value.strip().lower(), "未知")


def public_failure_reason(failure_code: str) -> str:
    return CODEX_FAILURE_REASON_LABELS.get(
        failure_code.strip().lower(), "Codex 自动审查执行异常"
    )


def pr_number_from_task_ref(task_ref: str) -> int | None:
    match = re.fullmatch(r"ci/pr-([0-9]+)/.+", task_ref)
    return int(match.group(1)) if match else None


def codex_pr_commit_marker(target: Target) -> str:
    return f"<!-- {CODEX_COMMENT_SHA_MARKER_PREFIX}:{target.sha} -->"


def codex_pr_comment_body(target: Target, result: LocalCIResult) -> str:
    body = public_comment_text(
        result.codex_ai.comment_markdown.strip(),
        result.codex_ai.validation_purposes,
    )
    if not body:
        return ""
    report_url = result.codex_ai.report_url or result.target_url
    location_links = github_finding_location_links(target, result.codex_ai.finding_locations)
    if location_links:
        validation_heading = "\n### 验证情况\n"
        if validation_heading in body:
            body = body.replace(
                validation_heading,
                f"\n{location_links}\n\n### 验证情况\n",
                1,
            )
        else:
            body = f"{body}\n\n{location_links}"
    execution_label = public_status_label(
        result.codex_ai.execution_status, CODEX_EXECUTION_STATUS_LABELS
    )
    verdict_label = public_status_label(result.codex_ai.verdict, CODEX_VERDICT_LABELS)
    test_label = public_status_label(
        result.codex_ai.test_status, CODEX_TEST_STATUS_LABELS
    )
    metadata_lines = [
        f"- 测试提交：`{target.sha[:12]}`",
        (
            f"- Codex 自动审查状态：{execution_label}；结论：{verdict_label}；"
            f"验证情况：{test_label}"
        ),
    ]
    if result.codex_ai.failure_code:
        metadata_lines.append(
            f"- 未完成原因：{public_failure_reason(result.codex_ai.failure_code)}"
        )
    if result.publish_manifest and result.publish_manifest.missing_expected_files:
        missing = ", ".join(result.publish_manifest.missing_expected_files)
        metadata_lines.append(f"- 结果发布提醒：缺少预期结果文件 `{missing}`")
    metadata_lines.append(f"- [查看完整 Codex AI 自动审查报告]({report_url})")
    return (
        f"{body}\n\n"
        f"---\n\n"
        f"{chr(10).join(metadata_lines)}\n\n"
        f"{CODEX_COMMENT_MARKER}\n"
        f"{codex_pr_commit_marker(target)}\n"
    )


def github_finding_location_links(
    target: Target, locations: tuple[FindingLocation, ...]
) -> str:
    repository = target.source_repository or os.getenv("GITHUB_REPOSITORY", "")
    if not GITHUB_REPOSITORY_RE.fullmatch(repository):
        repository = os.getenv("GITHUB_REPOSITORY", "")
    if not repository or not locations:
        return ""
    lines = [
        "### 可点击代码定位",
        "",
        "链接固定到本次测试提交，便于提交者修复和审核者核对代码功能。",
        "",
    ]
    for location in locations[:5]:
        line_range = parse_finding_line_range(location.line)
        if line_range is None:
            continue
        start, end = line_range
        anchor = f"#L{start}" if end == start else f"#L{start}-L{end}"
        quoted_path = urllib.parse.quote(location.file, safe="/")
        url = f"https://github.com/{repository}/blob/{target.sha}/{quoted_path}{anchor}"
        label = location.file.replace("`", "'").replace("@", "＠")
        identifier = public_comment_text(location.identifier)
        lines.append(f"- {identifier}：[{label}:L{location.line}]({url})")
    return "\n".join(lines) if len(lines) > 4 else ""


def post_codex_pr_comment(target: Target, result: LocalCIResult) -> None:
    pr_number = pr_number_from_task_ref(target.task_ref)
    body = codex_pr_comment_body(target, result)
    if pr_number is None or not body:
        return

    commit_marker = codex_pr_commit_marker(target)
    comments_path = f"/repos/{github_repo()}/issues/{pr_number}/comments"
    comments = get_github_json(comments_path, {"per_page": "100"})
    if not isinstance(comments, list):
        comments = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        comment_body = comment.get("body")
        comment_id = comment.get("id")
        comment_user = comment.get("user")
        is_bot = isinstance(comment_user, dict) and comment_user.get("type") == "Bot"
        if (
            isinstance(comment_body, str)
            and CODEX_COMMENT_MARKER in comment_body
            and commit_marker in comment_body
            and isinstance(comment_id, int)
            and is_bot
        ):
            status, _, raw = request_json(
                github_api_url(f"/repos/{github_repo()}/issues/comments/{comment_id}"),
                method="PATCH",
                token=github_token(),
                data={"body": body},
            )
            if status != 200:
                raise RuntimeError(
                    f"GitHub PR comment update failed: HTTP {status}: {raw[:500]}"
                )
            return

    status, _, raw = request_json(
        github_api_url(comments_path),
        method="POST",
        token=github_token(),
        data={"body": body},
    )
    if status not in (200, 201):
        raise RuntimeError(
            f"GitHub PR comment creation failed: HTTP {status}: {raw[:500]}"
        )


def post_stage_statuses(
    args: argparse.Namespace, target: Target, result: LocalCIResult
) -> None:
    for stage_id, _, context_suffix, label in REPORTABLE_STAGES:
        stage_status = result.stage_statuses.get(stage_id, "")
        state = stage_github_state(stage_status)
        if state is None:
            continue
        description = f"{label}: {stage_status}"
        try:
            post_github_status(
                target.sha,
                state,
                f"{args.context}/{context_suffix}",
                description,
                result.target_url,
            )
        except Exception as exc:
            print(
                f"Warning: failed to publish {label} status: {exc}",
                file=sys.stderr,
            )


def codex_advisory_description(codex_ai: CodexAIResult) -> str:
    execution_status = codex_ai.execution_status.strip().lower()
    verdict = codex_ai.verdict.strip().upper()
    test_status = codex_ai.test_status.strip().lower()
    constraint_status = codex_ai.constraint_status.strip().lower()
    if execution_status == "skipped":
        return "Codex AI 未运行（非阻塞）"
    if execution_status != "pass":
        if codex_ai.failure_code:
            return f"Codex AI 未完成：{public_failure_reason(codex_ai.failure_code)}（非阻塞）"
        return "Codex AI 未完成（非阻塞）"
    if verdict == "FAIL":
        return "Codex AI 建议性结论：失败（非阻塞）"
    if test_status == "insufficient_evidence":
        return "Codex AI 测试证据不足（非阻塞）"
    if test_status == "stable_failure":
        return "Codex AI 测试存在可稳定复现的失败（非阻塞）"
    if test_status == "flaky_failure":
        return "Codex AI 测试存在非确定性失败（非阻塞）"
    if test_status == "infrastructure_failure":
        return "Codex AI 测试遇到基础设施失败（非阻塞）"
    if test_status == "test_generation_error":
        return "Codex AI 测试生成失败（非阻塞）"
    if verdict == "WARNING":
        return "Codex AI 建议性结论：警告（非阻塞）"
    if constraint_status == "warning":
        return "Codex AI 测试约束警告（非阻塞）"
    return "Codex AI 建议性结论：通过（非阻塞）"


def post_codex_advisory_status(
    args: argparse.Namespace, target: Target, result: LocalCIResult
) -> None:
    post_github_status(
        target.sha,
        "success",
        f"{args.context}/codex-ai-advisory",
        codex_advisory_description(result.codex_ai),
        result.codex_ai.report_url or result.target_url,
    )


def codex_ai_output_json(result: LocalCIResult | None) -> str:
    if result is None:
        codex_ai = CodexAIResult(
            "not_reported",
            "UNKNOWN",
            "UNKNOWN",
            "not_run",
            "not_reported",
            "未找到 Codex AI 约束校验结果。",
            "尚未收到 Codex AI CI 结果。",
            "",
            "",
        )
    else:
        codex_ai = result.codex_ai
    payload = {
        "execution_status": codex_ai.execution_status,
        "verdict": codex_ai.verdict,
        "test_status": codex_ai.test_status,
        "analysis_mode": codex_ai.analysis_mode,
        "constraint_status": codex_ai.constraint_status,
        "constraint_reason": codex_ai.constraint_reason,
        "failure_reason": codex_ai.failure_reason,
        "failure_code": codex_ai.failure_code,
        "comment_markdown": codex_ai.comment_markdown,
        "report_url": codex_ai.report_url,
        "finding_locations": [
            {
                "id": location.identifier,
                "file": location.file,
                "line": location.line,
            }
            for location in codex_ai.finding_locations
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def write_github_outputs(result: LocalCIResult | None) -> None:
    output_path = os.getenv("GITHUB_OUTPUT", "")
    if not output_path:
        return
    if result is None:
        values = {
            "result_ready": "false",
            "overall_status": "not_ready",
            "target_url": "",
            "codex_ai_result": codex_ai_output_json(None),
            "run_id": "",
            "stage_results": "{}",
        }
    else:
        overall_status = "pass" if result.exit_code == 0 else "fail"
        stage_results = {"overall": overall_status, **result.stage_statuses}
        values = {
            "result_ready": "true",
            "overall_status": overall_status,
            "target_url": result.target_url,
            "run_id": result.run_id,
            "codex_ai_result": codex_ai_output_json(result),
            "stage_results": json.dumps(stage_results, separators=(",", ":")),
        }
    with open(output_path, "a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def sync_target(args: argparse.Namespace, target: Target, set_pending: bool) -> LocalCIResult | None:
    gitee_token = os.getenv("GITEE_TOKEN", "")
    if set_pending:
        post_github_status(target.sha, "pending", args.context, "Waiting for Gitee local CI result")

    timeout = max(args.timeout_seconds, 0)
    interval = max(args.poll_interval_seconds, 1)
    deadline = time.monotonic() + timeout

    while True:
        try:
            result = read_local_ci_result(args, target, gitee_token)
        except (
            TimeoutError,
            ConnectionError,
            ConnectionResetError,
            http.client.RemoteDisconnected,
            urllib.error.URLError,
        ) as exc:
            print(
                f"Transient error while checking Gitee local CI result for {target.label}: {exc}",
                file=sys.stderr,
            )
            result = None
        if result is not None:
            post_stage_statuses(args, target, result)
            try:
                post_codex_advisory_status(args, target, result)
            except Exception as exc:
                print(
                    f"Warning: failed to publish Codex AI advisory status: {exc}",
                    file=sys.stderr,
                )
            try:
                post_codex_pr_comment(target, result)
            except Exception as exc:
                print(
                    f"Warning: failed to publish Codex AI PR comment: {exc}",
                    file=sys.stderr,
                )
            write_github_outputs(result)
            if result.exit_code == 0:
                description = "Gitee local CI passed"
                warnings = [
                    label
                    for stage_id, _, _, label in REPORTABLE_STAGES
                    if result.stage_statuses.get(stage_id) == "warning"
                ]
                if warnings:
                    description = "Gitee local CI passed with " + ", ".join(warnings) + " warning"
                if result.publish_manifest and result.publish_manifest.missing_expected_files:
                    description = "Gitee local CI passed; artifact manifest has warnings"
                post_github_status(target.sha, "success", args.context, description, result.target_url)
                print(f"Gitee local CI passed for {target.label}: {result.target_url}")
            else:
                post_github_status(
                    target.sha,
                    "failure",
                    args.context,
                    f"Gitee local CI failed: status {result.exit_code}",
                    result.target_url,
                )
                print(f"Gitee local CI failed for {target.label}: {result.target_url}")
            return result

        if timeout == 0 or time.monotonic() >= deadline:
            print(f"No available Gitee local CI result for {target.label}; leaving GitHub status pending.")
            write_github_outputs(None)
            return None

        sleep_seconds = min(interval, max(1, int(deadline - time.monotonic())))
        print(f"Waiting {sleep_seconds}s before checking Gitee local CI result again...")
        time.sleep(sleep_seconds)


def reconcile_targets(args: argparse.Namespace) -> list[Target]:
    targets: list[Target] = []
    seen: set[tuple[str, str]] = set()
    configured_branches = args.reconcile_source_branches.strip()
    source_branches = re.split(r"[\s,]+", configured_branches) if configured_branches else [args.source_branch]

    for source_branch in source_branches:
        if not source_branch:
            continue
        branch_sha = github_branch_head(source_branch)
        if branch_sha:
            target = Target(
                source_branch,
                f"ci/push/{source_branch}",
                branch_sha,
                f"branch {source_branch}",
                github_repo(),
            )
            targets.append(target)
            seen.add((target.task_ref, target.sha))
        else:
            print(f"Source branch not found on GitHub: {source_branch}")

    for target in list_open_pr_targets(args.max_prs):
        key = (target.task_ref, target.sha)
        if key not in seen:
            targets.append(target)
            seen.add(key)
    return targets


def main() -> int:
    args = parse_args()

    if args.mode == "single":
        if not args.sha:
            print("--sha is required in single mode", file=sys.stderr)
            return 2
        target = Target(
            args.source_branch,
            args.task_ref or args.source_branch,
            args.sha,
            args.source_branch,
            github_repo(),
        )
        result = sync_target(args, target, args.set_pending)
        if result is None:
            return RESULT_NOT_READY_EXIT_CODE if args.require_result else 0
        if args.exit_with_result and result.exit_code != 0:
            return RESULT_FAILED_EXIT_CODE
        return 0

    updated = 0
    for target in reconcile_targets(args):
        if sync_target(args, target, set_pending=False) is not None:
            updated += 1
    print(f"Reconciled {updated} target(s) with available Gitee local CI results.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
