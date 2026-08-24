from __future__ import annotations

import hashlib
import json
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

    def test_cli_writes_core_consumable_same_build_evidence(self) -> None:
        package_versions = {
            "setuptools": "75.1.0",
            "wheel": "0.44.0",
            "build": "1.2.2",
            "pybind11": "2.13.6",
        }
        tool_versions = {"cmake": "3.30.4", "ninja": "1.12.1"}
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
            self.assertEqual("13.3.0", components["gcc-toolchain"]["version"])
            self.assertEqual(
                ["libzstd.so.1"], components["zstd"]["evidence"]["sonames"]
            )
            self.assertEqual("present", components["zstd"]["presence"])
            self.assertEqual("absent", components["uv"]["presence"])
            self.assertEqual(
                ["embedded"], components["ttgpu-variant-sources"]["usages"]
            )
            normalized = normalize_build_evidence(report, wheel_hash)
            self.assertEqual("success", normalized["status"], normalized["issues"])
            self.assertEqual("same-build", normalized["evidence_binding"])

            payload["native"]["unmapped_sonames"] = ["libcrypto.so.3"]
            normalized = normalize_build_evidence(report, wheel_hash)
            self.assertEqual("failed", normalized["status"])
            self.assertIn("libcrypto.so.3", normalized["issues"][0])

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
