#!/usr/bin/env python3
"""Agent-facing client. Only the host broker writes authoritative receipts."""
import argparse
import json
import os
from pathlib import Path
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tool', help='status, finalize, or a registered tool ID')
    parameters = parser.add_mutually_exclusive_group()
    parameters.add_argument('--parameters', default='{}', help='JSON parameters')
    parameters.add_argument('--parameters-file', type=Path, help='JSON file; avoids quoting a review in a shell command')
    args = parser.parse_args()
    if args.parameters_file:
        with args.parameters_file.open('rb') as stream:
            raw = stream.read(256001)
        if len(raw) > 256000:
            parser.error('parameters file exceeds the broker request limit')
    else:
        raw = args.parameters
    body = json.dumps({'tool': args.tool, 'parameters': json.loads(raw)}).encode()
    if len(body) > 256000:
        parser.error('parameters exceed the broker request limit')
    request = urllib.request.Request(os.environ['LOCAL_CI_BROKER_URL'], data=body,
              headers={'Authorization': 'Bearer ' + os.environ['LOCAL_CI_BROKER_TOKEN'],
                       'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=14400) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(), flush=True)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result.get('status') not in ('failed', 'error') else 1


if __name__ == '__main__':
    raise SystemExit(main())
