#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runner="${repo_root}/scripts/local_ci/run_codex_ai_ci.sh"
renderer="${repo_root}/scripts/local_ci/render_codex_ai_report.py"
test_root="$(mktemp -d /tmp/local-ci-codex-container-test.XXXXXX)"
trap 'rm -rf -- "${test_root}"' EXIT
export GIT_CONFIG_GLOBAL="${test_root}/gitconfig"

source_repo="${test_root}/source"
relay_repo="${test_root}/relay.git"
workspace_root="${test_root}/host-workspaces"
fake_bin="${test_root}/bin"
codex_home="${test_root}/codex-home"
fake_codex="${test_root}/codex"
task_branch="ci/push/container-test"
mkdir -p "${source_repo}" "${workspace_root}" "${fake_bin}" "${codex_home}"

printf '#!/usr/bin/env bash\necho codex-cli-test\n' > "${fake_codex}"
chmod +x "${fake_codex}"
printf 'model = "test"\n' > "${codex_home}/config.toml"
printf '{"token":"test"}\n' > "${codex_home}/auth.json"

git -C "${source_repo}" init -q
git -C "${source_repo}" config user.name local-ci-test
git -C "${source_repo}" config user.email local-ci-test@example.invalid
printf 'before\n' > "${source_repo}/payload.txt"
git -C "${source_repo}" add payload.txt
git -C "${source_repo}" commit -q -m base
base_sha="$(git -C "${source_repo}" rev-parse HEAD)"
printf 'after\n' > "${source_repo}/payload.txt"
git -C "${source_repo}" add payload.txt
git -C "${source_repo}" commit -q -m target
target_sha="$(git -C "${source_repo}" rev-parse HEAD)"
git init --bare -q "${relay_repo}"
git -C "${source_repo}" remote add gitee "${relay_repo}"
git -C "${source_repo}" push -q gitee "HEAD:refs/heads/${task_branch}"

docs_branch="ci/push/docs-test"
git -C "${source_repo}" checkout -q --detach "${base_sha}"
printf 'documentation only\n' > "${source_repo}/README.md"
git -C "${source_repo}" add README.md
git -C "${source_repo}" commit -q -m docs
docs_target_sha="$(git -C "${source_repo}" rev-parse HEAD)"
git -C "${source_repo}" push -q gitee "HEAD:refs/heads/${docs_branch}"

pr_branch="ci/pr-42/feature"
pr_base_branch="ci/base/pr-42/feature"
git -C "${source_repo}" checkout -q --detach "${base_sha}"
printf 'target branch only\n' > "${source_repo}/target-only.txt"
git -C "${source_repo}" add target-only.txt
git -C "${source_repo}" commit -q -m target-base
pr_target_base_sha="$(git -C "${source_repo}" rev-parse HEAD)"
git -C "${source_repo}" push -q gitee "HEAD:refs/heads/${pr_base_branch}"
git -C "${source_repo}" checkout -q --detach "${base_sha}"
printf 'pull request only\n' > "${source_repo}/pr-only.txt"
git -C "${source_repo}" add pr-only.txt
git -C "${source_repo}" commit -q -m pr-head
pr_head_sha="$(git -C "${source_repo}" rev-parse HEAD)"
git -C "${source_repo}" push -q gitee "HEAD:refs/heads/${pr_branch}"
git -C "${source_repo}" checkout -q --detach "${target_sha}"

repo_url="file://${relay_repo}"

cat > "${fake_bin}/docker" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

original = sys.argv[1:]
state = Path(os.environ["FAKE_DOCKER_STATE"])
root = Path(os.environ["FAKE_DOCKER_ROOT"])
source_workspace = Path(os.environ["FAKE_SOURCE_WORKSPACE"])
source_container = os.environ.get("FAKE_SOURCE_CONTAINER", "anchor-sophgo-ci")
scenario = os.environ.get("FAKE_SCENARIO", "success")
state.mkdir(parents=True, exist_ok=True)
root.mkdir(parents=True, exist_ok=True)
with (state / "docker.log").open("a", encoding="utf-8") as stream:
    stream.write(shlex.join(original) + "\n")


def mapped(path: str) -> Path:
    if path == "/workspace":
        return source_workspace
    if path.startswith("/workspace/"):
        return source_workspace / path[len("/workspace/") :]
    return root / path.lstrip("/")


def copy_into(source: str, destination: str) -> None:
    destination_path = mapped(destination)
    if source.endswith("/."):
        source_path = Path(source[:-2])
        destination_path.mkdir(parents=True, exist_ok=True)
        for child in source_path.iterdir():
            target = destination_path / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(child, target, follow_symlinks=False)
        return
    source_path = Path(source)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.is_dir():
        destination_path = destination_path / source_path.name
    if source_path.is_dir():
        shutil.copytree(source_path, destination_path, dirs_exist_ok=True, symlinks=True)
    else:
        shutil.copy2(source_path, destination_path, follow_symlinks=False)


def run_git(arguments: list[str]) -> int:
    translated = list(arguments)
    if len(translated) >= 2 and translated[0] == "-C":
        translated[1] = str(mapped(translated[1]))
    completed = subprocess.run(
        ["git", *translated],
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
        stderr=sys.stderr.buffer,
        check=False,
    )
    return completed.returncode


def write_report(mode: str, output_path: Path) -> None:
    if scenario == "format_error":
        summary = "English-only summary."
    elif mode == "analysis_only":
        summary = "确定性 Local CI 未通过，本次只分析了差异和已有日志。"
    elif scenario == "docs_only":
        summary = "本次只包含文档改动，因此没有生成或执行测试。"
    elif scenario == "zero_tests":
        summary = "本次可测试代码改动没有获得足够的生成测试证据。"
    else:
        summary = "未发现具体缺陷，生成的定向测试已经通过。"

    if mode == "analysis_only":
        execution = {
            "status": "not_run",
            "summary": "由于确定性 Local CI 未通过，本次没有生成或执行测试。",
            "generated_test_files": [],
            "commands": [],
        }
    elif scenario in {"zero_tests", "docs_only"}:
        execution = {
            "status": (
                "insufficient_evidence"
                if scenario == "zero_tests"
                else "not_run"
            ),
            "summary": (
                "可测试代码改动没有生成或执行定向测试，当前证据不足。"
                if scenario == "zero_tests"
                else "本次只包含文档改动，因此不需要生成或执行测试。"
            ),
            "generated_test_files": [],
            "commands": [],
        }
    elif scenario == "over_limit":
        checkout = mapped("/codex-workspace/checkout")
        generated_files = [
            f"generated_tests/test_generated_{index}.py"
            for index in range(1, 4)
        ]
        for relative_path in generated_files:
            generated = checkout / relative_path
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text(
                "def test_generated():\n    assert True\n",
                encoding="utf-8",
            )
        durations = [601, 400, 300, 100, 100]
        commands = [
            {
                "id": f"RUN-{index:03d}",
                "command": (
                    "python3 -m pytest "
                    f"generated_tests/test_generated_1.py -q"
                ),
                "exit_code": 0,
                "duration_seconds": duration,
                "status": "passed",
                "evidence": "定向测试命令执行通过。",
            }
            for index, duration in enumerate(durations, start=1)
        ]
        execution = {
            "status": "passed",
            "summary": "测试通过，但声明的文件数、命令数和耗时超过轻量约束。",
            "generated_test_files": generated_files,
            "commands": commands,
        }
    else:
        checkout = mapped("/codex-workspace/checkout")
        generated = checkout / "generated_tests" / "test_generated.py"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(
            "def test_generated():\n    assert True\n",
            encoding="utf-8",
        )
        execution = {
            "status": "passed",
            "summary": "生成的定向测试共执行一个用例并通过。",
            "generated_test_files": ["generated_tests/test_generated.py"],
            "commands": [
                {
                    "id": "RUN-001",
                    "command": (
                        "python3 -m pytest "
                        "generated_tests/test_generated.py"
                    ),
                    "exit_code": 0,
                    "duration_seconds": 0.2,
                    "status": "passed",
                    "evidence": "定向测试共执行一个用例并通过。",
                }
            ],
        }

    report = {
        "verdict": "PASS",
        "summary": summary,
        "findings": [],
        "suggested_tests": [],
        "residual_risks": ["本次仅覆盖了与代码差异直接相关的路径。"],
        "test_execution": execution,
        "completion_marker": "CODEX_AI_CI_COMPLETE",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False),
        encoding="utf-8",
    )


if not original:
    raise SystemExit(2)
command, *args = original

if command == "inspect":
    format_value = ""
    target = ""
    index = 0
    while index < len(args):
        if args[index] == "--format":
            format_value = args[index + 1]
            index += 2
        else:
            target = args[index]
            index += 1
    if "State.Running" in format_value:
        print("true")
    elif ".RW" in format_value:
        print("false")
    elif ".Destination" in format_value:
        print("/workspace")
    raise SystemExit(0)

if command == "commit":
    if scenario == "commit_error":
        raise SystemExit(21)
    (state / "active-image").write_text(args[-1], encoding="utf-8")
    print("sha256:fake")
    raise SystemExit(0)

if command == "run":
    name = args[args.index("--name") + 1]
    if scenario == "start_error":
        raise SystemExit(22)
    (state / "active-container").write_text(name, encoding="utf-8")
    print("fake-container-id")
    raise SystemExit(0)

if command == "cp":
    source, destination = args
    _, container_path = destination.split(":", 1)
    copy_into(source, container_path)
    raise SystemExit(0)

if command == "rm":
    (state / "active-container").unlink(missing_ok=True)
    raise SystemExit(0)

if command == "image":
    if args[:2] == ["rm", "-f"]:
        (state / "active-image").unlink(missing_ok=True)
        raise SystemExit(0)
    raise SystemExit(4)

if command != "exec":
    print(f"unsupported fake docker command: {command}", file=sys.stderr)
    raise SystemExit(5)

workdir = ""
environment: dict[str, str] = {}
index = 0
while index < len(args):
    value = args[index]
    if value == "-i":
        index += 1
    elif value in {"--user", "--workdir", "--env"}:
        option_value = args[index + 1]
        if value == "--workdir":
            workdir = option_value
        elif value == "--env":
            key, _, env_value = option_value.partition("=")
            environment[key] = env_value
        index += 2
    else:
        break
container = args[index]
command_args = args[index + 1 :]
if not container.startswith("anchor-codex-ai-"):
    raise SystemExit(6)
if not command_args:
    raise SystemExit(7)

program = command_args[0]
if program == "mkdir":
    for value in command_args[1:]:
        if value != "-p":
            mapped(value).mkdir(parents=True, exist_ok=True)
    raise SystemExit(0)
if program in {"chmod", "chown"}:
    raise SystemExit(0)
if program == "/usr/local/bin/codex":
    print("codex-cli-test")
    raise SystemExit(0)
if program == "git":
    raise SystemExit(run_git(command_args[1:]))
if program == "cat":
    sys.stdout.buffer.write(mapped(command_args[1]).read_bytes())
    raise SystemExit(0)
if program == "bash" and len(command_args) >= 2 and command_args[1] == "-c":
    checkout = mapped(command_args[4])
    list_path = mapped(command_args[5])
    archive_path = mapped(command_args[6])
    untracked = subprocess.check_output(
        ["git", "-C", str(checkout), "ls-files", "--others", "--exclude-standard", "-z"]
    )
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_bytes(untracked)
    paths = [item.decode() for item in untracked.split(b"\0") if item]
    with tarfile.open(archive_path, "w:gz") as archive:
        for relative in paths:
            archive.add(checkout / relative, arcname=relative)
    raise SystemExit(0)
if program == "bash" and len(command_args) >= 2 and command_args[1] == "-lc":
    prompt = sys.stdin.read()
    assert "${" not in prompt
    if "分支：ci/pr-42/feature" in prompt:
        assert "差异模式：merge-base" in prompt
        assert re.search(r"git diff --find-renames [0-9a-f]{40}\.\.\.[0-9a-f]{40}", prompt)
    else:
        assert "差异模式：two-point" in prompt
        assert re.search(r"git diff --find-renames [0-9a-f]{40} [0-9a-f]{40}", prompt)
    mode = environment.get("AI_ANALYSIS_MODE", "full")
    if mode == "analysis_only":
        assert "只分析模式" in prompt
    else:
        assert "自主生成有针对性的测试" in prompt
        assert "可测试代码改动应生成 1 至 3 个定向测试用例" in prompt
        assert "最多创建或修改 2 个测试文件" in prompt
        assert "最多执行 4 条测试、构建或 lint 命令" in prompt
        assert "单条命令预计不超过 600 秒" in prompt
        assert "测试命令累计预计不超过 1200 秒" in prompt
        assert "至少预留 300 秒" in prompt
        assert "失败用例最多额外复跑一次" in prompt
        assert "不要运行整个仓库的全量测试或完整重编译" in prompt
        assert "test_execution.status 必须使用 insufficient_evidence" in prompt
        expected = "false" if scenario == "docs_only" else "true"
        assert f"本次差异要求生成定向测试：{expected}" in prompt
    assert environment.get("CODEX_HOME") == "/root/.codex"
    assert environment.get("AI_SCHEMA_PATH") == "/codex-workspace/codex-ai-report.schema.json"
    if scenario == "timeout":
        time.sleep(5)
        raise SystemExit(0)
    write_report(mode, mapped(environment["AI_REPORT_PATH"]))
    print(json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}))
    print(json.dumps({"type": "turn.completed"}))
    raise SystemExit(0)

print(f"unsupported fake docker exec: {shlex.join(command_args)}", file=sys.stderr)
raise SystemExit(8)
PY
chmod +x "${fake_bin}/docker"

assert_chinese_failure_report() {
  local output_dir="$1"
  grep -Fq "# Codex AI CI 报告" "${output_dir}/codex-ai-report.md"
  grep -Fq "## 结论" "${output_dir}/codex-ai-report.md"
  grep -Fq "**失败**" "${output_dir}/codex-ai-report.md"
  python3 "${renderer}" \
    --input "${output_dir}/codex-ai-report.json" \
    --output "${output_dir}/validated-fallback.md" \
    --comment-output "${output_dir}/validated-fallback-comment.md" \
    --branch test --base-sha a --target-sha b --changed-file-count 0 \
    >/dev/null
  python3 -c 'from pathlib import Path; Path("'"${output_dir}"'/codex-ai-report.md").read_text(encoding="utf-8"); Path("'"${output_dir}"'/codex-ai-comment.md").read_text(encoding="utf-8")'
}

run_case() {
  local case_name="$1"
  local scenario="$2"
  local local_ci_status="$3"
  local timeout_seconds="$4"
  local expected_exit="$5"
  local case_target_sha="${6:-${target_sha}}"
  local case_base_sha="${7:-${base_sha}}"
  local case_branch="${8:-${task_branch}}"
  local case_base_ref="${9:-}"
  local case_root="${test_root}/${case_name}"
  local output_dir="${case_root}/output"
  local docker_root="${case_root}/container-root"
  local docker_state="${case_root}/docker-state"
  local source_workspace="${case_root}/source-workspace"
  mkdir -p "${output_dir}" "${docker_root}" "${docker_state}" \
    "${source_workspace}/local-ci-artifacts/${case_name}"
  printf 'immutable\n' > "${source_workspace}/sentinel.txt"
  printf 'artifact-immutable\n' \
    > "${source_workspace}/local-ci-artifacts/${case_name}/result.txt"
  local source_digest_before
  source_digest_before="$(
    sha256sum \
      "${source_workspace}/sentinel.txt" \
      "${source_workspace}/local-ci-artifacts/${case_name}/result.txt"
  )"
  printf 'Local CI finished. Artifacts are in /workspace/local-ci-artifacts/%s\n' \
    "${case_name}" > "${output_dir}/local-ci.log"

  set +e
  PATH="${fake_bin}:${PATH}" \
  FAKE_DOCKER_STATE="${docker_state}" \
  FAKE_DOCKER_ROOT="${docker_root}" \
  FAKE_SOURCE_WORKSPACE="${source_workspace}" \
  FAKE_SOURCE_CONTAINER="anchor-sophgo-ci" \
  FAKE_SCENARIO="${scenario}" \
  CODEX_BIN="${fake_codex}" \
  CODEX_HOME="${codex_home}" \
  LOCAL_CI_CONTAINER="anchor-sophgo-ci" \
  CODEX_AI_CI_WORKSPACE_ROOT="${workspace_root}" \
  CODEX_AI_CI_TIMEOUT_SECONDS="${timeout_seconds}" \
  CODEX_AI_CI_REASONING_EFFORT="low" \
    "${runner}" "${repo_url}" "${output_dir}" "${case_target_sha}" \
    "${case_base_sha}" "${case_base_ref}" "${case_branch}" "${local_ci_status}"
  local actual_exit=$?
  set -e

  if [[ "${expected_exit}" == "0" ]]; then
    [[ ${actual_exit} -eq 0 ]]
  else
    [[ ${actual_exit} -ne 0 ]]
  fi
  [[ "$(sha256sum \
      "${source_workspace}/sentinel.txt" \
      "${source_workspace}/local-ci-artifacts/${case_name}/result.txt"
    )" == "${source_digest_before}" ]]
  [[ ! -e "${docker_state}/active-container" ]]
  [[ ! -e "${docker_state}/active-image" ]]
  grep -Fq "commit anchor-sophgo-ci triton-anchor-codex-ai-snapshot:" \
    "${docker_state}/docker.log"
  grep -Fq "image rm -f triton-anchor-codex-ai-snapshot:" \
    "${docker_state}/docker.log"
  if grep -Eq '^exec .*anchor-sophgo-ci|^cp .*anchor-sophgo-ci:' \
    "${docker_state}/docker.log"; then
    echo "Codex runner 修改了原 Local CI 容器：${case_name}" >&2
    exit 1
  fi
  if find "${workspace_root}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "宿主机一次性 checkout 未清理：${case_name}" >&2
    exit 1
  fi
}

for prompt_template in \
  codex_ai_common.md \
  codex_ai_full.md \
  codex_ai_analysis_only.md; do
  [[ -r "${repo_root}/scripts/local_ci/prompts/${prompt_template}" ]]
done

run_case success success 0 30 0
success_output="${test_root}/success/output"
grep -Fxq "status: pass" "${success_output}/codex-ai-ci-summary.txt"
grep -Fxq "local_ci_status: 0" "${success_output}/codex-ai-ci-summary.txt"
grep -Fxq "analysis_mode: full" "${success_output}/codex-ai-ci-summary.txt"
grep -Fxq "execution_mode: ephemeral_container" "${success_output}/codex-ai-ci-summary.txt"
grep -Fxq "source_container: anchor-sophgo-ci" "${success_output}/codex-ai-ci-summary.txt"
grep -Fxq "workspace_reuse: read_only" "${success_output}/codex-ai-ci-summary.txt"
grep -Fxq "test_execution_status: passed" "${success_output}/codex-ai-ci-summary.txt"
grep -Fxq "workspace_dirty: true" "${success_output}/codex-ai-ci-summary.txt"
grep -Fq -- "--volumes-from anchor-sophgo-ci:ro" "${test_root}/success/docker-state/docker.log"
grep -Fq "generated_tests/test_generated.py" "${success_output}/codex-workspace-status.txt"
tar -tzf "${success_output}/codex-generated-files.tar.gz" | grep -Fxq "generated_tests/test_generated.py"
grep -Fq "# Codex AI CI 报告" "${success_output}/codex-ai-report.md"
grep -Fq "**通过**" "${success_output}/codex-ai-report.md"
grep -Fxq "test_generation_expected: true" "${success_output}/codex-ai-ci-summary.txt"
grep -Fxq "constraint_status: pass" "${success_output}/codex-ai-ci-summary.txt"
grep -Fxq "max_test_command_duration_seconds: 0.2" "${success_output}/codex-ai-ci-summary.txt"
grep -Fxq "total_test_command_duration_seconds: 0.2" "${success_output}/codex-ai-ci-summary.txt"
grep -Fq "## 测试执行约束" "${success_output}/codex-ai-report.md"
grep -Fq "状态：通过" "${success_output}/codex-ai-report.md"

run_case pr-merge-base success 0 30 0 \
  "${pr_head_sha}" "${pr_target_base_sha}" "${pr_branch}" "${pr_base_branch}"
pr_output="${test_root}/pr-merge-base/output"
grep -Fxq "requested_base_sha: ${pr_target_base_sha}" "${pr_output}/codex-ai-ci-summary.txt"
grep -Fxq "requested_base_ref: ${pr_base_branch}" "${pr_output}/codex-ai-ci-summary.txt"
grep -Fxq "base_sha: ${base_sha}" "${pr_output}/codex-ai-ci-summary.txt"
grep -Fxq "base_source: merge-base" "${pr_output}/codex-ai-ci-summary.txt"
grep -Fxq "diff_mode: merge-base" "${pr_output}/codex-ai-ci-summary.txt"
grep -Fxq "changed_file_count: 1" "${pr_output}/codex-ai-ci-summary.txt"
grep -Fq "目标分支提交" "${pr_output}/codex-ai-report.md"
grep -Fq "实际审查起点（merge-base）" "${pr_output}/codex-ai-report.md"
grep -Fq "${pr_target_base_sha}" "${pr_output}/codex-ai-report.md"
grep -Fq "${base_sha}" "${pr_output}/codex-ai-report.md"

missing_case_root="${test_root}/pr-missing-base"
missing_output="${missing_case_root}/output"
missing_docker_root="${missing_case_root}/container-root"
missing_docker_state="${missing_case_root}/docker-state"
missing_source_workspace="${missing_case_root}/source-workspace"
mkdir -p "${missing_output}" "${missing_docker_root}" \
  "${missing_docker_state}" "${missing_source_workspace}"
printf 'Local CI finished successfully.\n' > "${missing_output}/local-ci.log"
set +e
PATH="${fake_bin}:${PATH}" \
FAKE_DOCKER_STATE="${missing_docker_state}" \
FAKE_DOCKER_ROOT="${missing_docker_root}" \
FAKE_SOURCE_WORKSPACE="${missing_source_workspace}" \
FAKE_SOURCE_CONTAINER="anchor-sophgo-ci" \
FAKE_SCENARIO="success" \
CODEX_BIN="${fake_codex}" \
CODEX_HOME="${codex_home}" \
LOCAL_CI_CONTAINER="anchor-sophgo-ci" \
CODEX_AI_CI_WORKSPACE_ROOT="${workspace_root}" \
CODEX_AI_CI_TIMEOUT_SECONDS="30" \
  "${runner}" "${repo_url}" "${missing_output}" "${pr_head_sha}" \
  "" "" "${pr_branch}" "0"
missing_exit=$?
set -e
[[ ${missing_exit} -ne 0 ]]
grep -Fxq "status: fail" "${missing_output}/codex-ai-ci-summary.txt"
grep -Fxq "diff_mode: unresolved" "${missing_output}/codex-ai-ci-summary.txt"
grep -Fq "PR Codex 审查缺少目标分支引用" \
  "${missing_output}/codex-ai-ci-summary.txt"
assert_chinese_failure_report "${missing_output}"
if grep -Fq "commit anchor-sophgo-ci" "${missing_docker_state}/docker.log"; then
  echo "PR base 缺失时不应创建 Codex 临时镜像" >&2
  exit 1
fi
if find "${workspace_root}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo "PR base 缺失后的宿主机 checkout 未清理" >&2
  exit 1
fi

run_case over-limit over_limit 0 30 0
over_limit_output="${test_root}/over-limit/output"
grep -Fxq "status: pass" "${over_limit_output}/codex-ai-ci-summary.txt"
grep -Fxq "generated_test_file_count: 3" "${over_limit_output}/codex-ai-ci-summary.txt"
grep -Fxq "test_command_count: 5" "${over_limit_output}/codex-ai-ci-summary.txt"
grep -Fxq "max_test_command_duration_seconds: 601" "${over_limit_output}/codex-ai-ci-summary.txt"
grep -Fxq "total_test_command_duration_seconds: 1501" "${over_limit_output}/codex-ai-ci-summary.txt"
grep -Fxq "constraint_status: warning" "${over_limit_output}/codex-ai-ci-summary.txt"
grep -Fq "生成测试文件数量 3 超过限制 2" "${over_limit_output}/codex-ai-ci-summary.txt"
grep -Fq "命令数量 5 超过限制 4" "${over_limit_output}/codex-ai-comment.md"
grep -Fq "单条命令最长耗时 601 秒" "${over_limit_output}/codex-ai-report.md"
grep -Fq "测试命令累计耗时 1501 秒" "${over_limit_output}/codex-ai-report.md"
grep -Fq "### 测试执行约束警告" "${over_limit_output}/codex-ai-comment.md"

run_case zero-tests zero_tests 0 30 0
zero_output="${test_root}/zero-tests/output"
grep -Fxq "status: pass" "${zero_output}/codex-ai-ci-summary.txt"
grep -Fxq "test_execution_status: insufficient_evidence" "${zero_output}/codex-ai-ci-summary.txt"
grep -Fxq "generated_test_file_count: 0" "${zero_output}/codex-ai-ci-summary.txt"
grep -Fxq "test_command_count: 0" "${zero_output}/codex-ai-ci-summary.txt"
grep -Fxq "test_generation_expected: true" "${zero_output}/codex-ai-ci-summary.txt"
grep -Fxq "constraint_status: warning" "${zero_output}/codex-ai-ci-summary.txt"
grep -Fq "未生成测试文件，测试证据不足" "${zero_output}/codex-ai-comment.md"
grep -Fq "未记录测试、构建或 lint 命令" "${zero_output}/codex-ai-report.md"

run_case docs-only docs_only 0 30 0 "${docs_target_sha}" "${base_sha}" "${docs_branch}"
docs_output="${test_root}/docs-only/output"
grep -Fxq "status: pass" "${docs_output}/codex-ai-ci-summary.txt"
grep -Fxq "test_execution_status: not_run" "${docs_output}/codex-ai-ci-summary.txt"
grep -Fxq "generated_test_file_count: 0" "${docs_output}/codex-ai-ci-summary.txt"
grep -Fxq "test_command_count: 0" "${docs_output}/codex-ai-ci-summary.txt"
grep -Fxq "test_generation_expected: false" "${docs_output}/codex-ai-ci-summary.txt"
grep -Fxq "constraint_status: pass" "${docs_output}/codex-ai-ci-summary.txt"
grep -Fq "只包含文档改动" "${docs_output}/codex-ai-report.md"
if grep -Fq "测试执行约束警告" "${docs_output}/codex-ai-comment.md"; then
  echo "纯文档改动不应产生测试执行约束警告" >&2
  exit 1
fi

run_case analysis success 1 30 0
analysis_output="${test_root}/analysis/output"
grep -Fxq "status: pass" "${analysis_output}/codex-ai-ci-summary.txt"
grep -Fxq "local_ci_status: 1" "${analysis_output}/codex-ai-ci-summary.txt"
grep -Fxq "analysis_mode: analysis_only" "${analysis_output}/codex-ai-ci-summary.txt"
grep -Fxq "test_execution_status: not_run" "${analysis_output}/codex-ai-ci-summary.txt"
grep -Fxq "generated_test_file_count: 0" "${analysis_output}/codex-ai-ci-summary.txt"
grep -Fxq "test_command_count: 0" "${analysis_output}/codex-ai-ci-summary.txt"
grep -Fxq "constraint_status: not_applicable" "${analysis_output}/codex-ai-ci-summary.txt"
grep -Fxq "workspace_dirty: false" "${analysis_output}/codex-ai-ci-summary.txt"
grep -Fq "状态：未执行" "${analysis_output}/codex-ai-report.md"

run_case format-error format_error 0 30 1
format_output="${test_root}/format-error/output"
grep -Fxq "report_format_valid: false" "${format_output}/codex-ai-ci-summary.txt"
grep -Fq "中文内容校验" "${format_output}/codex-ai-ci-summary.txt"
assert_chinese_failure_report "${format_output}"

run_case timeout timeout 0 1 1
timeout_output="${test_root}/timeout/output"
grep -Fq "硬超时" "${timeout_output}/codex-ai-ci-summary.txt"
assert_chinese_failure_report "${timeout_output}"

run_case start-error start_error 0 30 1
start_output="${test_root}/start-error/output"
grep -Fq "无法启动本次任务的临时 Codex 容器" \
  "${start_output}/codex-ai-ci-summary.txt"
assert_chinese_failure_report "${start_output}"

echo "Codex 每任务临时容器生命周期与失败兜底：通过"