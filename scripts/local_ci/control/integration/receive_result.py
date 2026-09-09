#!/usr/bin/env python3
"""Receive only identity-bound results; untrusted result text grants no authority."""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from control.runtime.policy import TOOLS, minimum_checks
from control.runtime.result_paths import validate_result_path


SCHEMA = "triton-anchor-local-ci-result"
IDENTITY = (
    "task_id", "task_ref", "repository", "pr_number", "event_kind", "target_branch",
    "tested_sha", "base_sha", "head_sha", "worker_revision_sha",
)
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
SHA = re.compile(r"^[a-f0-9]{40}$")


class StaleTask(ValueError):
    """The task no longer represents the current GitHub/relay identity."""


def object_json(data: bytes) -> dict:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def validate_artifacts(index: dict, manifest: dict, result_bytes: bytes, expected: dict) -> dict:
    if index.get("task_id") != expected["task_id"] or not SAFE_ID.fullmatch(str(index.get("run_id", ""))):
        raise ValueError("Result index has a mismatched task or invalid run ID")
    run_id = index["run_id"]
    result_path = validate_result_path(index.get("result_path"), expected, run_id)
    manifest_path = validate_result_path(index.get("manifest_path"), expected, run_id, "publish-manifest.json")
    if PurePosixPath(result_path).parent != PurePosixPath(manifest_path).parent:
        raise ValueError("Result and manifest must belong to the same immutable run directory")
    digest = hashlib.sha256(result_bytes).hexdigest()
    if index.get("result_sha256") != digest:
        raise ValueError("Result digest does not match the published index")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("Publish manifest has no file ledger")
    entries = [item for item in files if isinstance(item, dict) and item.get("path") in {result_path, "result.json"}]
    if len(entries) != 1 or entries[0].get("sha256") != digest or entries[0].get("size") != len(result_bytes):
        raise ValueError("Result is missing or mismatched in the publish manifest")
    result = object_json(result_bytes)
    if result.get("task_id") != expected["task_id"] or result.get("run_id") != run_id:
        raise ValueError("Result identity differs from the published task/run index")
    return result


def validate_result(result: dict, metadata: dict, expected: dict) -> str:
    if result.get("schema") != SCHEMA:
        raise ValueError("Legacy or unknown result schema cannot satisfy the new CI gate")
    for field in IDENTITY:
        if (field not in expected or metadata.get(field) != expected[field] or result.get(field) != expected[field]
                or type(metadata.get(field)) is not type(expected[field]) or type(result.get(field)) is not type(expected[field])):
            raise StaleTask(f"Task/result identity mismatch: {field}")
    conclusion = result.get("conclusion")
    if conclusion not in {"success", "failure", "error", "cancelled"}:
        raise ValueError("Unknown result conclusion")
    checks = result.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("Result has no checks")
    ids = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or check["id"] in ids:
            raise ValueError("Invalid or duplicate check ID")
        ids.add(check["id"])
        if not isinstance(check.get("required"), bool) or check.get("status") not in {"passed", "failed", "error", "not_applicable", "skipped"}:
            raise ValueError("Invalid check status or required flag")
        if conclusion == "success" and (check["status"] in {"failed", "error"} or (check["required"] and check["status"] != "passed")):
            raise ValueError("Successful result contains failed or unfinished required checks")
        if check["status"] in {"skipped", "not_applicable"} and not check.get("reason"):
            raise ValueError("Unexecuted checks require an explicit reason")
    if not isinstance(result.get("blocking_reasons"), list):
        raise ValueError("Result has no blocking-reason list")
    if conclusion == "success" and result["blocking_reasons"]:
        raise ValueError("Successful result still contains blockers")
    if not isinstance(result.get("ai_review"), dict) or not isinstance(result.get("evidence"), list):
        raise ValueError("Result lacks AI review or host evidence")
    if conclusion == "success":
        control = result.get("control_identity", {})
        if (result.get("validation_scope") != "production" or not isinstance(control, dict)
                or control.get("verified") is not True
                or control.get("mode") not in {"git", "manifest"}
                or control.get("actual_sha") != expected["worker_revision_sha"]
                or not isinstance(control.get("files"), dict) or not control["files"]
                or any(not isinstance(name, str) or not isinstance(value, str)
                       or not re.fullmatch(r"[a-f0-9]{64}", value)
                       for name, value in control["files"].items())):
            raise ValueError("Successful result lacks verified production control identity")
        packed = json.dumps(control["files"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        if control.get("tree_sha256") != hashlib.sha256(packed).hexdigest():
            raise ValueError("Control file ledger does not match its tree digest")
        container = control.get("container", {})
        container_files = {path[len('scripts/local_ci/'):]: value for path, value in control['files'].items()
                           if path.startswith('scripts/local_ci/')}
        packed_container = json.dumps(container_files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if (not isinstance(container, dict) or container.get('verified') is not True
                or container.get('mount_read_only') is not True or not container_files
                or not re.fullmatch(r'[a-f0-9]{64}', str(container.get('container_id', '')))
                or container.get('tree_sha256') != hashlib.sha256(packed_container).hexdigest()):
            raise ValueError('Successful result lacks matching read-only container control evidence')
        if result.get("source_unchanged") is not True:
            raise ValueError("Successful result does not attest unchanged tested source")
        review = result["ai_review"]
        architecture = review.get("architecture", {})
        if not isinstance(review.get("summary"), str) or not review["summary"].strip():
            raise ValueError("Successful result has no AI review summary")
        if (not isinstance(architecture, dict) or architecture.get("status") != "passed" or
                not str(architecture.get("summary", "")).strip() or
                not isinstance(architecture.get("evidence"), list) or not architecture["evidence"] or
                any(not isinstance(item, dict) or not item.get("path") or not item.get("reason") for item in architecture["evidence"])):
            raise ValueError("Successful result has no evidenced architecture contract review")
        if not re.fullmatch(r"3\.\d+", str(metadata.get("triton_version", ""))):
            raise ValueError("Task has no frozen Triton version for minimum coverage")
        paths = metadata.get("changed_paths")
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError("Task has no trusted changed file list")
        policy = minimum_checks(paths, {"triton_version": metadata["triton_version"]}, metadata.get("flaggems_mode") == "full")
        by_id = {check["id"]: check for check in checks}
        if expected['event_kind'] == 'pull_request':
            information = review.get('pr_information', {})
            if (by_id.get('pr_information', {}).get('status') != 'passed'
                    or not isinstance(information, dict) or information.get('status') != 'passed'
                    or not isinstance(information.get('summary'), str) or not information['summary'].strip()):
                raise ValueError('Successful PR result lacks its mandatory PR information review')
        if any(tool not in by_id for tool in (*TOOLS, "architecture_review")):
            raise ValueError("Result omits checks from the complete tool inventory")
        receipts = {r.get("id"): r for r in result["evidence"] if isinstance(r, dict)}
        if len(receipts) != len(result["evidence"]):
            raise ValueError("Malformed or duplicate host command receipts")
        for name in policy["required"]:
            check = by_id.get(name, {})
            if check.get("status") != "passed":
                raise ValueError(f"Minimum coverage is missing or unfinished: {name}")
            if name == "architecture_review":
                continue
            references = check.get("evidence")
            if not isinstance(references, list) or not references:
                raise ValueError(f"Required tool has no host execution evidence: {name}")
            for reference in references:
                receipt = receipts.get(reference, {}) if isinstance(reference, str) else {}
                if (receipt.get("tool") != name or type(receipt.get("returncode")) is not int
                        or receipt["returncode"] != 0 or receipt.get("termination")):
                    raise ValueError(f"Required tool references missing or failed execution: {name}")
    if expected["event_kind"] == "pull_request":
        preflight = metadata.get("preflight", {})
        if any(preflight.get(name) != "success" for name in ("pr_information", "basic", "api", "security")):
            raise ValueError("PR was not admitted by every required preflight check")
        approval = metadata.get("approval", {})
        external = metadata.get("head_repo") != expected["repository"]
        if approval.get("required") is not external:
            raise ValueError("Approval requirement does not match the PR source")
        if external:
            if approval.get("status") != "approved" or any(approval.get(k) != expected[k] for k in ("head_sha", "base_sha", "tested_sha", "worker_revision_sha")):
                raise ValueError("External contribution has no exact-revision approval")
    return {"success": "success", "failure": "failure", "error": "error", "cancelled": "error"}[conclusion]


class API:
    def __init__(self, base: str, token: str = "", *, gitee: bool = False):
        self.base = base.rstrip("/")
        self.token = token
        self.gitee = gitee

    def call(self, method: str, path: str, body: dict | None = None):
        headers = {"Accept": "application/json", "User-Agent": "triton-anchor-local-ci"}
        if self.token:
            if self.gitee:
                # Gitee v5 documents access_token as a request parameter. Never log request URLs.
                path += ("&" if "?" in path else "?") + urllib.parse.urlencode({"access_token": self.token})
            else:
                headers["Authorization"] = "Bearer " + self.token
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + "/" + path.lstrip("/"), data=payload, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def contents(self, owner: str, repo: str, path: str, ref: str) -> bytes:
        route = f"repos/{owner}/{repo}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(ref, safe='')}"
        data = self.call("GET", route)
        if self.gitee and data == []:
            # Gitee returns HTTP 200 with [] for a file that is not published yet.
            # Normalize only that response into the existing bounded polling path.
            raise urllib.error.HTTPError(self.base + "/" + route, 404, "Relay file is not published", None, None)
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise ValueError("Relay contents API did not return a base64 file")
        return base64.b64decode(data["content"], validate=False)


def validate_current(api: API, expected: dict):
    prefix = f"repos/{expected['repository']}"
    if expected["event_kind"] == "pull_request":
        pull = api.call("GET", f"{prefix}/pulls/{expected['pr_number']}")
        if (pull.get("state") != "open" or pull.get("draft") or
                pull.get("head", {}).get("sha") != expected["head_sha"] or
                pull.get("base", {}).get("sha") != expected["base_sha"] or
                pull.get("base", {}).get("ref") != expected["target_branch"]):
            raise StaleTask("PR is closed, draft, retargeted or has a newer head/base")
        merge = api.call("GET", f"{prefix}/git/ref/pull/{expected['pr_number']}/merge")
        if merge.get("object", {}).get("sha") != expected["tested_sha"]:
            raise StaleTask("PR merge result is no longer current")
        commit = api.call("GET", f"{prefix}/git/commits/{expected['tested_sha']}")
        if [parent.get("sha") for parent in commit.get("parents", [])] != [expected["base_sha"], expected["head_sha"]]:
            raise StaleTask("Merge parents no longer match the task")
    else:
        branch = api.call("GET", f"{prefix}/branches/{urllib.parse.quote(expected['target_branch'], safe='')}")
        if branch.get("commit", {}).get("sha") != expected["tested_sha"]:
            raise StaleTask("Branch task was replaced by a newer commit")


def current_changed_paths(api: API, expected: dict) -> list[str]:
    """GitHub's current PR file list prevents a result from shrinking its own scope."""
    if expected["event_kind"] != "pull_request":
        return []
    paths = set()
    for page in range(1, 31):
        files = api.call("GET", f"repos/{expected['repository']}/pulls/{expected['pr_number']}/files?per_page=100&page={page}")
        if not isinstance(files, list):
            raise ValueError("GitHub did not return PR file metadata")
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                raise ValueError("Invalid GitHub PR file metadata")
            paths.add(item["filename"])
            if item.get("previous_filename"):
                paths.add(item["previous_filename"])
        if len(files) < 100:
            return sorted(paths)
    # The REST API caps large changes at 3000 files. Unknown coverage selects every applicable tool.
    return []


def current_triton_version(api: API, expected: dict) -> str:
    owner, repo = expected["repository"].split("/", 1)
    declaration = api.contents(owner, repo, "triton/python/triton/__init__.py", expected["tested_sha"]).decode("utf-8")
    match = re.search(r"(?m)^__version__\s*=\s*['\"](3\.\d+)\.", declaration)
    if not match:
        raise ValueError("Cannot identify the exact tested Triton version")
    return match[1]


def output(name: str, value):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if not isinstance(value, str) else value
    if "\n" in text or "\r" in text:
        raise ValueError("Multiline workflow output is not allowed")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"{name}={text}\n")


def summary_context(expected: dict, requested: str = "") -> str:
    """Keep branch results off the required PR context, including on the same SHA."""
    task_ref = expected["task_ref"]
    if expected["event_kind"] == "pull_request":
        prefix = f"ci/pr-{expected['pr_number']}/"
        if not expected["pr_number"] or not task_ref.startswith(prefix) or task_ref == prefix:
            raise ValueError("PR task reference does not match its identity")
        context = "local-ci/summary"
    elif expected["event_kind"] == "push" and not expected["pr_number"]:
        match = re.fullmatch(r"ci/(push|full)/.+", task_ref)
        if not match:
            raise ValueError("Branch task requires a push or full task reference")
        context = f"local-ci/summary/{match[1]}"
    else:
        raise ValueError("Unsupported task event or inconsistent PR identity")
    if requested and requested != context:
        raise ValueError("Status context does not match the task event")
    return context


CHECK_NAMES = {
    'pr_information': 'PR 说明与改动一致性', 'architecture_review': '架构与接口约束审查',
    'environment': '构建环境与依赖检查', 'frontend_build': '前端构建',
    'frontend_install': '前端安装与导入验证', 'frontend_tests': '前端测试',
    'frontend_smoke': '前端基本功能验证', 'backend_build': '后端构建',
    'backend_install': '后端安装与发现验证', 'backend_tests': '后端测试',
    'backend_smoke': '后端即时编译验证', 'flaggems': 'FlagGems 算子测试',
    'compile_time': '编译耗时测量', 'pass_profile': '编译阶段耗时分析',
    'ir_serialization': '中间表示序列化验证', 'control_plane': 'CI 流程回归检查',
    'custom_test': '针对性复现测试',
}


def comment_escape(text: str) -> str:
    text = html.escape(text, quote=False).replace('@', '@\u200b')
    return re.sub(r'([\\`*_\[\]])', r'\\\1', text)


def comment_text(value) -> str:
    """Keep legacy review prose readable; detailed diagnostics stay in the report."""
    text = str(value or '')
    if re.search(r'Codex.*(?:capacity|overload|rate.?limit)', text, re.I):
        return '审查服务暂时繁忙，重试后仍未完成；需要重新运行。'
    if re.search(r'Codex.*(?:timeout|budget exhausted)', text, re.I):
        return '审查达到运行时间上限，尚未完成；需要补齐验证。'
    if (text.startswith(('worker preparation', 'task environment copy is incomplete',
                         'worker cleanup failed; lease retained')) or
            re.search(r"^Command .*'cp'.*timed out after ", text)):
        return 'CI 运行环境准备未完成，需要维护者处理后重新执行；详细原因见完整报告。'
    replacements = {
        'context.changed_paths': '变更文件清单', 'broker policy': '检查范围',
        'broker status': '检查进度', 'context': '变更信息', '冻结 diff': '改动差异',
        'frontend/backend build': '前后端构建', 'control-plane': 'CI 流程',
        'broker': '检查执行服务', 'task_python': '隔离 Python',
        'smoke': '基本功能验证',
        'docs-only': '纯文档', 'docs_only=true': '仅修改文档',
        'trusted receipts': '执行记录', 'receipts': '执行记录',
        'required check was not completed': '必需检查尚未完成',
        'missing required check': '必需检查尚未执行，合入前需要补齐验证',
        'missing valid host command receipts': '缺少可核对的执行记录',
        'tested tracked source changed during execution': '验证期间源码发生变化，需要重新验证',
        'Codex did not submit a complete review': '审查未完成，需要重新运行',
        'no valid architecture review': '架构审查尚未完成',
        'PR intent and attributes were not reviewed': 'PR 说明与改动尚未完成核对',
        'host-verified artifact changed after tool completion': '检查产物在完成后发生变化，需要重新验证',
        'not selected for this change': '本次未执行',
        'profile has no backend/operator/performance capability': '当前环境缺少所需测试能力',
    }
    # Old reports sometimes append an exhaustive list of checks not selected.
    text = re.sub(r'(?:根据[^。\n]*策略)?未选择[^。\n]*(?:。|$)', '', text)
    text = re.sub(r'\b(?:pr_information/)?basic/api/security\b', '基础检查、接口兼容性与安全检查', text)
    for term, replacement in sorted({**replacements, **CHECK_NAMES}.items(), key=lambda pair: -len(pair[0])):
        text = text.replace(term, replacement)
    for term, replacement in {'passed': '通过', 'success': '通过', 'failed': '未通过',
                              'failure': '未通过', 'error': '未完成', 'cancelled': '已中断',
                              'skipped': '未执行', 'not_applicable': '不适用'}.items():
        text = re.sub(r'\b' + term + r'\b', replacement, text)
    text = re.sub(r'\b(?:task-[\w-]+|command-\d+|[0-9a-f]{40,64})\b', '（详见报告）', text)
    text = ' '.join(text.split())[:1600]
    text = re.sub(r'(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])', '', text)
    # Model prose must not create hidden HTML, mentions, headings or arbitrary links.
    return comment_escape(text)


def comment_evidence(items, result, *, explain=True) -> str:
    links = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        path = str(item.get('path', ''))
        relative = PurePosixPath(path)
        if not path or relative.is_absolute() or '..' in relative.parts or '\\' in path:
            continue
        label = comment_escape(path)
        line = item.get('line')
        suffix = f'#L{line}' if type(line) is int and line > 0 else ''
        if suffix:
            label += f':{line}'
        url = (f"https://github.com/anteloper-c/triton-anchor/blob/{result['tested_sha']}/"
               + urllib.parse.quote(path, safe='/') + suffix)
        detail = comment_text(item.get('reason', '')).rstrip('。；') if explain else ''
        links.append(f'[{label}]({url})' + (f'：{detail}' if detail else ''))
        if len(links) == 2:
            break
    return '；'.join(links)


def render_comment(result: dict, target_url: str, run_url: str = '') -> str:
    """Contributor-facing feedback; the full machine report remains unchanged."""
    review = result.get('ai_review', {})
    verdict = {'success': '本次要求的检查已通过。', 'failure': '发现合入阻塞，需要处理后重新验证。',
               'error': '验证尚未完成，暂不能确认可合入。', 'cancelled': '验证已中断，暂不能确认可合入。'}
    lines = [f"<!-- local-ci-result head={result['head_sha']} -->", '### CI 审查反馈', '',
             '**结论：' + verdict.get(result.get('conclusion'), '尚无完整结论。') + '**']
    head_sha = result.get('head_sha', '')
    tested_sha = result.get('tested_sha', '')
    if SHA.fullmatch(head_sha) and SHA.fullmatch(tested_sha):
        lines += ['', f'被测 PR 提交：`{head_sha[:12]}`；与目标分支合并后的验证提交：`{tested_sha[:12]}`。',
                  '此评论仅对应上述 PR 提交；新提交会追加评论，同一提交重试会更新本条评论。']
    summary = comment_text(review.get('summary'))
    if summary:
        lines += ['', '**变更意图与审查结论**', '', summary]
    receipts = {r.get('id'): r for r in result.get('evidence', []) if isinstance(r, dict)}
    executed, blockers, limitations = [], [], []
    rendered_blocking_reasons = set()
    for check in result.get('checks', []):
        name, status = check['id'], check['status']
        label = CHECK_NAMES.get(name, '补充检查')
        refs = check.get('evidence', [])
        ran = any(isinstance(ref, str) and receipts.get(ref, {}).get('tool') == name for ref in refs)
        judgment = review.get('architecture' if name == 'architecture_review' else 'pr_information', {})
        reviewed = (name in {'architecture_review', 'pr_information'} and
                    judgment.get('status') in {'passed', 'failed'} and bool(judgment.get('summary')))
        if (ran or reviewed) and status not in {'skipped', 'not_applicable'}:
            outcome = '通过' if status == 'passed' else '未通过' if status == 'failed' else '未完成'
            detail = comment_text(judgment['summary']) if reviewed and judgment['status'] == 'failed' else ''
            item = f'- **{label}：{outcome}。**' + (f' {detail}' if detail else '')
            if name == 'architecture_review':
                evidence = comment_evidence(judgment.get('evidence', []), result, explain=False)
                if evidence:
                    item += ' 依据：' + evidence
            executed.append(item)
        if status in {'failed', 'error'} or (check.get('required') and status != 'passed'):
            detail = comment_text(check.get('reason'))
            blockers.append(f'{label}尚未通过' + (f'：{detail}' if detail else '，合入前需要补齐验证。'))
            rendered_blocking_reasons.add(f"{name}: {check.get('reason', '')}")
    lines += ['', '**已执行的检查与审查**', '', *(executed or ['尚无可核对的检查或审查记录。'])]
    findings = []
    for finding in review.get('findings', []):
        if not isinstance(finding, dict) or not finding.get('summary'):
            continue
        detail = comment_text(finding['summary'])
        evidence = comment_evidence(finding.get('code_evidence', []), result)
        if evidence:
            detail += ' 依据：' + evidence
        if finding.get('blocking'):
            blockers.append(detail)
        else:
            findings.append(detail + ('（尚未确认由本次改动引入。）' if finding.get('caused_by_change') is not True else ''))
    if findings:
        lines += ['', '**需要关注的发现**', '', *('- ' + value for value in findings[:10])]
    for reason in result.get('blocking_reasons', []):
        if reason not in rendered_blocking_reasons:
            blockers.append(comment_text(reason))
    for item in review.get('uncompleted', []):
        value = item.get('summary') or item.get('reason') if isinstance(item, dict) else item
        if value and comment_text(value):
            limitations.append(comment_text(value))
    compiler_tools = set(TOOLS) - {'environment'}
    if not any(r.get('tool') in compiler_tools for r in receipts.values()):
        limitations.append('本次结论仅覆盖文档、代码或 CI 流程审查，不包含编译器构建与运行行为验证。')
    lines += ['', '**合入阻塞与重要限制**', '']
    if blockers:
        lines += ['- ' + value for value in dict.fromkeys(blockers) if value]
    elif result.get('conclusion') == 'success':
        lines.append('- 已完成的检查未报告合入阻塞。')
    else:
        lines.append('- 尚未取得完整验证结论，需要补齐检查后再判断。')
    lines += ['- ' + value for value in dict.fromkeys(limitations)][:10]
    links = [f'[完整执行报告]({target_url})']
    if run_url:
        links.insert(0, f'[查看 GitHub 检查记录]({run_url})')
    lines += ['', ' · '.join(links), '']
    return '\n'.join(lines)


def publish(api: API, expected: dict, result: dict, state: str, target_url: str, context: str = ""):
    context = summary_context(expected, context)
    validate_current(api, expected)
    prefix = f"repos/{expected['repository']}"
    status = {"state": state, "context": context, "description": {"success": "Required Local CI checks passed", "failure": "Local CI found blocking issues; review the report", "error": "Local CI could not complete verification", "cancelled": "Local CI verification was cancelled"}.get(result["conclusion"], "Local CI result available"), "target_url": target_url}
    api.call("POST", f"{prefix}/statuses/{expected['tested_sha']}", status)
    if expected["event_kind"] == "pull_request":
        # Recheck immediately before marking the head required check or updating its comment.
        validate_current(api, expected)
        api.call("POST", f"{prefix}/statuses/{expected['head_sha']}", {**status, "context": "local-ci/summary"})
        marker = f"<!-- local-ci-result head={expected['head_sha']} -->"
        run_id = os.environ.get('GITHUB_RUN_ID', '')
        run_url = f'https://github.com/anteloper-c/triton-anchor/actions/runs/{run_id}' if run_id.isdigit() else ''
        body = render_comment(result, target_url, run_url)
        comments = []
        for page in range(1, 21):
            batch = api.call("GET", f"{prefix}/issues/{expected['pr_number']}/comments?per_page=100&page={page}")
            comments.extend(batch)
            if len(batch) < 100:
                break
        previous = next((c for c in comments if c.get("user", {}).get("type") == "Bot" and marker in c.get("body", "")), None)
        if previous:
            api.call("PATCH", f"{prefix}/issues/comments/{previous['id']}", {"body": body})
        else:
            api.call("POST", f"{prefix}/issues/{expected['pr_number']}/comments", {"body": body})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gitee-owner", "gitee-repo", "task-id", "task-ref", "sha", "worker-revision-sha", "target-branch"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--gitee-results-branch", default="local-ci-results")
    parser.add_argument("--gitee-web-url", default="")
    parser.add_argument("--pr-number", default="")
    parser.add_argument("--expected-head-sha", default="")
    parser.add_argument("--comparison-base-sha", default="")
    parser.add_argument("--context", default="")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--poll-interval-seconds", type=int, default=30)
    parser.add_argument("--github-api", default="https://api.github.com")
    parser.add_argument("--gitee-api", default="https://gitee.com/api/v5")
    args = parser.parse_args(argv)
    if args.repository != "anteloper-c/triton-anchor":
        parser.error("This deployment is restricted to anteloper-c/triton-anchor")
    if args.gitee_owner != "heron-mc" or not re.fullmatch(r"[A-Za-z0-9_.-]+", args.gitee_repo) or not SAFE_ID.fullmatch(args.task_id):
        parser.error("Invalid task ID or unapproved relay owner")
    expected = dict(task_id=args.task_id, task_ref=args.task_ref, repository=args.repository,
                    pr_number=int(args.pr_number or 0), event_kind="pull_request" if args.pr_number else "push",
                    target_branch=args.target_branch, tested_sha=args.sha,
                    base_sha=args.comparison_base_sha or args.sha, head_sha=args.expected_head_sha or args.sha,
                    worker_revision_sha=args.worker_revision_sha)
    if not all(SHA.fullmatch(expected[k]) for k in ("tested_sha", "head_sha", "worker_revision_sha")):
        parser.error("Task identity requires full commit SHAs")
    try:
        args.context = summary_context(expected, args.context)
    except ValueError as exc:
        parser.error(str(exc))
    github = API(args.github_api, os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", ""))
    gitee = API(args.gitee_api, os.environ.get("GITEE_TOKEN", ""), gitee=True)
    metadata_ref = "ci/meta/" + args.task_ref.removeprefix("ci/")
    deadline = time.monotonic() + max(0, args.timeout_seconds)
    output("result_ready", "false")
    while True:
        try:
            validate_current(github, expected)
            metadata = object_json(gitee.contents(args.gitee_owner, args.gitee_repo, "task-metadata.json", metadata_ref))
            if any(metadata.get(key) != expected[key] or type(metadata.get(key)) is not type(expected[key]) for key in IDENTITY):
                raise StaleTask("A newer task replaced the relay metadata")
            index = object_json(gitee.contents(args.gitee_owner, args.gitee_repo, f"tasks/{args.task_id}/latest.json", args.gitee_results_branch))
            run_id = str(index.get("run_id", ""))
            if not SAFE_ID.fullmatch(run_id):
                raise ValueError("Invalid published run ID")
            if index.get("task_id") != args.task_id:
                raise ValueError("Published index belongs to a different task")
            manifest_path = validate_result_path(index.get("manifest_path"), expected, run_id, "publish-manifest.json")
            result_path = validate_result_path(index.get("result_path"), expected, run_id)
            if PurePosixPath(result_path).parent != PurePosixPath(manifest_path).parent:
                raise ValueError("Result and manifest directories differ")
            manifest = object_json(gitee.contents(args.gitee_owner, args.gitee_repo, manifest_path, args.gitee_results_branch))
            result_bytes = gitee.contents(args.gitee_owner, args.gitee_repo, result_path, args.gitee_results_branch)
            result = validate_artifacts(index, manifest, result_bytes, expected)
            # Scope is derived from GitHub, not the model-owned result or relay's file list.
            metadata["changed_paths"] = current_changed_paths(github, expected)
            if metadata.get("triton_version") != current_triton_version(github, expected):
                raise ValueError("Task profile version differs from the exact tested source")
            state = validate_result(result, metadata, expected)
            latest_metadata = object_json(gitee.contents(args.gitee_owner, args.gitee_repo, "task-metadata.json", metadata_ref))
            if any(latest_metadata.get(key) != expected[key] or type(latest_metadata.get(key)) is not type(expected[key]) for key in IDENTITY):
                raise StaleTask("Task was replaced while receiving its result")
            target = f"{args.gitee_web_url or f'https://gitee.com/{args.gitee_owner}/{args.gitee_repo}'}/blob/{urllib.parse.quote(args.gitee_results_branch, safe='')}/{urllib.parse.quote(result_path, safe='/')}"
            publish(github, expected, result, state, target, args.context)
            output("overall_status", state)
            output("target_url", target)
            output("stage_results", {c["id"]: c["status"] for c in result["checks"]})
            output("result_ready", "true")
            return 0 if state == "success" else 10
        except StaleTask as exc:
            print(f"Stale task ignored: {exc}")
            return 11
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code not in {404, 409, 429, 500, 502, 503, 504}:
                raise
            print(f"Waiting for relay or service recovery (HTTP {exc.code})")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # A transient TLS/connection timeout must use the same bounded wait
            # and existing receiver continuation, rather than reject the task.
            print(f"Waiting for relay or service recovery ({type(exc).__name__})")
        if time.monotonic() >= deadline:
            return 3
        time.sleep(min(max(1, args.poll_interval_seconds), 60, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, urllib.error.URLError) as error:
        message = str(error)
        for name in ("GITEE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
            if os.environ.get(name):
                message = message.replace(os.environ[name], "[redacted]").replace(urllib.parse.quote(os.environ[name], safe=""), "[redacted]")
        print(f"Local CI result rejected: {message}", file=sys.stderr)
        raise SystemExit(2)
