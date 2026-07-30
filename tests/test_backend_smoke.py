"""
triton-anchor 统一后端冒烟测试
===============================

用于三方测试中的企业后端注册及基础接口检查，验证：
  1. 企业后端 wheel 的 entry point 能被发现；
  2. Compiler 和 Driver 已注册到 Triton；
  3. Driver 能够初始化并返回 Target。

本脚本不编译或运行 kernel。只有一个后端时可直接运行：
    python3 tests/test_backend_smoke.py

安装了多个后端时，使用实际注册的 entry point 名称指定受测后端：
    python3 tests/test_backend_smoke.py --backend sophgo
"""

import argparse
import contextlib
import io
import os
import sys
import traceback
from importlib import metadata as importlib_metadata


def get_backend_entry_points():
    """返回当前 Python 环境中注册到 ``triton.backends`` 的 entry points。"""
    try:
        return list(importlib_metadata.entry_points(group="triton.backends"))
    except TypeError:
        # Python 3.9 兼容：entry_points() 尚不支持 group 参数。
        return list(importlib_metadata.entry_points().get("triton.backends", []))


def choose_entry_point(entry_points, requested_backend):
    available = sorted(entry_point.name for entry_point in entry_points)
    if not available:
        raise RuntimeError(
            "未找到任何 triton.backends entry point；请先安装企业后端 wheel"
        )

    if requested_backend:
        matches = [
            entry_point
            for entry_point in entry_points
            if entry_point.name == requested_backend
        ]
        if not matches:
            raise RuntimeError(
                f"未找到后端 {requested_backend!r}；当前已注册后端: "
                f"{', '.join(available)}"
            )
        if len(matches) != 1:
            raise RuntimeError(
                f"后端 {requested_backend!r} 注册了 {len(matches)} 次，请清理重复安装"
            )
        return matches[0]

    if len(entry_points) != 1:
        raise RuntimeError(
            "检测到多个后端，请使用 --backend 明确指定本次受测后端；"
            f"当前已注册后端: {', '.join(available)}"
        )
    return entry_points[0]


def instantiate_driver(driver_class):
    """实例化 Driver，并返回实例和当前 target。"""
    driver = driver_class()
    target = driver.get_current_target()
    if target is None:
        raise RuntimeError("Driver.get_current_target() 返回了 None")
    return driver, target


def run_check(index, total, name, function):
    """执行单项检查，输出统一结果，并在失败信息中保留检查名称。"""
    try:
        result = function()
    except Exception as error:
        print(f"  [FAIL] {index}/{total} {name}")
        raise RuntimeError(f"{name}失败：{error}") from error
    print(f"  [PASS] {index}/{total} {name}")
    return result


def run(args):
    separator = "=" * 68
    print(separator)
    print("Triton Anchor 企业后端冒烟测试")
    print(separator)

    # 部分企业后端在自动发现时会打印架构信息，将其收纳到固定区块中。
    initialization_output = io.StringIO()
    with contextlib.redirect_stdout(initialization_output):
        import triton
        import triton.backends
        import triton_anchor
        from triton.backends.driver import DriverBase

    print("\n[环境信息]")
    print(f"  Python          : {sys.version.split()[0]}")
    print(f"  Triton          : {getattr(triton, '__version__', 'unknown')}")
    print(f"  Triton 安装路径 : {triton.__file__}")
    print(f"  Anchor          : {getattr(triton_anchor, '__version__', 'unknown')}")
    print(f"  Anchor 安装路径 : {triton_anchor.__file__}")

    initialization_log = initialization_output.getvalue().strip()
    if initialization_log:
        print("\n[后端初始化日志]")
        for line in initialization_log.splitlines():
            print(f"  {line}")

    total_checks = 4
    print("\n[检查结果]")

    def discover_backend():
        return choose_entry_point(get_backend_entry_points(), args.backend)

    entry_point = run_check(
        1, total_checks, "发现企业后端 Entry Point", discover_backend
    )

    def get_registered_backend():
        registered = triton.backends.backends.get(entry_point.name)
        if registered is None:
            raise RuntimeError(
                f"triton.backends 中没有 {entry_point.name!r} 注册项"
            )
        return registered

    registered = run_check(
        2, total_checks, "验证 Triton 后端注册", get_registered_backend
    )

    def get_backend_classes():
        if registered.compiler is None or registered.driver is None:
            raise RuntimeError("注册项缺少 Compiler 或 Driver")
        if not callable(registered.compiler) or not hasattr(
            registered.compiler, "supports_target"
        ):
            raise TypeError("注册项中的 Compiler 接口不完整")
        if not callable(registered.driver):
            raise TypeError("注册项中的 Driver 接口不完整")
        return registered.compiler, registered.driver

    compiler_class, driver_class = run_check(
        3, total_checks, "读取已注册的 Compiler 和 Driver", get_backend_classes
    )

    def initialize_backend_driver():
        driver, target = instantiate_driver(driver_class)
        if not isinstance(driver, DriverBase):
            raise TypeError(f"{type(driver).__name__} 不是 DriverBase 子类")
        return target

    target = run_check(
        4, total_checks, "初始化 Driver 并获取 Target", initialize_backend_driver
    )

    print("\n[后端详情]")
    print(f"  后端名称       : {entry_point.name}")
    print(f"  Entry Point    : {entry_point.value}")
    print(f"  Compiler       : {compiler_class.__module__}.{compiler_class.__name__}")
    print(f"  Driver         : {driver_class.__module__}.{driver_class.__name__}")
    print(f"  Target         : {target}")

    print(f"\n{separator}")
    print(f"最终结果: PASS（{total_checks}/{total_checks} 项检查通过）")
    print(separator)


def main():
    parser = argparse.ArgumentParser(
        description="验证 triton-anchor 对已安装企业后端的发现、注册和基础接口"
    )
    parser.add_argument(
        "--backend",
        default=os.getenv("ANCHOR_BACKEND"),
        help="受测后端的 entry point 名称；默认读取 ANCHOR_BACKEND",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="失败时打印完整 Python traceback",
    )
    args = parser.parse_args()

    try:
        run(args)
    except Exception as error:
        separator = "=" * 68
        print(f"\n{separator}", file=sys.stderr)
        print("最终结果: FAIL", file=sys.stderr)
        print(f"失败原因: {error}", file=sys.stderr)
        print(separator, file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
