from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Optional

SCRIPT = Path(__file__).resolve().parents[1] / "mirror_upstream_prs.py"
SPEC = importlib.util.spec_from_file_location("mirror_upstream_prs", SCRIPT)
assert SPEC and SPEC.loader
mirror = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mirror
SPEC.loader.exec_module(mirror)
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "mirror-upstream-prs.yml"


def pr_payload(number: int = 42, base_ref: str = "main") -> dict:
    return {
        "number": number,
        "title": "Improve adapter safety",
        "body": "Handle the boundary case.",
        "state": "open",
        "html_url": f"https://github.com/RACE-org/triton-anchor/pull/{number}",
        "user": {"login": "contributor"},
        "base": {"ref": base_ref, "sha": "a" * 40},
        "head": {
            "ref": "feature",
            "sha": "b" * 40,
            "repo": {"full_name": "contributor/triton-anchor"},
        },
    }


class FakeGitHub:
    def __init__(self, existing: Optional[dict] = None) -> None:
        self.existing = existing
        self.created = 0
        self.updated = 0
        self.closed = 0
        self.deleted: list[str] = []

    def find_mirror_pr(self, upstream_number: int) -> Optional[dict]:
        return self.existing

    def create_mirror_pr(self, pr: object, live_base_sha: str) -> dict:
        self.created += 1
        return {"number": 100}

    def update_mirror_pr(
        self, mirror_number: int, pr: object, live_base_sha: str
    ) -> None:
        self.updated += 1

    def close_mirror_pr(self, payload: dict, reason: str) -> None:
        self.closed += 1

    def delete_branch(self, branch: str) -> None:
        self.deleted.append(branch)

    def open_mirror_prs(self) -> list[dict]:
        return []


class FakeGit:
    def __init__(
        self,
        mergeable: bool = True,
        changes: bool = True,
        pushed: bool = True,
        head_sha: str = "b" * 40,
    ) -> None:
        self.is_mergeable = mergeable
        self.changes = changes
        self.pushed = pushed
        self.head_sha = head_sha
        self.fetch_calls = 0
        self.push_calls = 0

    def fetch_exact(self, pr: object) -> tuple[str, str]:
        self.fetch_calls += 1
        return "c" * 40, self.head_sha

    def mergeable(self, base_sha: str, head_sha: str) -> tuple[bool, str]:
        return self.is_mergeable, "conflict in file" if not self.is_mergeable else ""

    def has_changes(self, base_sha: str, head_sha: str) -> bool:
        return self.changes

    def push_refs(self, refs: object) -> bool:
        self.push_calls += 1
        return self.pushed


class PayloadTests(unittest.TestCase):
    def test_accepts_non_main_base_branch(self) -> None:
        pr = mirror.UpstreamPullRequest.from_payload(pr_payload(base_ref="triton_v3.6"))
        self.assertEqual(pr.base_ref, "triton_v3.6")
        self.assertEqual(pr.head_sha, "b" * 40)

    def test_rejects_invalid_head_sha(self) -> None:
        payload = pr_payload()
        payload["head"]["sha"] = "not-a-sha"
        with self.assertRaisesRegex(mirror.MirrorError, "head SHA"):
            mirror.UpstreamPullRequest.from_payload(payload)

    def test_branch_names_are_deterministic(self) -> None:
        self.assertEqual(
            mirror.mirror_branch_names(42),
            ("review/race-pr-42/base", "review/race-pr-42/head"),
        )

    def test_body_contains_stable_marker_and_exact_identity(self) -> None:
        pr = mirror.UpstreamPullRequest.from_payload(pr_payload())
        body = mirror.mirror_body(pr, "c" * 40)
        self.assertIn("upstream-pr-mirror:RACE-org/triton-anchor#42", body)
        self.assertIn("`cccccccccccccccccccccccccccccccccccccccc`", body)
        self.assertIn("`bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`", body)
        self.assertEqual(mirror.mirrored_upstream_number({"body": body}), 42)

    def test_allowed_base_refs_default_to_active_review_branches(self) -> None:
        self.assertEqual(
            mirror.parse_allowed_base_refs(""),
            ("main", "triton_v3.0", "anchorbase_dev"),
        )

    def test_allowed_base_refs_accept_all_marker(self) -> None:
        self.assertIsNone(mirror.parse_allowed_base_refs("all"))
        self.assertIsNone(mirror.parse_allowed_base_refs("*"))

    def test_allowed_base_refs_deduplicate_comma_or_space_values(self) -> None:
        self.assertEqual(
            mirror.parse_allowed_base_refs("main triton_v3.0,main anchorbase_dev"),
            ("main", "triton_v3.0", "anchorbase_dev"),
        )

    def test_allowed_base_refs_reject_invalid_ref(self) -> None:
        with self.assertRaisesRegex(mirror.MirrorError, "invalid allowed base ref"):
            mirror.parse_allowed_base_refs("main,../bad")


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pr = mirror.UpstreamPullRequest.from_payload(pr_payload())

    def test_creates_new_mirror_after_pushing_exact_refs(self) -> None:
        github = FakeGitHub()
        git = FakeGit()
        result = mirror.MirrorService(github, git).sync_pr(self.pr)
        self.assertEqual(result, "updated")
        self.assertEqual(git.push_calls, 1)
        self.assertEqual(github.created, 1)

    def test_default_allows_triton_30_and_anchorbase_dev(self) -> None:
        for base_ref in ("triton_v3.0", "anchorbase_dev"):
            with self.subTest(base_ref=base_ref):
                github = FakeGitHub()
                git = FakeGit()
                pr = mirror.UpstreamPullRequest.from_payload(
                    pr_payload(base_ref=base_ref)
                )
                result = mirror.MirrorService(github, git).sync_pr(pr)
                self.assertEqual(result, "updated")
                self.assertEqual(git.fetch_calls, 1)
                self.assertEqual(github.created, 1)

    def test_default_skips_unsupported_base_ref_without_fetching(self) -> None:
        github = FakeGitHub()
        git = FakeGit()
        pr = mirror.UpstreamPullRequest.from_payload(
            pr_payload(base_ref="triton_v3.6")
        )
        result = mirror.MirrorService(github, git).sync_pr(pr)
        self.assertEqual(result, "skipped_base_ref")
        self.assertEqual(git.fetch_calls, 0)
        self.assertEqual(git.push_calls, 0)
        self.assertEqual(github.created, 0)

    def test_skipping_base_ref_closes_existing_open_mirror(self) -> None:
        github = FakeGitHub(existing={"number": 100, "state": "open"})
        git = FakeGit()
        pr = mirror.UpstreamPullRequest.from_payload(
            pr_payload(base_ref="triton_v3.6")
        )
        result = mirror.MirrorService(github, git).sync_pr(pr)
        self.assertEqual(result, "skipped_base_ref")
        self.assertEqual(git.fetch_calls, 0)
        self.assertEqual(github.closed, 1)

    def test_all_base_refs_allows_future_branch_profiles(self) -> None:
        github = FakeGitHub()
        git = FakeGit()
        pr = mirror.UpstreamPullRequest.from_payload(
            pr_payload(base_ref="triton_v3.6")
        )
        result = mirror.MirrorService(
            github, git, allowed_base_refs=None
        ).sync_pr(pr)
        self.assertEqual(result, "updated")
        self.assertEqual(git.fetch_calls, 1)
        self.assertEqual(github.created, 1)

    def test_unchanged_refs_do_not_trigger_a_push(self) -> None:
        github = FakeGitHub(existing={"number": 100, "state": "open"})
        git = FakeGit(pushed=False)
        result = mirror.MirrorService(github, git).sync_pr(self.pr)
        self.assertEqual(result, "unchanged")
        self.assertEqual(git.push_calls, 1)
        self.assertEqual(github.updated, 1)

    def test_conflict_closes_existing_mirror_without_pushing(self) -> None:
        github = FakeGitHub(existing={"number": 100, "state": "open"})
        git = FakeGit(mergeable=False)
        result = mirror.MirrorService(github, git).sync_pr(self.pr)
        self.assertEqual(result, "conflict")
        self.assertEqual(git.push_calls, 0)
        self.assertEqual(github.closed, 1)

    def test_no_diff_closes_existing_mirror(self) -> None:
        github = FakeGitHub(existing={"number": 100, "state": "open"})
        git = FakeGit(changes=False)
        result = mirror.MirrorService(github, git).sync_pr(self.pr)
        self.assertEqual(result, "no_changes")
        self.assertEqual(git.push_calls, 0)
        self.assertEqual(github.closed, 1)

    def test_head_movement_is_rejected(self) -> None:
        github = FakeGitHub()
        git = FakeGit(head_sha="d" * 40)
        with self.assertRaisesRegex(mirror.MirrorError, "head moved"):
            mirror.MirrorService(github, git).sync_pr(self.pr)
        self.assertEqual(git.push_calls, 0)

    def test_dry_run_never_writes(self) -> None:
        github = FakeGitHub()
        git = FakeGit()
        result = mirror.MirrorService(github, git, dry_run=True).sync_pr(self.pr)
        self.assertEqual(result, "would_sync")
        self.assertEqual(git.push_calls, 0)
        self.assertEqual(github.created, 0)


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_uses_canonical_entrypoint_and_node24_actions(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("actions/checkout@v6", workflow)
        self.assertIn("actions/setup-python@v6", workflow)
        self.assertIn(
            "python scripts/local_ci/upstream_pr_mirror/mirror_upstream_prs.py",
            workflow,
        )
        self.assertNotIn("scripts/ci/mirror_upstream_prs.py", workflow)


if __name__ == "__main__":
    unittest.main()
