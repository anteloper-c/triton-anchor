"""Combine model judgments with host receipts; never trust model pass flags."""
from pathlib import Path, PurePosixPath, PureWindowsPath
from .common import digest, utcnow
from .policy import TOOLS, identity


def validate_architecture_evidence(evidence, source_root):
    """Require source locations, shared by repairable finalize and final reporting."""
    field = '缺少源码位置证据：review.architecture.evidence'
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(field + ' 必须是非空对象列表，每项包含 path 和 reason；命令回执不能替代源码位置。')
    if not source_root:
        raise ValueError(field + ' 无法核对当前任务的源码目录。')
    try:
        root = Path(source_root).resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        raise ValueError(field + ' 无法核对当前任务的源码目录。') from None
    for index, item in enumerate(evidence):
        entry = f'{field}[{index}]'
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip()
                                             for key in ('path', 'reason')):
            raise ValueError(entry + ' 必须包含非空字符串 path 和 reason；command 回执 ID 不能替代源码位置。')
        relative = PurePosixPath(item['path'])
        try:
            candidate = root / item['path']
            valid_path = (not relative.is_absolute() and not PureWindowsPath(item['path']).drive
                          and '..' not in relative.parts and '\\' not in item['path']
                          and candidate.is_file() and root in candidate.resolve(strict=True).parents)
        except (OSError, ValueError, RuntimeError):
            valid_path = False
        if not valid_path:
            raise ValueError(entry + '.path 必须是当前任务源码目录中已存在文件的相对路径。')
        if 'line' in item:
            if type(item['line']) is not int or item['line'] < 1:
                raise ValueError(entry + '.line 必须是文件实际范围内的正整数。')
            try:
                lines = len(candidate.read_text(encoding='utf-8', errors='replace').splitlines())
            except (OSError, ValueError):
                raise ValueError(entry + '.path 文件不可读，无法核对 line。') from None
            if item['line'] > lines:
                raise ValueError(entry + '.line 必须是文件实际范围内的正整数。')


def build_result(task, run_id, policy, broker, *, started_at, source_unchanged,
                 agent_exitcode=0, cancelled=False, error=None):
    checks = dict(broker.checks)
    blocking = []
    review = broker.review or {}
    for name, fingerprints in getattr(broker, 'artifact_fingerprints', {}).items():
        if checks.get(name, {}).get('status') != 'passed':
            continue
        for path, expected in fingerprints.items():
            candidate = Path(path)
            if not candidate.is_file() or candidate.is_symlink() or digest(candidate) != expected:
                checks[name] = {**checks[name], 'status': 'error',
                                'reason': 'host-verified artifact changed after tool completion'}
                break
    known = {r['id']: r for r in broker.receipts}
    pr_review = review.get('pr_information', {})
    is_pr = task['event_kind'] == 'pull_request'
    pr_status = pr_review.get('status')
    if is_pr and (pr_status not in ('passed', 'failed') or not str(pr_review.get('summary', '')).strip()):
        pr_status = 'error'
    checks['pr_information'] = {'id': 'pr_information', 'required': is_pr,
        'status': pr_status if is_pr else 'not_applicable', 'evidence': [],
        'reason': pr_review.get('summary', 'PR intent and attributes were not reviewed') if is_pr else 'not a PR event'}
    architecture = review.get('architecture', {})
    if not isinstance(architecture, dict):
        architecture = {}
    arch_status = architecture.get('status')
    arch_evidence = architecture.get('evidence', [])
    source_root = getattr(broker, 'context', {}).get('source_host_dir')
    arch_error = None
    try:
        validate_architecture_evidence(arch_evidence, source_root)
    except ValueError:
        arch_error = '架构审查缺少可核对的源码位置，请提供实际文件路径及审查说明，并核对所引用的行号。'
    valid_arch = (not arch_error and arch_status in ('passed', 'failed')
                  and isinstance(architecture.get('summary'), str) and bool(architecture['summary'].strip()))
    checks['architecture_review'] = {'id': 'architecture_review', 'required': True,
       'status': arch_status if valid_arch else 'error',
       'reason': architecture['summary'] if valid_arch else arch_error or '架构审查缺少明确结论或审查说明。',
       'evidence': arch_evidence if isinstance(arch_evidence, list) else []}
    for tool in TOOLS:
        if tool not in checks:
            na = tool in policy['not_applicable']
            required = tool in policy['required']
            checks[tool] = {'id': tool, 'required': required,
                'status': 'not_applicable' if na else 'skipped', 'evidence': [],
                'reason': 'profile has no backend/operator/performance capability' if na else
                          ('required check was not completed' if required else 'not selected for this change')}
    for required in policy['required']:
        check = checks.get(required)
        if not check or check['status'] != 'passed':
            blocking.append(f'{required}: ' + (check['reason'] if check else 'missing required check'))
        elif required != 'architecture_review':
            refs = check.get('evidence', [])
            if not refs or any(ref not in known or known[ref].get('tool') != required or
                              known[ref].get('returncode') != 0 or known[ref].get('termination') for ref in refs):
                blocking.append(f'{required}: missing valid host command receipts')
    for check in checks.values():
        if check['status'] in ('failed', 'error') and check['id'] not in policy['required']:
            blocking.append(f"{check['id']}: {check['reason']}")
    for finding in review.get('findings', []):
        if finding.get('blocking'):
            refs = finding.get('reproduction_receipts', [])
            proven_failure = any(ref in known and known[ref]['returncode'] != 0 and not known[ref]['termination'] for ref in refs)
            code_evidence = bool(finding.get('code_evidence'))
            if finding.get('severity') in ('critical', 'high') and proven_failure and code_evidence and finding.get('caused_by_change') is True:
                blocking.append(finding.get('summary', 'reproduced high-risk failure'))
            else:
                finding['blocking'] = False
                finding['qualification'] = 'reported risk; deterministic failure and change attribution not established'
    if not source_unchanged:
        blocking.append('tested tracked source changed during execution')
    if agent_exitcode or not broker.review or not review.get('summary', '').strip():
        error = error or 'Codex did not submit a complete review'
    if error:
        blocking.append(error)
    conclusion = ('cancelled' if cancelled else 'error' if error or not valid_arch or pr_status == 'error' else
                  'failure' if blocking else 'success')
    return {'schema': 'triton-anchor-local-ci-result', **identity(task), 'run_id': run_id,
            'control_identity': getattr(broker, 'context', {}).get('control_identity', {'verified': False}),
            'validation_scope': getattr(broker, 'context', {}).get('validation_scope', 'production'),
            'conclusion': conclusion, 'checks': list(checks.values()),
            'blocking_reasons': blocking, 'ai_review': review, 'evidence': broker.receipts,
            'performance': getattr(broker, 'performance', []), 'policy': policy,
            'started_at': started_at, 'completed_at': utcnow(), 'source_unchanged': source_unchanged}


def markdown(result):
    lines = [f"## Local CI: {result['conclusion']}", '',
             f"Task `{result['task_id']}` · tested `{result['tested_sha']}`", '',
             '| Check | Result | Reason |', '| --- | --- | --- |']
    for check in result['checks']:
        reason = str(check['reason']).replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| {check['id']} | {check['status']} | {reason} |")
    if result['blocking_reasons']:
        lines.extend(['', '### Blocking reasons', ''])
        lines.extend('- ' + str(reason) for reason in result['blocking_reasons'])
    lines.extend(['', '### AI review', '', result['ai_review'].get('summary', 'Review unavailable'), ''])
    return '\n'.join(lines)
