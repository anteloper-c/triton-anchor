"""Run grouping preserves identity and discovers historical evidence safely."""
import tempfile
import unittest
from pathlib import Path

from scripts.local_ci.control.runtime.result_paths import (
    branch_directory, iter_run_files, run_relative, task_run_files, validate_result_path,
)


def task(**changes):
    return {'task_id': 'task-one', 'event_kind': 'pull_request', 'target_branch': 'release/3.0',
            'pr_number': 4, 'task_ref': 'ci/pr-4/contributor/topic', **changes}


class RunPathTests(unittest.TestCase):
    def test_pr_and_push_group_by_target_not_source_branch(self):
        self.assertEqual(run_relative(task(), 'run-one'), 'runs/pr/release%2F3.0/pr-4/task-one/run-one')
        for ref in ('ci/push/release/3.0', 'ci/full/release/3.0'):
            self.assertEqual(run_relative(task(event_kind='push', pr_number=0, task_ref=ref), 'run-one'),
                             'runs/push/release%2F3.0/task-one/run-one')
        self.assertNotEqual(branch_directory('release/3.0'), branch_directory('release%2F3.0'))
        self.assertEqual(branch_directory('CON'), '%43%4F%4E')
        self.assertNotEqual(branch_directory('Main').lower(), branch_directory('main').lower())

    def test_cross_branch_pr_or_mode_paths_cannot_claim_current_task(self):
        good = run_relative(task(), 'run-one') + '/result.json'
        self.assertEqual(validate_result_path(good, task(), 'run-one'), good)
        for bad in (good.replace('pr-4', 'pr-5'), good.replace('release%2F3.0', 'main'),
                    good.replace('/pr/', '/push/'), good.replace('task-one', 'task-other'),
                    good.replace('/run-one/', '/run-other/'), good.replace('runs/', 'runs//', 1),
                    good.replace('runs/', 'runs/../', 1), good.replace('/', '\\')):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                validate_result_path(bad, task(), 'run-one')
        for changed in ({'pr_number': 5}, {'pr_number': True}, {'event_kind': 'push'},
                        {'task_ref': 'ci/pr-5/topic'}, {'task_ref': 'ci/pr-4/../secret'}):
            with self.subTest(identity=changed), self.assertRaises(ValueError):
                run_relative(task(**changed), 'run-one')
        with self.assertRaises(ValueError):
            run_relative(task(event_kind='push', pr_number=0, task_ref='ci/push/other'), 'run-one')

    def test_unsafe_branches_are_rejected_and_legacy_path_requires_exact_identity(self):
        for branch in ('', '../other', 'a/../b', '/main', 'a//b', 'a\\b', 'x\x00y',
                       '.hidden', 'branch.lock', 'refs/heads/x..y', 'x:y', 'a b'):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                run_relative(task(target_branch=branch), 'run-one')
        legacy = 'runs/task-one/run-one/result.json'
        self.assertEqual(validate_result_path(legacy, task(), 'run-one'), legacy)
        with self.assertRaises(ValueError):
            validate_result_path(legacy, task(), 'run-one', allow_legacy=False)
        with self.assertRaises(ValueError):
            validate_result_path(legacy, task(task_id='another-task'), 'run-one')

    def test_scan_finds_only_known_run_layouts_and_task_journals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ['runs/task-one/old/execution.json', run_relative(task(), 'new') + '/execution.json',
                     run_relative(task(event_kind='push', pr_number=0, task_ref='ci/full/release/3.0'), 'full') + '/execution.json']
            for name in names + ['runs/task-one/old/artifacts/nested/execution.json']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            self.assertEqual({p.relative_to(root).as_posix() for p in iter_run_files(root, 'execution.json')}, set(names))
            self.assertEqual({p.relative_to(root).as_posix() for p in task_run_files(root, task())}, set(names[:2]))
