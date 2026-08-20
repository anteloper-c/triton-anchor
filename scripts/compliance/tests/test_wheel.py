from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.compliance.wheel import inspect_wheel, validate_record

from scripts.compliance.tests.helpers import DIST_INFO, make_wheel


class WheelValidationTests(unittest.TestCase):
    def test_record_accepts_sha256_and_stronger_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for algorithm in ("sha256", "sha512"):
                with self.subTest(algorithm=algorithm):
                    wheel = make_wheel(root, filename=f"demo-{algorithm}.whl", algorithm=algorithm)
                    report = validate_record(wheel)
                    self.assertEqual("pass", report["status"])
                    self.assertGreaterEqual(report["verified_entry_count"], 3)

    def test_archive_rejects_unsafe_members_duplicates_and_symlinks(self) -> None:
        unsafe_names = ("../escape.py", "..\\escape.py", "/absolute.py", "C:/absolute.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, name in enumerate(unsafe_names):
                with self.subTest(name=name):
                    wheel = make_wheel(
                        root,
                        filename=f"demo_unsafe{index}-1.0-py3-none-any.whl",
                        members=[(name, b"unsafe")],
                    )
                    with self.assertRaises(ValueError):
                        inspect_wheel(wheel)

            duplicate = make_wheel(
                root,
                filename="demo_duplicate-1.0-py3-none-any.whl",
                members=[("demo/data.txt", b"one"), ("demo/data.txt", b"two")],
            )
            with self.assertRaises(ValueError):
                inspect_wheel(duplicate)

            symlink = make_wheel(
                root,
                filename="demo_symlink-1.0-py3-none-any.whl",
                members=[],
                symlink=("demo/link", b"../../outside"),
            )
            with self.assertRaises(ValueError):
                inspect_wheel(symlink)

    def test_record_paths_are_validated_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wheel = make_wheel(
                Path(temporary),
                record_extra_rows=[("..\\outside.py", "sha256=AAAA", "1")],
            )
            with self.assertRaises(ValueError):
                validate_record(wheel)

    def test_record_must_cover_archive_in_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = {
                "unrecorded": make_wheel(
                    root,
                    filename="unrecorded.whl",
                    extra_unrecorded=[("demo/extra.py", b"extra")],
                ),
                "missing-member": make_wheel(
                    root,
                    filename="missing-member.whl",
                    record_extra_rows=[("demo/missing.py", "sha256=AAAA", "1")],
                ),
                "unhashed-member": make_wheel(
                    root,
                    filename="unhashed-member.whl",
                    unhashed={"demo/__init__.py"},
                ),
                "bad-hash": make_wheel(
                    root,
                    filename="bad-hash.whl",
                    record_overrides={"demo/__init__.py": ("sha256=AAAA", "20")},
                ),
                "omitted-member": make_wheel(
                    root,
                    filename="omitted-member.whl",
                    omit_from_record={f"{DIST_INFO}/METADATA"},
                ),
            }
            for name, wheel in cases.items():
                with self.subTest(case=name), self.assertRaises(ValueError):
                    validate_record(wheel)

    def test_native_member_is_inventoried_without_loading_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wheel = make_wheel(
                Path(temporary),
                members=[
                    ("demo/__init__.py", b""),
                    ("demo/plugin.so", b"not-an-elf"),
                    (
                        f"{DIST_INFO}/METADATA",
                        b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
                    ),
                    (f"{DIST_INFO}/WHEEL", b"Wheel-Version: 1.0\nTag: py3-none-any\n"),
                ],
            )
            report = inspect_wheel(wheel)
            self.assertEqual(["demo/plugin.so"], [entry["path"] for entry in report["native_files"]])
            self.assertEqual(64, len(report["native_files"][0]["sha256"]))

    def test_python_imports_are_inventoried_without_importing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wheel = make_wheel(
                Path(temporary),
                members=[
                    (
                        "demo/__init__.py",
                        b"import json\nimport requests\nfrom demo import local\n",
                    ),
                    ("demo/local.py", b"from __future__ import annotations\n"),
                    (
                        f"{DIST_INFO}/METADATA",
                        b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
                    ),
                    (f"{DIST_INFO}/WHEEL", b"Wheel-Version: 1.0\nTag: py3-none-any\n"),
                ],
            )
            report = inspect_wheel(wheel)
            self.assertEqual(
                [
                    {
                        "name": "requests",
                        "paths": ["demo/__init__.py"],
                        "context": "runtime-external",
                    }
                ],
                report["python_imports"],
            )
            self.assertEqual([], report["python_import_issues"])


if __name__ == "__main__":
    unittest.main()
