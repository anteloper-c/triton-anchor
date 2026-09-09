"""Opt-in native POSIX permission checks in an existing persistent Linux worker.

LOCAL_CI_LINUX_PERMISSIONS_INTEGRATION=1 /usr/bin/python3 -I -S \
  /opt/anchor-ci/tests/test_linux_permissions.py -v

Uses only a new /tmp fixture, never real tasks, containers, credentials or services.
"""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HELPER = Path(__file__).resolve().parents[1] / 'control/runtime/task_permissions.py'
spec = importlib.util.spec_from_file_location('task_permissions', HELPER)
permissions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(permissions)


@unittest.skipUnless(os.environ.get('LOCAL_CI_LINUX_PERMISSIONS_INTEGRATION') == '1',
                     'opt-in: requires root in an existing persistent Linux worker')
class LinuxTaskPermissions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise unittest.SkipTest('requires native Linux root and real UID switching')
        cls.temp = tempfile.TemporaryDirectory(prefix='anchor-ci-posix-', dir='/tmp')
        cls.root = Path(cls.temp.name)
        cls.root.chmod(0o711)
        mounts = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields = line.split()
            mount = Path(fields[4].replace('\\040', ' '))
            if cls.root.is_relative_to(mount):
                mounts.append((len(mount.parts), fields[fields.index('-') + 1]))
        cls.filesystem = max(mounts)[1]
        if cls.filesystem not in {'overlay', 'tmpfs', 'ext4', 'xfs', 'btrfs'}:
            cls.temp.cleanup()
            raise unittest.SkipTest('requires native POSIX storage, not a desktop bind mount')
        print('Native Linux permission fixture: ' + str(cls.root) + ' (' + cls.filesystem + ')', flush=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.case = self.root / self._testMethodName
        self.case.mkdir(mode=0o711)
        self.workspace = self.case / 'workspace'
        self.task = self.workspace / 'tasks/task-one/run-one'
        previous = os.umask(0o077)
        try:
            self.task.mkdir(parents=True)
            for name in ('source', 'agent', 'artifacts/custom'):
                (self.task / name).mkdir(parents=True)
            (self.task / 'source/candidate.py').write_text('VALUE = 42\n')
            self.secret = self.case / 'private/auth.json'
            self.secret.parent.mkdir()
            self.secret.write_text('synthetic-auth-boundary')
        finally:
            os.umask(previous)

    def as_uid(self, uid, code):
        result = subprocess.run(['/usr/bin/python3', '-I', '-S', '-c', code,
                                 str(self.workspace), str(self.task), str(self.secret)],
                                user=uid, group=1000, extra_groups=[], umask=0o077,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def prepare_leaf(self):
        # These represent the Engine's existing leaf isolation, independent of
        # the new shared-ancestor helper exercised directly above them.
        for path in [self.task, *self.task.rglob('*')]:
            os.chown(path, 1000, 1000)
        os.chown(self.task, 0, 0)
        self.task.chmod(0o755)
        for path in (self.task / 'source', self.task / 'source/candidate.py'):
            path.chmod(0o755 if path.is_dir() else 0o644)
        (self.task / 'artifacts').chmod(0o2750)
        (self.task / 'artifacts/custom').chmod(0o2770)
        os.chown(self.task / 'agent', 1001, 1000)
        (self.task / 'agent').chmod(0o700)

    def test_umask_0077_requires_ancestor_traversal_then_both_uids_work(self):
        self.prepare_leaf()
        denied = self.as_uid(1000, "import os,sys,json; print(json.dumps({'reachable':os.access(sys.argv[2],os.X_OK)}))")
        self.assertFalse(denied['reachable'])
        permissions.prepare_ancestors(self.workspace, self.task)
        for path in (self.workspace, self.workspace / 'tasks', self.task.parent):
            self.assertEqual((path.stat().st_uid, path.stat().st_gid, stat.S_IMODE(path.stat().st_mode)), (0, 0, 0o711))
        # Trusted host root can still write into the private agent directory.
        previous = os.umask(0o077)
        try:
            permissions.write_agent_document(self.task / 'agent/context.json', {'task_id': 'task-one'})
        finally:
            os.umask(previous)
        self.assertEqual((self.task / 'agent/context.json').stat().st_uid, 0)
        self.assertEqual(stat.S_IMODE((self.task / 'agent/context.json').stat().st_mode), 0o644)
        common = """
import json,os,sys
from pathlib import Path
workspace,task,secret=map(Path,sys.argv[1:])
def denied(action):
 try: action()
 except PermissionError: return True
 return False
assert denied(lambda:list(workspace.iterdir()))
assert denied(lambda:(workspace/'replace-task').mkdir())
assert denied(lambda:secret.read_text())
assert (task/'source/candidate.py').read_text()=='VALUE = 42\\n'
"""
        build = self.as_uid(1000, common + """
assert denied(lambda:(task/'agent/context.json').read_text())
(task/'source/build-marker').write_text('built by UID 1000')
(task/'artifacts/private-command.log').write_text('private command evidence')
print(json.dumps({'uid':os.geteuid(),'built':True,'agent_private':True,'host_credentials_private':True}))
""")
        agent = self.as_uid(1001, common + """
assert json.loads((task/'agent/context.json').read_text())['task_id']=='task-one'
assert denied(lambda:(task/'source/candidate.py').write_text('forbidden'))
assert denied(lambda:(task/'artifacts/private-command.log').read_text())
(task/'agent/completion.json').write_text('{"observed":true}')
(task/'artifacts/custom/targeted.json').write_text('{"custom":true}')
print(json.dumps({'uid':os.geteuid(),'context_read':True,'source_write_denied':True,'custom_artifact_written':True}))
""")
        self.assertEqual((build['uid'], agent['uid']), (1000, 1001))
        self.assertEqual((self.task / 'artifacts/private-command.log').read_text(), 'private command evidence')
        self.assertEqual(json.loads((self.task / 'agent/completion.json').read_text()), {'observed': True})
        self.assertEqual(stat.S_IMODE((self.task / 'agent').stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)
        print(json.dumps({'build': build, 'agent': agent, 'host_root_collected_private_evidence': True}), flush=True)

    def test_symlink_parent_is_rejected_without_changing_target(self):
        alternate = self.case / 'alternate'
        alternate.mkdir(mode=0o700)
        link = self.case / 'linked-workspace'
        link.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(ValueError):
            permissions.prepare_ancestors(link, link / 'tasks/task-one/run-one')
        self.assertEqual(stat.S_IMODE(self.workspace.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(alternate.stat().st_mode), 0o700)

    def test_wrong_task_structure_is_rejected_before_permissions_change(self):
        with self.assertRaises(ValueError):
            permissions.prepare_ancestors(self.workspace, self.task / 'source')
        self.assertEqual(stat.S_IMODE(self.workspace.stat().st_mode), 0o700)

    def test_host_context_write_does_not_follow_agent_controlled_symlinks(self):
        self.prepare_leaf()
        permissions.prepare_ancestors(self.workspace, self.task)
        destination = self.task / 'agent/publication-diagnostic.json'
        destination.symlink_to(self.secret)
        (self.task / 'agent/publication-diagnostic.json.tmp').symlink_to(self.secret)
        permissions.write_agent_document(destination, {'phase': 'publish_pending'})
        self.assertEqual(self.secret.read_text(), 'synthetic-auth-boundary')
        self.assertFalse(destination.is_symlink())
        self.assertEqual(json.loads(destination.read_text()), {'phase': 'publish_pending'})

    def test_chmod_uses_open_descriptor_after_agent_replaces_temporary_path(self):
        self.prepare_leaf()
        permissions.prepare_ancestors(self.workspace, self.task)
        actual_fchmod = os.fchmod

        def replace_then_chmod(descriptor, mode):
            # UID 1001 owns the parent and can replace the pathname even though
            # the open temporary file itself is owned by root. Exercise that
            # actual POSIX race before the chmod operation.
            self.as_uid(1001, """
import json,sys
from pathlib import Path
task,secret=Path(sys.argv[2]),Path(sys.argv[3])
temporary=next((task/'agent').glob('.host-*'))
temporary.unlink()
temporary.symlink_to(secret)
print(json.dumps({'temporary_replaced':True}))
""")
            actual_fchmod(descriptor, mode)

        with mock.patch.object(permissions.os, 'fchmod', side_effect=replace_then_chmod):
            permissions.write_agent_document(self.task / 'agent/context.json', {'task_id': 'task-one'})
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)
        self.assertEqual(self.secret.read_text(), 'synthetic-auth-boundary')


if __name__ == '__main__':
    unittest.main()
