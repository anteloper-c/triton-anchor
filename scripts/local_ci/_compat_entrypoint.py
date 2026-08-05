"""Load a relocated Local CI script through its legacy path."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def run_legacy_entrypoint(relative_target: str, namespace: dict[str, Any]) -> None:
    target = Path(__file__).resolve().parent / relative_target
    target_dir = str(target.parent)
    run_as_main = namespace.get("__name__") == "__main__"
    module_name = "__main__" if run_as_main else str(namespace["__name__"])
    spec = importlib.util.spec_from_file_location(module_name, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load relocated Local CI script: {target}")
    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    sys.path.insert(0, target_dir)
    try:
        spec.loader.exec_module(module)
        namespace.update(
            {name: value for name, value in vars(module).items() if not name.startswith("__")}
        )
    finally:
        sys.path.remove(target_dir)
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
