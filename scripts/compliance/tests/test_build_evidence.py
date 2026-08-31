from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts.compliance import build_evidence
from scripts.compliance.discovery import normalize_build_evidence


class BuildEvidenceTests(unittest.TestCase):
    @staticmethod
    def make_wheel(directory: Path, native: bytes = b"ELF") -> Path:
        wheel = directory / "triton_anchor-0.2.0-cp312-cp312-linux_x86_64.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(build_evidence.NATIVE_WHEEL_MEMBER, native)
        return wheel

    @staticmethod
    def make_source_root(directory: Path) -> Path:
        root = directory / "source"
        (root / "triton" / "third_party" / "f2reduce").mkdir(parents=True)
        (root / "triton" / "cmake").mkdir(parents=True)
        (root / "triton" / "TRITON_VERSION").write_text(
            "# Commit: " + "7" * 40 + "\n", encoding="utf-8"
        )
        (root / "triton" / "cmake" / "llvm-hash.txt").write_text(
            "8" * 40 + "\n", encoding="utf-8"
        )
        (root / "triton" / "third_party" / "f2reduce" / "VERSION").write_text(
            "9" * 40 + ".\n", encoding="utf-8"
        )
        return root

    @staticmethod
    def python_build_report(package_versions: dict[str, str]) -> dict[str, object]:
        requirements = {
            "build": ["packaging>=19.1", "pyproject_hooks"],
            "packaging": [],
            "pybind11": [],
            "pyproject_hooks": [],
            "setuptools": [],
            "wheel": [],
        }
        roots = {"build", "pybind11", "setuptools", "wheel"}
        return {
            "version": "1",
            "pip_version": "25.2",
            "environment": {"python_full_version": platform.python_version()},
            "install": [
                {
                    "requested": name in roots,
                    "metadata": {
                        "name": name,
                        "version": package_versions[name],
                        "requires_dist": requirements[name],
                    },
                }
                for name in requirements
            ],
        }

    def test_cli_writes_core_consumable_same_build_evidence(self) -> None:
        package_versions = {
            "setuptools": "75.1.0",
            "wheel": "0.44.0",
            "build": "1.2.2",
            "pybind11": "2.13.6",
            "packaging": "24.1",
            "pyproject_hooks": "1.2.0",
        }
        tool_versions = {"cmake": "3.30.4", "ninja": "1.12.1"}
        ubuntu_queries = {
            "cmake": "3.30.4-1ubuntu1",
            "g++-13": "13.3.0-6ubuntu2~24.04",
            "libzstd1": "1.5.5+dfsg2-2build1.1",
            "ninja-build": "1.12.1-1",
        }
        sonames = [
            "libLLVM.so.19.1",
            "libz.so.1",
            "libzstd.so.1",
            "libc.so.6",
            "libstdc++.so.6",
        ]

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            wheel = self.make_wheel(directory)
            source_root = self.make_source_root(directory)
            output = directory / "build-evidence.json"
            python_build_report = directory / "python-build-environment.json"
            python_build_report.write_text(
                json.dumps(self.python_build_report(package_versions)),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    build_evidence,
                    "_inspect_native_dependencies",
                    return_value=sonames,
                ),
                mock.patch.object(
                    build_evidence,
                    "_distribution_version",
                    side_effect=lambda name: package_versions.get(name),
                ),
                mock.patch.object(
                    build_evidence,
                    "_command_version",
                    side_effect=lambda command: tool_versions.get(
                        Path(command[0]).name
                    ),
                ),
                mock.patch.object(
                    build_evidence, "_llvm_version", return_value="19.1.7"
                ),
                mock.patch.object(
                    build_evidence,
                    "_configured_cxx_compiler",
                    return_value=build_evidence._component(
                        "gcc-toolchain",
                        "13.3.0",
                        ["build-only"],
                        {
                            "source": "configured-cxx-compiler",
                            "path": "/usr/bin/g++",
                        },
                    ),
                ),
                mock.patch.object(
                    build_evidence,
                    "_ubuntu_package_query",
                    side_effect=lambda package: (
                        {
                            "name": package,
                            "version": ubuntu_queries[package],
                            "ecosystem": "Ubuntu:24.04:LTS",
                        },
                        {
                            "source": "dpkg-query",
                            "source_package": package,
                            "source_version": ubuntu_queries[package],
                            "binary_package": package,
                            "binary_version": ubuntu_queries[package],
                            "architecture": "amd64",
                        },
                    ),
                ),
                mock.patch.dict("os.environ", {"TTGPU": "1"}),
            ):
                result = build_evidence.main(
                    [
                        "--wheel",
                        str(wheel),
                        "--source-root",
                        str(source_root),
                        "--evidence-binding",
                        "same-build",
                        "--cxx-compiler",
                        "/usr/bin/g++",
                        "--package-tool",
                        "pypa-build",
                        "--python-build-report",
                        str(python_build_report),
                        "--ubuntu-package",
                        "zstd=libzstd1",
                        "--ubuntu-package",
                        "gcc-toolchain=g++-13",
                        "--ubuntu-package",
                        "cmake=cmake",
                        "--ubuntu-package",
                        "ninja=ninja-build",
                        "--cpython-source-commit",
                        "a" * 40,
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, result)
            report = json.loads(output.read_text(encoding="utf-8"))
            payload = report["compliance_build"]
            wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
            self.assertEqual("same-build", payload["evidence_binding"])
            self.assertEqual(wheel_hash, payload["artifact_sha256"])
            self.assertEqual(sonames, payload["native"]["dt_needed"])
            components = {item["id"]: item for item in payload["components"]}
            self.assertEqual("8" * 40, components["llvm-project"]["version"])
            self.assertEqual(
                "19.1.7", components["llvm-project"]["evidence"]["tool_version"]
            )
            self.assertEqual("7" * 40, components["triton"]["version"])
            self.assertEqual("9" * 40, components["f2reduce"]["version"])
            self.assertEqual(
                "13.3.0-6ubuntu2~24.04",
                components["gcc-toolchain"]["version"],
            )
            self.assertEqual(
                "13.3.0",
                components["gcc-toolchain"]["evidence"]["tool_version"],
            )
            self.assertEqual(
                {
                    "name": "github.com/python/cpython",
                    "commit": "a" * 40,
                },
                components["cpython"]["osv_query"],
            )
            self.assertEqual(
                "Ubuntu:24.04:LTS",
                components["cmake"]["osv_query"]["ecosystem"],
            )
            self.assertEqual(
                ["libzstd.so.1"], components["zstd"]["evidence"]["sonames"]
            )
            self.assertEqual(
                "1.5.5+dfsg2-2build1.1", components["zstd"]["version"]
            )
            self.assertEqual("present", components["zstd"]["presence"])
            self.assertEqual("absent", components["uv"]["presence"])
            self.assertEqual(
                ["packaging", "pyproject-hooks"],
                components["pypa-build"]["depends_on"],
            )
            self.assertEqual("24.1", components["packaging"]["version"])
            self.assertFalse(components["packaging"]["evidence"]["requested"])
            self.assertEqual(
                ["embedded"], components["ttgpu-variant-sources"]["usages"]
            )
            normalized = normalize_build_evidence(report, wheel_hash)
            self.assertEqual("success", normalized["status"], normalized["issues"])
            self.assertEqual("same-build", normalized["evidence_binding"])
            normalized_by_id = {
                item["component_id"]: item for item in normalized["items"]
            }
            self.assertEqual(
                ["packaging", "pyproject-hooks"],
                normalized_by_id["pypa-build"]["depends_on"],
            )

            payload["native"]["unmapped_sonames"] = ["libcrypto.so.3"]
            normalized = normalize_build_evidence(report, wheel_hash)
            self.assertEqual("failed", normalized["status"])
            self.assertIn("libcrypto.so.3", normalized["issues"][0])

    def test_python_build_report_rejects_an_unrelated_requested_root(self) -> None:
        versions = {
            "setuptools": "75.1.0",
            "wheel": "0.44.0",
            "build": "1.2.2",
            "pybind11": "2.13.6",
            "packaging": "24.1",
            "pyproject_hooks": "1.2.0",
            "ambient": "9.9.9",
        }
        report = self.python_build_report(versions)
        report["install"].append(
            {
                "requested": True,
                "metadata": {
                    "name": "ambient",
                    "version": "9.9.9",
                    "requires_dist": [],
                },
            }
        )
        with (
            mock.patch.object(
                build_evidence,
                "_distribution_version",
                side_effect=lambda name: versions.get(name),
            ),
            self.assertRaisesRegex(
                build_evidence.BuildEvidenceError, "requested roots"
            ),
        ):
            build_evidence._python_build_components(report)

    def test_ubuntu_package_query_uses_source_package_identity(self) -> None:
        completed = subprocess.CompletedProcess(
            ["dpkg-query"],
            0,
            stdout=(
                "gcc-13\t13.3.0-6ubuntu2~24.04\tg++-13\t"
                "13.3.0-6ubuntu2~24.04\tamd64"
            ),
            stderr="",
        )
        with (
            mock.patch.object(
                build_evidence.shutil, "which", return_value="/usr/bin/dpkg-query"
            ),
            mock.patch.object(
                build_evidence.subprocess, "run", return_value=completed
            ) as invoked,
            mock.patch.object(
                build_evidence,
                "_ubuntu_ecosystem",
                return_value="Ubuntu:24.04:LTS",
            ),
        ):
            query, evidence = build_evidence._ubuntu_package_query("g++-13")

        self.assertEqual(
            {
                "name": "gcc-13",
                "version": "13.3.0-6ubuntu2~24.04",
                "ecosystem": "Ubuntu:24.04:LTS",
            },
            query,
        )
        self.assertEqual("g++-13", evidence["binary_package"])
        self.assertIn("${source:Version}", invoked.call_args.args[0][2])

    def test_native_inspection_reads_only_libtriton(self) -> None:
        native = b"only libtriton reaches readelf"
        output = """
0x0000000000000001 (NEEDED) Shared library: [libz.so.1]
0x0000000000000001 (NEEDED) Shared library: [libc.so.6]
0x0000000000000001 (NEEDED) Shared library: [libz.so.1]
"""

        def fake_readelf(
            command: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            self.assertEqual(["readelf", "-d"], command[:2])
            extracted = Path(command[2])
            self.assertEqual(native, extracted.read_bytes())
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        with tempfile.TemporaryDirectory() as temporary:
            wheel = self.make_wheel(Path(temporary), native)
            with (
                mock.patch.object(build_evidence.shutil, "which", return_value="readelf"),
                mock.patch.object(
                    build_evidence.subprocess, "run", side_effect=fake_readelf
                ),
            ):
                needed = build_evidence._inspect_native_dependencies(wheel)

        self.assertEqual(["libc.so.6", "libz.so.1"], needed)


if __name__ == "__main__":
    unittest.main()
