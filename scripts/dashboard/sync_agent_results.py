#!/usr/bin/env python3
"""Normalize published Local CI results and worker health into a static UI feed."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote, urlparse

SCHEMA = 'triton-anchor-dashboard-local-ci'
RESULT_SCHEMA = 'triton-anchor-local-ci-result'
HEALTH_SCHEMA = 'triton-anchor-local-ci-worker-health'
ID = re.compile(r'[A-Za-z0-9_.-]{1,160}')
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
    parts = PurePosixPath(relative).parts
    if len(parts) != 4 or parts[0] != 'runs' or parts[3] != 'result.json':
        raise ValueError('unsupported result layout')
    if document.get('schema') != RESULT_SCHEMA:
        raise ValueError('unsupported result schema')
    if document.get('task_id') != parts[1] or document.get('run_id') != parts[2]:
        raise ValueError('result identity differs from publication path')
    if not ID.fullmatch(parts[1]) or not ID.fullmatch(parts[2]) or not SHA.fullmatch(document.get('tested_sha', '')):
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
    directory = '/'.join(parts[:3])
    value = {**document, 'is_latest': latest.get(parts[1]) == parts[2],
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


def sync_agent_results(results_dir, output_dir, results_web_url='', results_branch='local-ci-results', limit=100):
    root, output = Path(results_dir), Path(output_dir)
    warnings, latest, invalid = [], {}, set()
    for path in sorted((root / 'tasks').glob('*/latest.json')):
        try:
            pointer = read_json(path)
            task_id, run_id = pointer['task_id'], pointer['run_id']
            if path.parent.name != task_id or not ID.fullmatch(task_id) or not ID.fullmatch(run_id):
                raise ValueError('latest index identity mismatch')
            relative = f'runs/{task_id}/{run_id}/result.json'
            if pointer.get('result_path') != relative:
                raise ValueError('latest index result path mismatch')
            result_path = local_path(root, relative)
            expected = pointer.get('result_sha256', '')
            if not re.fullmatch(r'[0-9a-f]{64}', expected) or hashlib.sha256(result_path.read_bytes()).hexdigest() != expected:
                invalid.add(relative)
                raise ValueError('latest result digest mismatch')
            latest[task_id] = run_id
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append({'path': path.relative_to(root).as_posix(), 'reason': str(exc)})
    runs = []
    for path in sorted((root / 'runs').glob('*/*/result.json')):
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
