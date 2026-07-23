#!/usr/bin/env python3
"""Compare the stable Python API of two triton-anchor source trees.

The checker parses source with ``ast``. It never imports candidate code, so it
is suitable for pull requests from forks and does not require Triton/LLVM.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any


BREAKING_EXIT_CODE = 2


class ContractError(RuntimeError):
    """Raised for an invalid contract or an unreadable source tree."""


def _source_text(node: ast.AST | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def _decorator_name(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    return _source_text(target) or ""


def _has_decorator(node: ast.AST, name: str) -> bool:
    decorators = getattr(node, "decorator_list", [])
    return any(_decorator_name(item).split(".")[-1] == name for item in decorators)


def _module_file(root: Path, module: str) -> tuple[Path, bool]:
    relative = Path("python", *module.split("."))
    module_file = root / relative.with_suffix(".py")
    if module_file.is_file():
        return module_file, False
    package_file = root / relative / "__init__.py"
    if package_file.is_file():
        return package_file, True
    return module_file, False


def _parse_module(root: Path, module: str) -> tuple[ast.Module | None, bool]:
    source_file, is_package = _module_file(root, module)
    if not source_file.is_file():
        return None, is_package
    try:
        return ast.parse(source_file.read_text(encoding="utf-8"), str(source_file)), is_package
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise ContractError(f"Cannot parse {source_file}: {exc}") from exc


def _resolve_import(module: str, imported: str | None, level: int, is_package: bool) -> str:
    if level == 0:
        return imported or ""
    package = module.split(".") if is_package else module.split(".")[:-1]
    keep = max(0, len(package) - (level - 1))
    parts = package[:keep]
    if imported:
        parts.extend(imported.split("."))
    return ".".join(parts)


def _exports(tree: ast.Module, module: str, is_package: bool) -> dict[str, str]:
    exports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            exports[node.name] = f"{module}.{node.name}"
        elif isinstance(node, ast.ImportFrom):
            source_module = _resolve_import(module, node.module, node.level, is_package)
            for alias in node.names:
                if alias.name == "*":
                    continue
                exported_name = alias.asname or alias.name
                exports[exported_name] = f"{source_module}.{alias.name}"
    return exports


def _parameter(name: str, kind: str, annotation: ast.AST | None, default: ast.AST | None,
               has_default: bool) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "annotation": _source_text(annotation),
        "has_default": has_default,
        "default": _source_text(default) if has_default else None,
    }


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    args = node.args
    positional = list(args.posonlyargs) + list(args.args)
    default_offset = len(positional) - len(args.defaults)
    parameters: list[dict[str, Any]] = []

    for index, arg in enumerate(positional):
        has_default = index >= default_offset
        default = args.defaults[index - default_offset] if has_default else None
        kind = "positional_only" if index < len(args.posonlyargs) else "positional_or_keyword"
        parameters.append(_parameter(arg.arg, kind, arg.annotation, default, has_default))

    if args.vararg:
        parameters.append(_parameter(args.vararg.arg, "var_positional", args.vararg.annotation, None, False))

    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(_parameter(arg.arg, "keyword_only", arg.annotation, default, default is not None))

    if args.kwarg:
        parameters.append(_parameter(args.kwarg.arg, "var_keyword", args.kwarg.annotation, None, False))

    decorators = {
        name
        for name in ("abstractmethod", "classmethod", "property", "staticmethod")
        if _has_decorator(node, name)
    }
    return {
        "async": isinstance(node, ast.AsyncFunctionDef),
        "decorators": sorted(decorators),
        "parameters": parameters,
        "return_annotation": _source_text(node.returns),
    }


def _class_kind(node: ast.ClassDef) -> str:
    if _has_decorator(node, "dataclass"):
        return "dataclass"
    base_names = {_source_text(base).split(".")[-1] for base in node.bases}
    if "Enum" in base_names:
        return "enum"
    if base_names & {"BaseException", "Exception"}:
        return "exception"
    return "class"


def _dataclass_options(node: ast.ClassDef) -> dict[str, str]:
    for decorator in node.decorator_list:
        if _decorator_name(decorator).split(".")[-1] != "dataclass":
            continue
        if not isinstance(decorator, ast.Call):
            return {}
        return {keyword.arg or "**": _source_text(keyword.value) or "" for keyword in decorator.keywords}
    return {}


def _dataclass_fields(node: ast.ClassDef) -> list[dict[str, Any]]:
    fields = []
    for item in node.body:
        if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
            continue
        annotation = _source_text(item.annotation)
        if annotation and (annotation == "ClassVar" or annotation.startswith("ClassVar[")):
            continue
        fields.append({
            "name": item.target.id,
            "annotation": annotation,
            "has_default": item.value is not None,
            "default": _source_text(item.value),
        })
    return fields


def _enum_members(node: ast.ClassDef) -> dict[str, str | None]:
    members: dict[str, str | None] = {}
    for item in node.body:
        if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
            name = item.targets[0].id
            if not name.startswith("_"):
                members[name] = _source_text(item.value)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            name = item.target.id
            if not name.startswith("_"):
                members[name] = _source_text(item.value)
    return members


def _class_snapshot(node: ast.ClassDef, config: dict[str, Any]) -> dict[str, Any]:
    methods = {
        item.name: item
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    selected_methods = {
        name: _signature(methods[name]) if name in methods else None
        for name in config.get("methods", [])
    }
    abstract_methods = sorted(
        name for name, method in methods.items() if _has_decorator(method, "abstractmethod")
    )
    kind = _class_kind(node)
    snapshot: dict[str, Any] = {
        "kind": kind,
        "bases": [_source_text(base) for base in node.bases],
        "methods": selected_methods,
        "abstract_methods": abstract_methods,
    }
    if kind == "dataclass":
        snapshot["dataclass_options"] = _dataclass_options(node)
        snapshot["fields"] = _dataclass_fields(node)
    if kind == "enum":
        snapshot["members"] = _enum_members(node)
    return snapshot


def extract_contract(root: Path, scope: dict[str, Any]) -> dict[str, Any]:
    """Extract only the symbols selected by the contract scope."""
    modules: dict[str, Any] = {}
    for module, config in scope["modules"].items():
        tree, is_package = _parse_module(root, module)
        module_snapshot: dict[str, Any] = {"missing": tree is None}
        if tree is None:
            module_snapshot["exports"] = {name: None for name in config.get("exports", [])}
            module_snapshot["functions"] = {name: None for name in config.get("functions", [])}
            module_snapshot["classes"] = {name: None for name in config.get("classes", {})}
            modules[module] = module_snapshot
            continue

        definitions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        available_exports = _exports(tree, module, is_package)
        module_snapshot["exports"] = {
            name: available_exports.get(name) for name in config.get("exports", [])
        }
        module_snapshot["functions"] = {
            name: _signature(definitions[name])
            if isinstance(definitions.get(name), (ast.FunctionDef, ast.AsyncFunctionDef))
            else None
            for name in config.get("functions", [])
        }
        classes: dict[str, Any] = {}
        for name, class_config in config.get("classes", {}).items():
            node = definitions.get(name)
            classes[name] = _class_snapshot(node, class_config) if isinstance(node, ast.ClassDef) else None
        module_snapshot["classes"] = classes
        modules[module] = module_snapshot
    return {"modules": modules}


def _change(changes: list[dict[str, str]], severity: str, code: str, symbol: str,
            message: str) -> None:
    changes.append({"severity": severity, "code": code, "symbol": symbol, "message": message})


def _compare_signature(base: dict[str, Any], candidate: dict[str, Any], symbol: str,
                       changes: list[dict[str, str]]) -> None:
    if base["async"] != candidate["async"]:
        _change(changes, "breaking", "callable-kind-changed", symbol, "sync/async callable kind changed")

    invocation_decorators = {"classmethod", "property", "staticmethod"}
    base_decorators = set(base["decorators"])
    candidate_decorators = set(candidate["decorators"])
    if base_decorators & invocation_decorators != candidate_decorators & invocation_decorators:
        _change(changes, "breaking", "call-style-changed", symbol, "classmethod, staticmethod, or property behavior changed")
    if "abstractmethod" not in base_decorators and "abstractmethod" in candidate_decorators:
        _change(changes, "breaking", "method-became-abstract", symbol, "existing method became abstract")

    base_params = base["parameters"]
    candidate_params = candidate["parameters"]
    base_by_name = {item["name"]: item for item in base_params}
    candidate_by_name = {item["name"]: item for item in candidate_params}
    base_names = [item["name"] for item in base_params]
    candidate_existing_names = [item["name"] for item in candidate_params if item["name"] in base_by_name]

    if candidate_existing_names != [name for name in base_names if name in candidate_by_name]:
        _change(changes, "breaking", "parameter-order-changed", symbol, "existing parameters were reordered")

    for name, old in base_by_name.items():
        current = candidate_by_name.get(name)
        parameter_symbol = f"{symbol}({name})"
        if current is None:
            _change(changes, "breaking", "parameter-removed", parameter_symbol, "parameter was removed or renamed")
            continue
        if old["kind"] != current["kind"]:
            _change(changes, "breaking", "parameter-kind-changed", parameter_symbol, "parameter calling convention changed")
        if old["has_default"] != current["has_default"]:
            if old["has_default"]:
                _change(changes, "breaking", "parameter-became-required", parameter_symbol, "optional parameter became required")
            else:
                _change(changes, "compatible", "parameter-became-optional", parameter_symbol, "required parameter became optional")
        elif old["has_default"] and old["default"] != current["default"]:
            _change(changes, "breaking", "parameter-default-changed", parameter_symbol, "parameter default value changed")
        if old["annotation"] != current["annotation"]:
            _change(changes, "warning", "parameter-annotation-changed", parameter_symbol, "parameter annotation changed")

    positional_kinds = {"positional_only", "positional_or_keyword"}
    for index, current in enumerate(candidate_params):
        if current["name"] in base_by_name:
            continue
        parameter_symbol = f"{symbol}({current['name']})"
        variadic = current["kind"] in {"var_positional", "var_keyword"}
        if not current["has_default"] and not variadic:
            _change(changes, "breaking", "required-parameter-added", parameter_symbol, "new required parameter was added")
            continue
        later_old_positional = any(
            item["name"] in base_by_name and item["kind"] in positional_kinds
            for item in candidate_params[index + 1:]
        )
        if current["kind"] in positional_kinds and later_old_positional:
            _change(changes, "breaking", "positional-parameter-inserted", parameter_symbol, "new positional parameter shifts existing positional calls")
        else:
            _change(changes, "compatible", "optional-parameter-added", parameter_symbol, "new optional parameter was added")

    if base["return_annotation"] != candidate["return_annotation"]:
        _change(changes, "warning", "return-annotation-changed", symbol, "return annotation changed")


def _compare_fields(base: list[dict[str, Any]], candidate: list[dict[str, Any]], symbol: str,
                    changes: list[dict[str, str]]) -> None:
    base_by_name = {item["name"]: item for item in base}
    candidate_by_name = {item["name"]: item for item in candidate}
    candidate_existing = [item["name"] for item in candidate if item["name"] in base_by_name]
    expected_existing = [item["name"] for item in base if item["name"] in candidate_by_name]
    if candidate_existing != expected_existing:
        _change(changes, "breaking", "dataclass-field-order-changed", symbol, "existing dataclass fields were reordered")

    for name, old in base_by_name.items():
        current = candidate_by_name.get(name)
        field_symbol = f"{symbol}.{name}"
        if current is None:
            _change(changes, "breaking", "dataclass-field-removed", field_symbol, "dataclass field was removed or renamed")
            continue
        if old["has_default"] != current["has_default"]:
            severity = "breaking" if old["has_default"] else "compatible"
            code = "dataclass-field-became-required" if old["has_default"] else "dataclass-field-became-optional"
            _change(changes, severity, code, field_symbol, "dataclass field required/optional status changed")
        elif old["has_default"] and old["default"] != current["default"]:
            _change(changes, "breaking", "dataclass-field-default-changed", field_symbol, "dataclass field default changed")
        if old["annotation"] != current["annotation"]:
            _change(changes, "warning", "dataclass-field-annotation-changed", field_symbol, "dataclass field annotation changed")

    last_old_index = max((index for index, item in enumerate(candidate) if item["name"] in base_by_name), default=-1)
    for index, current in enumerate(candidate):
        if current["name"] in base_by_name:
            continue
        field_symbol = f"{symbol}.{current['name']}"
        if not current["has_default"]:
            _change(changes, "breaking", "required-dataclass-field-added", field_symbol, "new required dataclass field was added")
        elif index < last_old_index:
            _change(changes, "breaking", "dataclass-field-inserted", field_symbol, "new field shifts existing positional construction")
        else:
            _change(changes, "compatible", "optional-dataclass-field-added", field_symbol, "new optional dataclass field was appended")


def compare_contracts(base: dict[str, Any], candidate: dict[str, Any], scope: dict[str, Any],
                      scope_changed: bool = False) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    if scope_changed:
        _change(changes, "warning", "scope-file-changed", "api_contract/public_api.json",
                "the candidate scope changed; this run still uses the reviewed base-branch scope")

    for module, config in scope["modules"].items():
        old_module = base["modules"][module]
        new_module = candidate["modules"][module]
        if new_module["missing"]:
            _change(changes, "breaking", "module-removed", module, "public API module is missing")

        for name in config.get("exports", []):
            symbol = f"{module}.{name}"
            old = old_module["exports"][name]
            current = new_module["exports"][name]
            if old is None:
                raise ContractError(f"Base contract export does not exist: {symbol}")
            if current is None:
                _change(changes, "breaking", "export-removed", symbol, "public export was removed")
            elif old != current:
                _change(changes, "breaking", "export-target-changed", symbol, "public export points to a different implementation symbol")

        for name in config.get("functions", []):
            symbol = f"{module}.{name}"
            old = old_module["functions"][name]
            current = new_module["functions"][name]
            if old is None:
                raise ContractError(f"Base contract function does not exist: {symbol}")
            if current is None:
                _change(changes, "breaking", "function-removed", symbol, "public function was removed or changed into another symbol kind")
            else:
                _compare_signature(old, current, symbol, changes)

        for name, class_config in config.get("classes", {}).items():
            symbol = f"{module}.{name}"
            old = old_module["classes"][name]
            current = new_module["classes"][name]
            if old is None:
                raise ContractError(f"Base contract class does not exist: {symbol}")
            expected_kind = class_config["kind"]
            if old["kind"] != expected_kind:
                raise ContractError(f"Base contract kind mismatch for {symbol}: expected {expected_kind}, found {old['kind']}")
            if current is None:
                _change(changes, "breaking", "class-removed", symbol, "public class was removed or changed into another symbol kind")
                continue
            if current["kind"] != old["kind"]:
                _change(changes, "breaking", "class-kind-changed", symbol, "class kind changed")
                continue
            if current["bases"] != old["bases"]:
                _change(changes, "breaking", "class-bases-changed", symbol, "public class base classes changed")
            if old["kind"] == "dataclass":
                if current["dataclass_options"] != old["dataclass_options"]:
                    _change(changes, "breaking", "dataclass-options-changed", symbol, "dataclass options changed")
                _compare_fields(old["fields"], current["fields"], symbol, changes)
            if old["kind"] == "enum":
                for member, value in old["members"].items():
                    member_symbol = f"{symbol}.{member}"
                    if member not in current["members"]:
                        _change(changes, "breaking", "enum-member-removed", member_symbol, "enum member was removed or renamed")
                    elif current["members"][member] != value:
                        _change(changes, "breaking", "enum-value-changed", member_symbol, "enum member value changed")
                for member in current["members"].keys() - old["members"].keys():
                    _change(changes, "compatible", "enum-member-added", f"{symbol}.{member}", "enum member was added")
            for method in class_config.get("methods", []):
                method_symbol = f"{symbol}.{method}"
                old_method = old["methods"][method]
                current_method = current["methods"][method]
                if old_method is None:
                    raise ContractError(f"Base contract method does not exist: {method_symbol}")
                if current_method is None:
                    _change(changes, "breaking", "method-removed", method_symbol, "public method was removed")
                else:
                    _compare_signature(old_method, current_method, method_symbol, changes)
            if class_config.get("track_added_abstract_methods"):
                for method in set(current["abstract_methods"]) - set(old["abstract_methods"]):
                    _change(changes, "breaking", "abstract-method-added", f"{symbol}.{method}", "new abstract method breaks existing implementations")
    return changes


def _load_scope(path: Path) -> dict[str, Any]:
    try:
        scope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"Cannot read API scope {path}: {exc}") from exc
    if scope.get("schema_version") != 1 or not isinstance(scope.get("modules"), dict):
        raise ContractError(f"Unsupported or invalid API scope: {path}")
    return scope


def run_check(base_root: Path, candidate_root: Path, scope_path: Path,
              candidate_scope_path: Path | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    scope = _load_scope(scope_path)
    candidate_scope_missing = bool(candidate_scope_path and not candidate_scope_path.is_file())
    candidate_scope = _load_scope(candidate_scope_path) if candidate_scope_path and candidate_scope_path.is_file() else scope
    base = extract_contract(base_root, scope)
    candidate = extract_contract(candidate_root, scope)
    scope_changed = candidate_scope_missing or scope != candidate_scope
    changes = compare_contracts(base, candidate, scope, scope_changed=scope_changed)
    if candidate_scope_missing:
        _change(changes, "breaking", "scope-file-removed", "api_contract/public_api.json",
                "the public API scope file is missing from the candidate revision")
    breaking_count = sum(change["severity"] == "breaking" for change in changes)
    warning_count = sum(change["severity"] == "warning" for change in changes)
    return {
        "schema_version": 1,
        "status": "breaking" if breaking_count else "compatible",
        "breaking_count": breaking_count,
        "warning_count": warning_count,
        **(metadata or {}),
        "changes": changes,
    }


def _markdown(result: dict[str, Any]) -> str:
    if result["status"] == "error":
        return f"# Public API compatibility\n\nChecker error: {result['error']}\n"
    status = "Breaking changes detected" if result["status"] == "breaking" else "Compatible"
    lines = [
        "# Public API compatibility",
        "",
        f"**Result:** {status}",
        f"**Breaking changes:** {result['breaking_count']}  ",
        f"**Warnings:** {result['warning_count']}",
    ]
    for severity, heading in (("breaking", "Breaking changes"), ("warning", "Warnings"), ("compatible", "Compatible additions")):
        selected = [change for change in result["changes"] if change["severity"] == severity]
        if selected:
            lines.extend(["", f"## {heading}", ""])
            lines.extend(f"- `{change['symbol']}`: {change['message']} (`{change['code']}`)" for change in selected)
    return "\n".join(lines) + "\n"


def _write_result(result: dict[str, Any], json_output: Path, markdown_output: Path) -> None:
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_output.write_text(_markdown(result), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--candidate-scope", type=Path)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--event-name", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--pr-number", type=int, default=0)
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--head-sha", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    metadata = {
        "event_name": args.event_name,
        "actor": args.actor,
        "pr_number": args.pr_number,
        "base_sha": args.base_sha,
        "head_sha": args.head_sha,
    }
    try:
        result = run_check(
            args.base_root,
            args.candidate_root,
            args.scope,
            candidate_scope_path=args.candidate_scope,
            metadata=metadata,
        )
    except Exception as exc:
        result = {
            "schema_version": 1,
            "status": "error",
            "breaking_count": 0,
            "warning_count": 0,
            **metadata,
            "changes": [],
            "error": str(exc),
        }
        _write_result(result, args.json_output, args.markdown_output)
        print(f"API compatibility checker error: {exc}", file=sys.stderr)
        return 1

    _write_result(result, args.json_output, args.markdown_output)
    print(_markdown(result), end="")
    return BREAKING_EXIT_CODE if result["status"] == "breaking" else 0


if __name__ == "__main__":
    raise SystemExit(main())
