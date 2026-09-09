#!/usr/bin/env python3
"""Plan/apply the dedicated GitHub ruleset and external-fork approval environment.

API contracts: https://docs.github.com/en/rest/repos/rules
https://docs.github.com/en/rest/deployments/environments
No existing ruleset is edited unless it has our exact managed name and repository.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse

from receive_result import API


MANAGED_NAME = "Local CI mandatory checks"
CONTEXTS = ("local-ci/basic", "local-ci/api", "local-ci/security", "local-ci/summary")


def ruleset_payload(branches: list[str], integration_id: int) -> dict:
    if not branches or any(not branch or branch.startswith("refs/") or ".." in branch for branch in branches):
        raise ValueError("Explicit valid target branch names are required")
    return {
        "name": MANAGED_NAME, "target": "branch", "enforcement": "active",
        "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}],
        "conditions": {"ref_name": {"include": ["refs/heads/" + branch for branch in branches], "exclude": []}},
        "rules": [{"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": True,
            "do_not_enforce_on_create": True,
            "required_status_checks": [{"context": name, "integration_id": integration_id} for name in CONTEXTS],
        }}],
    }


def managed_rulesets(api, prefix: str, repository: str, existing: list, desired: dict) -> list:
    """Identify an existing equivalent gate by structure, not protocol editions.

    Display-name changes must not create a second mandatory ruleset. Only an
    exact repository-owned gate, target set and check policy qualify for rename.
    """
    matches = []
    for item in existing:
        if item.get('source') != repository or item.get('target') != 'branch':
            continue
        if item.get('name') == MANAGED_NAME:
            matches.append(item)
            continue
        detail = api.call('GET', f"{prefix}/rulesets/{item['id']}")
        actual_refs = detail.get('conditions', {}).get('ref_name', {})
        desired_refs = desired['conditions']['ref_name']
        same_targets = (set(actual_refs.get('include', [])) == set(desired_refs['include'])
                        and actual_refs.get('exclude', []) == desired_refs['exclude'])
        actual_rules = detail.get('rules', [])
        if (same_targets and detail.get('bypass_actors', []) in ([], desired['bypass_actors']) and len(actual_rules) == 1
                and actual_rules[0].get('type') == 'required_status_checks'):
            actual = dict(actual_rules[0].get('parameters', {}))
            wanted = dict(desired['rules'][0]['parameters'])
            check_key = lambda check: (check.get('context', ''), check.get('integration_id', 0))
            actual['required_status_checks'] = sorted(actual.get('required_status_checks', []), key=check_key)
            wanted['required_status_checks'] = sorted(wanted['required_status_checks'], key=check_key)
            if actual == wanted:
                matches.append(item)
    return matches


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="anteloper-c/triton-anchor")
    parser.add_argument("--branch", action="append", required=True)
    parser.add_argument("--reviewer", action="append", required=True, help="GitHub maintainer login; at most six")
    parser.add_argument("--environment", default="local-ci-fork-approval")
    parser.add_argument("--apply", action="store_true", help="Apply the printed plan; omission performs reads only")
    args = parser.parse_args(argv)
    if args.repository != "anteloper-c/triton-anchor":
        parser.error("Only anteloper-c/triton-anchor is authorized")
    if not 1 <= len(args.reviewer) <= 6:
        parser.error("An approval environment needs one to six reviewers")
    api = API("https://api.github.com", os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""))
    prefix = "repos/" + args.repository
    reviewers = []
    for login in dict.fromkeys(args.reviewer):
        user = api.call("GET", "users/" + urllib.parse.quote(login, safe=""))
        permission = api.call("GET", f"{prefix}/collaborators/{urllib.parse.quote(login, safe='')}/permission")
        if permission.get("permission") not in {"write", "maintain", "admin"}:
            raise ValueError(f"Approval reviewer {login} is not a repository maintainer")
        reviewers.append({"type": "User", "id": user["id"]})
    application = api.call("GET", "apps/github-actions")
    rule = ruleset_payload(list(dict.fromkeys(args.branch)), application["id"])
    existing = api.call("GET", f"{prefix}/rulesets?includes_parents=false&per_page=100")
    matches = managed_rulesets(api, prefix, args.repository, existing, rule)
    if len(matches) > 1:
        raise ValueError("Duplicate managed rulesets need explicit cleanup")
    environment_path = f"{prefix}/environments/{urllib.parse.quote(args.environment, safe='')}"
    try:
        current_env = api.call("GET", environment_path)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        current_env = {}
    wait_timer = next((rule.get("wait_timer", 0) for rule in current_env.get("protection_rules", []) if rule.get("type") == "wait_timer"), 0)
    environment = {"wait_timer": wait_timer, "prevent_self_review": True, "reviewers": reviewers,
                   "deployment_branch_policy": current_env.get("deployment_branch_policy")}
    print(json.dumps({"repository": args.repository, "ruleset": rule, "environment_name": args.environment,
                      "environment": environment, "apply": args.apply}, ensure_ascii=False, indent=2))
    if args.apply:
        # Establish human approval first; enabling required checks does not grant admission.
        api.call("PUT", environment_path, environment)
        path = f"{prefix}/rulesets" + (f"/{matches[0]['id']}" if matches else "")
        changed = api.call("PUT" if matches else "POST", path, rule)
        verified = api.call("GET", f"{prefix}/rulesets/{changed['id']}")
        if verified.get("enforcement") != "active":
            raise ValueError("GitHub did not activate the required-check ruleset")
        print("Required checks and external-fork approval environment applied and read back.")


if __name__ == "__main__":
    main()
