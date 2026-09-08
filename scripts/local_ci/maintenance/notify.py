"""One GitHub operations Issue records health changes across watchdog runners."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
from urllib import error, request


REPOSITORY = "anteloper-c/triton-anchor"
START, END = "<!-- anchor-ci-health:start -->", "<!-- anchor-ci-health:end -->"
CONFIG_ERROR = ("请配置 LOCAL_CI_OPERATIONS_ISSUE_NUMBER 为本仓库已创建的运维 Issue 编号，"
                "并让维护者订阅该 Issue；配置完成前保持 LOCAL_CI_WATCHDOG_ENABLED=false。")
FAULTS = {
    "host_offline": "服务器心跳过期，请检查服务器、电源、网络和健康发布服务。",
    "heartbeat_invalid": "健康快照校验失败，请检查监控配置和发布服务。",
    "heartbeat_unreachable": "无法读取健康快照，请检查网络和发布通道。",
    "poller_stale": "任务调度心跳过期，请检查调度服务。",
    "poller_error": "任务调度服务报告异常，请检查服务日志。",
    "poller_failed": "任务调度服务失败，请检查服务日志。",
    "poller_publish_pending": "任务结果等待发布，请检查发布通道。",
    "task_stalled": "任务执行心跳过期，请检查构建与执行进程。",
    "task_overdue": "任务超过预期时长，请检查执行阶段。",
    "publication_pending": "任务结果等待发布，请检查发布通道。",
    "health_publication_pending": "健康快照发布失败，请检查发布通道。",
    "disk_low": "可用磁盘空间不足，请按保留策略清理任务产物。",
    "disk_unavailable": "工作磁盘不可访问，请检查存储状态。",
    "container_unavailable": "常驻测试容器未运行，请检查版本环境。",
    "container_oom": "测试容器发生内存不足，请检查内存与编译并行度。",
    "maintenance_failed": "环境维护失败，请检查维护日志和回滚状态。",
    "docker_unavailable": "无法读取测试容器状态，请检查 Docker 服务。",
    "service_unavailable": "必要服务未运行，请检查服务管理器。",
}


class NotificationError(ValueError):
    """A safe operator-facing diagnostic, never an API response or credential."""


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NotificationError("GitHub API 地址发生重定向；请检查仓库配置。")


class GitHubIssue:
    def __init__(self, config):
        number = config.get("issue_number")
        if (config.get("repository") != REPOSITORY or type(number) is not int or number <= 0):
            raise NotificationError(CONFIG_ERROR)
        self.path = f"/repos/{REPOSITORY}/issues/{number}"
        self.token = os.environ.get(config.get("token_env", "GITHUB_TOKEN"), "")
        if not self.token:
            raise NotificationError("缺少 GitHub 工作流令牌；请为 watchdog 作业授予 issues:write 权限。")

    def call(self, method, suffix="", document=None):
        payload = None if document is None else json.dumps(document, ensure_ascii=False).encode("utf-8")
        call = request.Request("https://api.github.com" + self.path + suffix, data=payload, method=method,
                               headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer " + self.token,
                                        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
        try:
            with request.build_opener(NoRedirect()).open(call, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("oversized response")
            return json.loads(raw)
        except (OSError, error.URLError, ValueError):
            raise NotificationError("无法读取或更新运维 Issue；请检查仓库、Issue 编号、网络及 issues:write 权限。") from None


def public_faults(issues):
    """Never copy heartbeat messages, service names, host paths or unknown codes."""
    rows = []
    for item in issues:
        text = FAULTS.get(item["code"], "健康检查报告异常，请维护者查看受限的运维日志。")
        match = re.fullmatch(r"(?:triton-)?(3\.(?:0|3|6))", str(item.get("profile_id", "")))
        if match:
            text = "Triton " + match[1] + "：" + text
        rows.append(text)
    return sorted(set(rows))


def status_block(body, current):
    """Replace only our block, retaining the operator's existing Issue prose."""
    if body.count(START) != body.count(END) or body.count(START) > 1:
        raise NotificationError("运维 Issue 的状态区域不完整；请维护者修复或移除该区域后重试。")
    block = START + "\n" + current + "\n" + END
    if START in body:
        begin, end = body.index(START), body.index(END)
        if end < begin:
            raise NotificationError("运维 Issue 的状态区域顺序无效；请维护者修复后重试。")
        updated = body[:begin] + block + body[end + len(END):]
    else:
        updated = body.rstrip() + "\n\n" + block
    if len(updated) > 65000:
        raise NotificationError("运维 Issue 正文过长；请维护者缩短正文后重试。")
    return updated


class Notifier:
    def __init__(self, config, api=None):
        self.api = api or GitHubIssue(config)

    def update(self, worker_id, issues, dry_run=False):
        """Post only transitions; reconcile the body after an interrupted PATCH.

        The latest authenticated Actions-bot comment is the delivery receipt.
        A lost POST response is resolved on the next run without another POST.
        The workflow must serialize this one Issue's watchdog runs.
        """
        key = hashlib.sha256(worker_id.encode()).hexdigest()[:24]
        # Identity changes count, while durations/messages cannot cause alert spam.
        stable = sorted({json.dumps([i["code"], i.get("profile_id", ""), i.get("service", ""), i.get("path", "")], sort_keys=True) for i in issues})
        signature = hashlib.sha256(json.dumps(stable).encode()).hexdigest()
        marker = f"<!-- anchor-ci-health:{key}:{signature} -->"
        rows = public_faults(issues)
        title = "Local CI 需要处理" if issues else "Local CI 已恢复"
        current = marker + "\n**" + title + "**\n\n"
        current += "\n".join("- " + row for row in rows) if issues else "此前报告的故障已恢复，当前心跳与服务检查正常。"
        if dry_run:
            return {"status": "dry_run", "body": current}
        issue = self.api.call("GET")
        if not isinstance(issue, dict) or "pull_request" in issue or issue.get("state") != "open":
            raise NotificationError("请使用本仓库保持打开的运维 Issue；不能使用 PR 或已关闭的 Issue。")
        body, count = issue.get("body") or "", issue.get("comments", 0)
        if not isinstance(body, str) or type(count) is not int or count < 0:
            raise NotificationError("运维 Issue 响应不完整；请稍后重试。")
        # Validate the managed area before any comment mutation.
        status_block(body, current)
        latest = None
        pattern = re.compile(r"^<!-- anchor-ci-health:" + key + r":([0-9a-f]{64}) -->\n")
        last_page = max(1, math.ceil(count / 100))
        for page in range(last_page, max(0, last_page - 10), -1) if count else []:
            comments = self.api.call("GET", f"/comments?per_page=100&page={page}")
            if not isinstance(comments, list):
                raise NotificationError("运维 Issue 评论响应无效；请稍后重试。")
            for comment in reversed(comments):
                author = comment.get("user", {})
                text = comment.get("body", "")
                match = pattern.match(text) if isinstance(text, str) else None
                if author.get("login") == "github-actions[bot]" and author.get("type") == "Bot" and match:
                    latest = (match[1], text)
                    break
            if latest:
                break
        if count > 1000 and latest is None:
            raise NotificationError("近期评论中找不到监控记录；请维护者使用专用运维 Issue。")
        posted = False
        if latest and latest[0] == signature:
            current = latest[1]
        elif issues or latest:
            current += "\n\n检测时间（UTC）：" + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self.api.call("POST", "/comments", {"body": current})
            posted = True
        else:
            current = marker + "\n**Local CI 正常**\n\n监控已建立，当前心跳与服务检查正常。"
        updated = status_block(body, current)
        if updated != body:
            self.api.call("PATCH", document={"body": updated})
        return {"status": "sent" if posted else "unchanged" if latest or updated == body else "healthy"}
