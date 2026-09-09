"""Opt-in integration against an existing persistent Linux worker.

Run as root inside that worker, with its normal read-only control mount:
  LOCAL_CI_TASK_PYTHON_INTEGRATION=1 /usr/bin/python3 -I -S \
    /opt/anchor-ci/tests/test_task_python.py -v

Commands run as uid/gid 1000 in a new synthetic task. No container or seed is
created/replaced. Logs and artifacts remain in that task for local audit. Normal
unit-test runs explicitly skip this suite instead of reporting simulated proof.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid


WRAPPER = Path('/opt/anchor-ci/control/runtime/task_python')
SEED = Path('/opt/ci-venv')
SYSTEM_PYTHON = '/usr/bin/python3'


def readonly_control_mount():
    """Use the most specific Linux mount, including nested mount overrides."""
    candidates = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        mount = Path(fields[4].replace('\\040', ' ').replace('\\134', '\\'))
        if WRAPPER.is_relative_to(mount):
            candidates.append((len(mount.parts), fields[5].split(',')))
    return bool(candidates) and 'ro' in max(candidates, key=lambda item: item[0])[1]


@unittest.skipUnless(os.environ.get('LOCAL_CI_TASK_PYTHON_INTEGRATION') == '1',
                     'opt-in: requires an existing persistent Linux CI worker')
class TaskPythonIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != 'linux' or not hasattr(os, 'geteuid') or os.geteuid() != 0:
            raise unittest.SkipTest('requires Linux root to prepare a task and drop to uid 1000')
        if not WRAPPER.is_file() or not SEED.is_dir() or not readonly_control_mount():
            raise unittest.SkipTest('requires immutable /opt/ci-venv and read-only /opt/anchor-ci')
        cls.directory = Path('/workspace/tasks') / ('review-python-' + uuid.uuid4().hex[:12]) / 'run'
        cls.directory.mkdir(parents=True, exist_ok=False)
        cls.venv = cls.directory / 'venv'
        cls.record = []
        subprocess.run([SYSTEM_PYTHON, '-I', '-S', '-m', 'venv', '--without-pip', str(cls.venv)],
                       check=True, capture_output=True, text=True, timeout=60)
        cls.site = next((cls.venv / 'lib').glob('python*/site-packages'))
        marker = cls.directory / 'startup-executed'
        poison = f"import pathlib,os; pathlib.Path({str(marker)!r}).write_text('poison'); os._exit(0)\n"
        for name in ('sitecustomize.py', 'usercustomize.py', 'poison.pth'):
            (cls.site / name).write_text(poison)
        for name in ('pip', 'build', 'pytest', 'packaging'):
            package = cls.site / name
            package.mkdir()
            (package / '__init__.py').write_text('import os; os._exit(0)\n')
            (package / '__main__.py').write_text('import os; os._exit(0)\n')
        (cls.site / 'candidate_probe.py').write_text("VALUE='task-candidate'\n")
        cls.package = cls.directory / 'package'
        cls.package.mkdir()
        (cls.package / 'pyproject.toml').write_text(
            '[build-system]\nrequires=["setuptools","wheel"]\nbuild-backend="setuptools.build_meta"\n')
        (cls.package / 'setup.py').write_text(
            "import sys,subprocess\nfrom setuptools import setup\n"
            f"assert sys.executable == {str(WRAPPER)!r}, sys.executable\n"
            "child=subprocess.run([sys.executable,'-c','raise SystemExit(17)'])\n"
            "assert child.returncode == 17, child.returncode\n"
            "setup(name='ci-guard-probe',version='0.0.1',py_modules=['ci_guard_probe'])\n")
        (cls.package / 'ci_guard_probe.py').write_text("SENTINEL='tested-wheel'\n")
        for path in [cls.directory, *cls.directory.rglob('*')]:
            if not path.is_symlink():
                os.chown(path, 1000, 1000)
        print(f'Persistent worker task Python integration artifacts: {cls.directory}', flush=True)

    def command(self, name, argv, *, expected=0, cwd=None):
        environment = dict(os.environ, LOCAL_CI_TASK_VENV=str(self.venv))
        result = subprocess.run(argv, cwd=cwd or self.directory, env=environment,
                                user=1000, group=1000, extra_groups=[],
                                capture_output=True, text=True, timeout=120)
        self.record.append({'name': name, 'argv': [str(value) for value in argv],
                            'exit_code': result.returncode, 'expected': expected,
                            'stdout': result.stdout, 'stderr': result.stderr})
        (self.directory / 'task-python-integration.json').write_text(json.dumps(self.record, indent=2))
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_startup_poison_cannot_forge_success(self):
        marker = self.directory / 'startup-executed'
        self.command('old writable interpreter reproduces false success',
                     [self.venv / 'bin/python3', '-c', 'raise SystemExit(17)'])
        self.assertTrue(marker.is_file(), 'old interpreter must actually execute the poison')
        marker.unlink()
        self.command('immutable startup preserves failure',
                     [WRAPPER, '-I', '-c', 'raise SystemExit(17)'], expected=17)
        self.assertFalse(marker.exists(), 'wrapper executed a task startup hook')

    def test_subprocess_reenters_wrapper_and_task_prefix(self):
        code = (
            "import sys,sysconfig,subprocess,json; "
            "r=subprocess.run([sys.executable,'-I','-c','raise SystemExit(17)']); "
            "assert r.returncode == 17; "
            "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,"
            "'purelib':sysconfig.get_path('purelib'),'platlib':sysconfig.get_path('platlib')}))")
        result = self.command('child interpreter and install scheme', [WRAPPER, '-c', code])
        data = json.loads(result.stdout)
        self.assertEqual(data['executable'], str(WRAPPER))
        self.assertEqual(data['prefix'], str(self.venv))
        self.assertEqual(data['purelib'], str(self.site))
        self.assertEqual(data['platlib'], str(self.site))

    def test_tooling_uses_seed_and_business_imports_use_candidate(self):
        code = (
            "import pip,build,pytest,packaging,candidate_probe,json; "
            "assert candidate_probe.VALUE == 'task-candidate'; "
            "print(json.dumps([pip.__file__,build.__file__,pytest.__file__,packaging.__file__,candidate_probe.__file__]))")
        result = self.command('toolchain shadow poison cannot replace tools', [WRAPPER, '-c', code])
        files = json.loads(result.stdout)
        self.assertTrue(all(Path(path).is_relative_to(SEED) for path in files[:4]))
        self.assertTrue(Path(files[4]).is_relative_to(self.site))
        result = self.command('real pip survives startup poison', [WRAPPER, '-m', 'pip', '--version'])
        self.assertIn('pip ', result.stdout)
        self.assertIn(str(SEED), result.stdout)

    def test_control_plane_plan_imports_frozen_source_without_startup_hooks(self):
        sys.path.insert(0, str(WRAPPER.parents[1]))
        from control.runtime.control_plane import plan
        source = self.directory / 'control-source'
        for name in ('scripts/local_ci/tests', 'scripts/ci/tests', 'scripts/dashboard'):
            (source / name).mkdir(parents=True, exist_ok=True)
        (source / 'scripts/dashboard/probe.py').write_text("VALUE = 'frozen-source'\n")
        marker = self.directory / 'source-startup-executed'
        poison = f"import pathlib,os; pathlib.Path({str(marker)!r}).touch(); os._exit(0)\n"
        for name in ('sitecustomize.py', 'usercustomize.py', 'poison.pth', 'pytest.py'):
            (source / name).write_text(poison)
        for path, name in (('scripts/local_ci/tests/test_import.py', 'dashboard'),
                           ('scripts/ci/tests/test_ci_import.py', 'worker')):
            (source / path).write_text(
                'from pathlib import Path\nimport pytest\nfrom scripts.dashboard.probe import VALUE\n'
                f'def test_{name}():\n    assert VALUE == "frozen-source"\n'
                f'    assert Path(pytest.__file__).is_relative_to({str(SEED)!r})\n')
        for path in [source, *source.rglob('*')]:
            os.chown(path, 1000, 1000)
        command = plan({'source_host_dir': str(source), 'source_dir': str(source),
                        'target_branch': 'ci_repo', 'python_bin': str(WRAPPER)})['commands'][0]
        legacy = command['argv'][:]
        option = legacy.index('-o')
        del legacy[option:option + 2]
        failed = self.command('old isolated pytest cannot import scripts', legacy, expected=2, cwd=source)
        self.assertIn("No module named 'scripts'", failed.stdout)
        passed = self.command('planned pytest imports frozen control source', command['argv'], cwd=source)
        self.assertIn('2 passed', passed.stdout)
        self.assertFalse(marker.exists(), 'source startup hook or pytest shadow was executed')
        self.assertFalse((self.directory / 'startup-executed').exists(), 'task startup hook was executed')

    def test_wheel_build_install_and_import(self):
        wheel_dir = self.directory / 'wheels'
        self.command('wheel backend and its subprocess use wrapper',
                     [WRAPPER, '-m', 'build', '--wheel', '--no-isolation', '--outdir', wheel_dir, self.package],
                     cwd=self.package)
        wheels = list(wheel_dir.glob('ci_guard_probe-*.whl'))
        self.assertEqual(len(wheels), 1)
        self.assertGreater(wheels[0].stat().st_size, 0)
        self.command('pip installs only into task prefix',
                     [WRAPPER, '-m', 'pip', 'install', '--no-index', '--no-deps', wheels[0]])
        installed = self.site / 'ci_guard_probe.py'
        self.assertTrue(installed.is_file())
        result = self.command('installed wheel is the imported candidate',
                              [WRAPPER, '-I', '-c', "import ci_guard_probe; assert ci_guard_probe.SENTINEL == 'tested-wheel'; print(ci_guard_probe.__file__)"])
        self.assertEqual(result.stdout.strip(), str(installed))


if __name__ == '__main__':
    unittest.main()
