import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SLIM = Path(__file__).resolve().parents[1] / "profiles/slim"
spec = importlib.util.spec_from_file_location(
    "enable_flaggems", SLIM / "enable_flaggems.py"
)
enable_flaggems = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enable_flaggems)


class SlimFoundationTests(unittest.TestCase):
    def test_adds_path_without_import_or_install_when_mount_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(
                    enable_flaggems.sysconfig, "get_path", return_value=temporary
                ),
                patch(
                    "sys.argv",
                    [
                        "enable_flaggems",
                        "--source",
                        "/opt/local-ci/runtime/deps/flaggems",
                    ],
                ),
            ):
                enable_flaggems.main()
            self.assertEqual(
                "/opt/local-ci/runtime/deps/flaggems/src\n",
                (Path(temporary) / "local_ci_flaggems.pth").read_text(),
            )

    def test_rejects_paths_outside_trusted_mount_root(self):
        for source in (
            "/task/candidate",
            "/opt/local-ci/runtime/deps/..",
            "/opt/local-ci/runtime/deps/flaggems/nested",
            "/tmp/import\nmalicious",
        ):
            with (
                self.subTest(source=source),
                patch("sys.argv", ["enable_flaggems", "--source", source]),
                self.assertRaises(ValueError),
            ):
                enable_flaggems.main()
