"""Historical dashboard result layout; independent from the retired CI runtime."""
from pathlib import PurePosixPath
import re
from urllib.parse import quote


def safe_path_part(value):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', value).strip('_') or 'default'


def result_task_dir(task_ref):
    for expression, category, prefix in (
        (r'ci/full/(.+)', 'ci_full', 'ci_full_'),
        (r'ci/push/(.+)', 'ci_push', 'ci_push_'),
        (r'ci/(pr-[0-9]+/.+)', 'ci_pr', 'ci_'),
        (r'ci/(base/pr-[0-9]+/.+)', 'ci_pr', 'ci_'),
    ):
        match = re.fullmatch(expression, task_ref.strip('/'))
        if match:
            return PurePosixPath('runs', category, prefix + safe_path_part(match.group(1)))
    raise ValueError('unsupported historical task reference')


def result_run_dir(task_ref, sha, run_id, head_sha=''):
    if re.fullmatch(r'ci/pr-[0-9]+/.+', task_ref.strip('/')) and head_sha:
        sha = f'h-{head_sha[:12]}_m-{sha[:12]}'
    return result_task_dir(task_ref) / safe_path_part(sha) / safe_path_part(run_id)


def gitee_tree_url(web_url, ref, relative_path):
    return f"{web_url.rstrip('/')}/tree/{quote(ref, safe='')}/{quote(str(relative_path), safe='/')}"
