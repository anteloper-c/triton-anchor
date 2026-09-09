#!/usr/bin/env python3
"""Normalize published Local CI results and worker health into a static UI feed."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local_ci'))
from control.runtime.result_paths import iter_run_files, validate_result_path

SCHEMA = 'triton-anchor-dashboard-local-ci'
RESULT_SCHEMA = 'triton-anchor-local-ci-result'
HEALTH_SCHEMA = 'triton-anchor-local-ci-worker-health'
ID = re.compile(r'[A-Za-z0-9_-]{1,160}')
SHA = re.compile(r'[0-9a-f]{40}')


def read_json(path):
    if path.is_symlink() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('unsafe or oversized JSON source')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('JSON source must be an object')
    return value


def local_path(root, relative):
    if not isinstance(relative, str):
        raise ValueError('published path must be a string')
    pure = PurePosixPath(relative)
    if pure.is_absolute() or '..' in pure.parts or '\\' in relative:
        raise ValueError('published path escapes result checkout')
    candidate = root.joinpath(*pure.parts)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('published path escapes result checkout')
    return candidate


def web_link(web_url, branch, relative, tree=False):
    parsed = urlparse(web_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        return ''
    return f"{web_url.rstrip('/')}/{'tree' if tree else 'blob'}/{quote(branch, safe='')}/{quote(relative, safe='/')}"


def normalize_result(document, relative, web_url, branch, latest):
    if document.get('schema') != RESULT_SCHEMA:
        raise ValueError('unsupported result schema')
    task_id, run_id = document.get('task_id'), document.get('run_id')
    validate_result_path(relative, document, run_id)
    if not SHA.fullmatch(document.get('tested_sha', '')):
        raise ValueError('invalid task/run/commit identity')
    if document.get('repository') != 'anteloper-c/triton-anchor':
        raise ValueError('result belongs to an unauthorized repository')
    if document.get('conclusion') not in {'success', 'failure', 'error', 'cancelled'}:
        raise ValueError('missing result conclusion')
    if any(not isinstance(document.get(k), list) for k in ('checks', 'blocking_reasons', 'evidence', 'performance')):
        raise ValueError('result arrays are missing')
    if not isinstance(document.get('ai_review'), dict) or not isinstance(document.get('policy'), dict):
        raise ValueError('result review/policy are missing')
    for check in document['checks']:
        if not isinstance(check, dict) or not isinstance(check.get('id'), str) or check.get('status') not in {'passed', 'failed', 'error', 'skipped', 'not_applicable'}:
            raise ValueError('invalid check entry')
    directory = str(PurePosixPath(relative).parent)
    value = {**document, 'is_latest': latest.get(task_id) == relative,
             'result_url': web_link(web_url, branch, relative),
             'artifacts_url': web_link(web_url, branch, directory, tree=True)}
    evidence = []
    for receipt in document['evidence']:
        if not isinstance(receipt, dict):
            raise ValueError('invalid host evidence entry')
        log = receipt.get('log_path', '')
        safe = isinstance(log, str) and not PurePosixPath(log).is_absolute() and '..' not in PurePosixPath(log).parts and '\\' not in log
        evidence.append({**receipt, 'log_url': web_link(web_url, branch, directory + '/' + log) if safe and log else ''})
    value['evidence'] = evidence
    return value


def result_scope(result):
    """Group retries that answer the same public CI question."""
    if result.get('event_kind') == 'pull_request' and result.get('pr_number'):
        return f"pr:{result.get('target_branch', '')}:{result['pr_number']}"
    task_ref = result.get('task_ref', '')
    if isinstance(task_ref, str) and task_ref.startswith('ci/full/'):
        return f"full:{result.get('target_branch', '')}"
    return f"push:{result.get('target_branch', '')}"


def sync_agent_results(results_dir, output_dir, results_web_url='', results_branch='local-ci-results', limit=100):
    root, output = Path(results_dir), Path(output_dir)
    warnings, latest, invalid = [], {}, set()
    for path in sorted((root / 'tasks').glob('*/latest.json')):
        try:
            pointer = read_json(path)
            task_id, run_id = pointer['task_id'], pointer['run_id']
            if path.parent.name != task_id or not ID.fullmatch(task_id) or not ID.fullmatch(run_id):
                raise ValueError('latest index identity mismatch')
            relative = pointer.get('result_path')
            if not isinstance(relative, str) or PurePosixPath(relative).as_posix() != relative or ':' in relative:
                raise ValueError('latest index result path is not canonical')
            parts = PurePosixPath(relative).parts
            grouped = ((len(parts) == 7 and parts[:2] == ('runs', 'pr') and re.fullmatch(r'pr-[1-9][0-9]*', parts[3]))
                       or (len(parts) == 6 and parts[:2] == ('runs', 'push')))
            legacy = len(parts) == 4 and parts[0] == 'runs'
            if not (grouped or legacy) or parts[-3:] != (task_id, run_id, 'result.json'):
                raise ValueError('latest index result path mismatch')
            result_path = local_path(root, relative)
            if result_path.is_symlink() or result_path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError('unsafe or oversized JSON source')
            expected = pointer.get('result_sha256', '')
            if not re.fullmatch(r'[0-9a-f]{64}', expected) or hashlib.sha256(result_path.read_bytes()).hexdigest() != expected:
                invalid.add(relative)
                raise ValueError('latest result digest mismatch')
            # Path shape and index hash are checked before trusting any identity
            # inside the result, then the complete identity must bind that path.
            invalid.add(relative)
            source = read_json(result_path)
            if source.get('task_id') != task_id or source.get('run_id') != run_id:
                raise ValueError('latest result task/run differs from its index')
            validate_result_path(relative, source, run_id)
            if 'manifest_path' in pointer:
                manifest_path = validate_result_path(pointer['manifest_path'], source, run_id, 'publish-manifest.json')
                if PurePosixPath(manifest_path).parent != PurePosixPath(relative).parent:
                    raise ValueError('latest manifest/result directories differ')
            invalid.discard(relative)
            latest[task_id] = relative
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append({'path': path.relative_to(root).as_posix(), 'reason': str(exc)})
    runs = []
    for path in iter_run_files(root, 'result.json'):
        relative = path.relative_to(root).as_posix()
        if relative in invalid:
            continue
        try:
            source = read_json(local_path(root, relative))
            # Old result layouts may coexist; the legacy dashboard still reads them.
            if source.get('schema') != RESULT_SCHEMA:
                continue
            runs.append(normalize_result(source, relative, results_web_url, results_branch, latest))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append({'path': relative, 'reason': str(exc)})
    runs.sort(key=lambda r: (r.get('completed_at', ''), r['run_id']), reverse=True)
    current_scopes = set()
    for run in runs:
        scope = result_scope(run)
        run['is_current'] = bool(run['is_latest'] and scope not in current_scopes)
        if run['is_current']:
            current_scopes.add(scope)
    workers = []
    for path in sorted((root / 'health').glob('*.json')):
        try:
            source = read_json(local_path(root, path.relative_to(root).as_posix()))
            if source.get('schema') != HEALTH_SCHEMA or not isinstance(source.get('worker_id'), str):
                raise ValueError('invalid worker health schema/identity')
            if path.stem != source['worker_id'] or not isinstance(source.get('issues'), list):
                raise ValueError('worker health identity/issues mismatch')
            workers.append(source)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append({'path': path.relative_to(root).as_posix(), 'reason': str(exc)})
    document = {'schema': SCHEMA, 'data_mode': 'live',
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'runs': runs[:limit], 'total_runs': len(runs), 'workers': workers, 'warnings': warnings}
    output.mkdir(parents=True, exist_ok=True)
    (output / 'local-ci.json').write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--results-web-url', default='')
    parser.add_argument('--results-branch', default='local-ci-results')
    args = parser.parse_args(argv)
    document = sync_agent_results(args.results_dir, args.output_dir, args.results_web_url, args.results_branch)
    print(json.dumps({'runs': len(document['runs']), 'workers': len(document['workers']), 'warnings': len(document['warnings'])}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
