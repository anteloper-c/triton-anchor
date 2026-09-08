"""Combine model judgments with host receipts; never trust model pass flags."""
from pathlib import Path, PurePosixPath
from .common import digest, utcnow
from .policy import TOOLS, identity


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
    arch_status = architecture.get('status')
    arch_evidence = architecture.get('evidence', [])
    valid_arch = (arch_status in ('passed', 'failed') and bool(architecture.get('summary'))
                  and isinstance(arch_evidence, list) and bool(arch_evidence)
                  and all(isinstance(item, dict) and item.get('path') and item.get('reason') for item in arch_evidence))
    source_root = getattr(broker, 'context', {}).get('source_host_dir')
    if valid_arch and source_root:
        root = Path(source_root).resolve()
        for item in arch_evidence:
            relative = PurePosixPath(item['path'])
            candidate = root / item['path']
            if relative.is_absolute() or '..' in relative.parts or not candidate.is_file() or root not in candidate.resolve().parents:
                valid_arch = False
                break
            if 'line' in item and (not isinstance(item['line'], int) or item['line'] < 1 or
                    item['line'] > len(candidate.read_text(encoding='utf-8', errors='replace').splitlines())):
                valid_arch = False
                break
    checks['architecture_review'] = {'id': 'architecture_review', 'required': True,
       'status': arch_status if valid_arch else 'error', 'reason': architecture.get('summary', 'no valid architecture review'),
       'evidence': arch_evidence}
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
    return {'schema': 'triton-anchor-local-ci-result/v4', **identity(task), 'run_id': run_id,
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
