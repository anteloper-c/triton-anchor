"""Real windowless Python parent exercising the host command launcher on Windows."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows console creation is platform-specific')
class WindowlessHostExecution(unittest.TestCase):
    def test_pythonw_console_child_keeps_output_and_exit_status_without_console(self):
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        if not pythonw.is_file():
            self.skipTest('Matching pythonw.exe is not installed')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'windowless_parent.py'
            script.write_text(textwrap.dedent('''\
                import ctypes, json, sys
                from pathlib import Path
                sys.path.insert(0, sys.argv[1])
                from control.runtime.common import execute
                root = Path(sys.argv[3])
                child = "import ctypes,json; print(json.dumps({'console':ctypes.windll.kernel32.GetConsoleWindow(),'output':'captured'})); raise SystemExit(7)"
                result = execute([sys.argv[2], '-I', '-c', child], root / 'child.log', timeout=20)
                result['parent_console'] = ctypes.windll.kernel32.GetConsoleWindow()
                (root / 'result.json').write_text(json.dumps(result), encoding='utf-8')
                '''), encoding='utf-8')
            parent = subprocess.run([str(pythonw), '-I', str(script), str(Path(__file__).resolve().parents[1]),
                                     sys.executable, str(root)], capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(parent.returncode, 0, parent.stderr)
            result = json.loads((root / 'result.json').read_text(encoding='utf-8'))
            child = json.loads((root / 'child.log').read_text(encoding='utf-8'))
            self.assertEqual(result['parent_console'], 0)
            self.assertEqual(child, {'console': 0, 'output': 'captured'})
            self.assertEqual(result['returncode'], 7)
            self.assertIsNone(result['termination'])
            self.assertEqual(len(result['log_sha256']), 64)


if __name__ == '__main__':
    unittest.main()
