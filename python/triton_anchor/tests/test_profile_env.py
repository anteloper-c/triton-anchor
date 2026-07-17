from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
ENV_HEADER = ROOT / "triton" / "include" / "triton" / "Tools" / "Sys" / "GetEnv.hpp"
PASS_MANAGER_BINDINGS = (
    ROOT / "csrc" / "lib" / "ttgpu" / "ir.cc",
    ROOT / "triton" / "python" / "src" / "ir.cc",
)


def test_profile_env_is_registered_and_enables_pass_timing():
    source = ENV_HEADER.read_text(encoding="utf-8")

    assert '"TRITON_ANCHOR_PROFILE"' in source
    assert "isPassTimingEnabled" in source
    assert 'getBoolEnv("MLIR_ENABLE_TIMING")' in source
    assert 'getBoolEnv("TRITON_ANCHOR_PROFILE")' in source


@pytest.mark.parametrize("source_path", PASS_MANAGER_BINDINGS)
def test_every_pass_manager_binding_uses_profile_switch(source_path):
    source = source_path.read_text(encoding="utf-8")

    assert "isPassTimingEnabled()" in source
    assert "self.enableTiming()" in source
