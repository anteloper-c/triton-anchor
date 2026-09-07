import importlib.util
import os
import sys
from pathlib import Path


LOCAL_CI_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = LOCAL_CI_ROOT / "shared/task_tmp.py"
SPEC = importlib.util.spec_from_file_location("local_ci_task_tmp", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
TASK_TMP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TASK_TMP
SPEC.loader.exec_module(TASK_TMP)


TARGET_SHA = "a" * 40
TASK_NAME = "triton-anchor-local-ci-task.aaaaaaaaaaaa.Ab12Cd"


def make_task_root(parent: Path) -> Path:
    task_root = parent / TASK_NAME
    task_root.mkdir()
    TASK_TMP.prepare_task_root(task_root, TARGET_SHA, parent=parent)
    return task_root


def test_owned_dump_stage_cleanup_preserves_task_and_shared_caches(tmp_path: Path):
    task_root = make_task_root(tmp_path)
    stage = task_root / "dump/backend-smoke"
    stage.mkdir()
    (stage / "module.ttir").write_text("ttir", encoding="utf-8")
    (stage / "kernel.so").write_bytes(b"binary")

    shared_cache = tmp_path / "root/.triton/cache"
    uv_cache = tmp_path / "root/.cache/uv"
    for cache in (shared_cache, uv_cache):
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "keep").write_text("keep", encoding="utf-8")

    result = TASK_TMP.cleanup_owned_path(
        task_root,
        TARGET_SHA,
        "dump/backend-smoke",
        parent=tmp_path,
    )

    assert result["removed_files"] == 2
    assert not stage.exists()
    assert task_root.is_dir()
    assert (shared_cache / "keep").read_text(encoding="utf-8") == "keep"
    assert (uv_cache / "keep").read_text(encoding="utf-8") == "keep"


def test_task_cleanup_removes_only_the_marked_task_root(tmp_path: Path):
    task_root = make_task_root(tmp_path)
    (task_root / "tmp/1234-0").mkdir()
    (task_root / "tmp/1234-0/kernel.so").write_bytes(b"temporary")
    neighboring_task = tmp_path / "triton-anchor-local-ci-task.bbbbbbbbbbbb.Zy98Xw"
    neighboring_task.mkdir()
    (neighboring_task / "keep").write_text("keep", encoding="utf-8")

    result = TASK_TMP.cleanup_task_root(
        task_root,
        TARGET_SHA,
        parent=tmp_path,
    )

    assert result["removed_files"] >= 2
    assert not task_root.exists()
    assert (neighboring_task / "keep").read_text(encoding="utf-8") == "keep"


def test_task_cleanup_unlinks_symlink_without_following_it(tmp_path: Path):
    task_root = make_task_root(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = task_root / "tmp/outside-link"
    try:
        os.symlink(outside, link)
    except OSError:
        TASK_TMP.cleanup_task_root(task_root, TARGET_SHA, parent=tmp_path)
        return

    TASK_TMP.cleanup_task_root(task_root, TARGET_SHA, parent=tmp_path)

    assert outside.read_text(encoding="utf-8") == "keep"


def test_task_cleanup_rejects_an_ownership_marker_for_another_sha(tmp_path: Path):
    task_root = make_task_root(tmp_path)
    marker = task_root / TASK_TMP.MARKER_NAME
    marker.write_text(
        marker.read_text(encoding="utf-8").replace(TARGET_SHA, "b" * 40),
        encoding="utf-8",
    )

    try:
        TASK_TMP.cleanup_task_root(task_root, TARGET_SHA, parent=tmp_path)
    except ValueError as error:
        assert "marker SHA mismatch" in str(error)
    else:
        raise AssertionError("cleanup accepted an ownership marker for another SHA")

    marker.write_text(
        marker.read_text(encoding="utf-8").replace("b" * 40, TARGET_SHA),
        encoding="utf-8",
    )
    TASK_TMP.cleanup_task_root(task_root, TARGET_SHA, parent=tmp_path)


def test_owned_cleanup_rejects_paths_outside_one_dump_stage(tmp_path: Path):
    task_root = make_task_root(tmp_path)

    for relative in ("tmp", "dump", "../outside", "dump/a/b"):
        try:
            TASK_TMP.cleanup_owned_path(
                task_root,
                TARGET_SHA,
                relative,
                parent=tmp_path,
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe owned cleanup path was accepted: {relative}")

    TASK_TMP.cleanup_task_root(task_root, TARGET_SHA, parent=tmp_path)


# v3 snapshot assertion retired; v4 executor isolation is tested in agent_ci/tests.
