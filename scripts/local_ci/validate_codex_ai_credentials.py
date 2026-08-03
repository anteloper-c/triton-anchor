#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


class CredentialValidationError(ValueError):
    pass


def fail(message: str) -> None:
    raise CredentialValidationError(message)


def require_private_path(
    path: Path, *, expected_mode: int, kind: str
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        fail(f"{kind}不存在或不可访问：{path}")

    if stat.S_ISLNK(metadata.st_mode):
        fail(f"{kind}不得是符号链接：{path}")
    if kind.endswith("目录"):
        if not stat.S_ISDIR(metadata.st_mode):
            fail(f"{kind}不是目录：{path}")
    elif not stat.S_ISREG(metadata.st_mode):
        fail(f"{kind}不是普通文件：{path}")

    if metadata.st_uid != os.geteuid():
        fail(
            f"{kind}必须归当前用户所有：{path}"
            f"（当前 UID {os.geteuid()}，文件 UID {metadata.st_uid}）"
        )
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if actual_mode != expected_mode:
        fail(
            f"{kind}权限必须是 {expected_mode:03o}：{path}（当前为 {actual_mode:03o}）"
        )
    return metadata


def require_no_symlink_components(path: Path, *, kind: str) -> Path:
    if not path.is_absolute():
        fail(f"{kind}必须使用绝对路径：{path}")
    lexical = Path(os.path.abspath(path))
    try:
        resolved = lexical.resolve(strict=True)
    except OSError:
        fail(f"{kind}不存在或包含不可访问的路径组件：{path}")
    if lexical != resolved:
        fail(f"{kind}及其路径组件不得使用符号链接：{path}")
    return resolved


def validate_config(config_path: Path) -> None:
    try:
        raw_config = config_path.read_text(encoding="utf-8")
        if tomllib is not None:
            config: dict[str, Any] = tomllib.loads(raw_config)
        else:
            config = parse_toml_fallback(raw_config)
    except (OSError, UnicodeError, TypeError, ValueError):
        fail(f"Codex config.toml 不是有效 TOML：{config_path}")

    provider_name = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(provider_name, str) or not provider_name.strip():
        fail("Codex config.toml 缺少非空 model_provider")
    if not isinstance(providers, dict) or not isinstance(
        providers.get(provider_name), dict
    ):
        fail(f"Codex config.toml 缺少 model_providers.{provider_name} 配置")

    provider = providers[provider_name]
    if provider.get("wire_api") != "responses":
        fail('Codex AI CI 的 provider 必须使用 wire_api = "responses"')
    if provider.get("requires_openai_auth") is not True:
        fail("Codex AI CI 的 provider 必须设置 requires_openai_auth = true")
    base_url = provider.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        fail("Codex AI CI 的 provider 缺少非空 base_url")


def strip_toml_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == "#":
            return line[:index]
    if quote:
        raise ValueError("unterminated string")
    return line


def parse_toml_name(raw_name: str) -> list[str]:
    parts: list[str] = []
    token: list[str] = []
    quote = ""
    escaped = False
    for character in raw_name.strip():
        if quote:
            token.append(character)
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
        elif character in {'"', "'"}:
            if token:
                raise ValueError("invalid quoted key")
            quote = character
            token.append(character)
        elif character == ".":
            parts.append(parse_toml_name_part("".join(token)))
            token = []
        else:
            token.append(character)
    if quote:
        raise ValueError("unterminated quoted key")
    parts.append(parse_toml_name_part("".join(token)))
    return parts


def parse_toml_name_part(raw_part: str) -> str:
    part = raw_part.strip()
    if not part:
        raise ValueError("empty key")
    if part.startswith('"'):
        if not part.endswith('"'):
            raise ValueError("unterminated quoted key")
        value = json.loads(part)
        if not isinstance(value, str) or not value:
            raise ValueError("empty quoted key")
        return value
    if part.startswith("'"):
        if not part.endswith("'") or len(part) < 3 or "'" in part[1:-1]:
            raise ValueError("invalid literal key")
        return part[1:-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", part):
        raise ValueError("invalid bare key")
    return part


def split_toml_assignment(line: str) -> tuple[str, str]:
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == "=":
            return line[:index], line[index + 1 :]
    raise ValueError("missing assignment")


def validate_balanced_toml_value(value: str) -> None:
    pairs = {"]": "[", "}": "{"}
    stack: list[str] = []
    quote = ""
    escaped = False
    for character in value:
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character in "[{":
            stack.append(character)
        elif character in "]}" and (not stack or stack.pop() != pairs[character]):
            raise ValueError("unbalanced value")
    if quote or stack:
        raise ValueError("unterminated value")


def parse_toml_value(raw_value: str) -> Any:
    value = raw_value.strip()
    if not value:
        raise ValueError("empty value")
    if value.startswith('"'):
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError("invalid string")
        return parsed
    if value.startswith("'"):
        if not value.endswith("'") or "'" in value[1:-1]:
            raise ValueError("invalid literal string")
        return value[1:-1]
    if value in {"true", "false"}:
        return value == "true"
    if re.fullmatch(r"[+-]?[0-9][0-9_]*", value):
        return int(value.replace("_", ""))
    if re.fullmatch(r"[+-]?(?:[0-9][0-9_]*)?\.[0-9][0-9_]*(?:[eE][+-]?[0-9]+)?", value):
        return float(value.replace("_", ""))
    if value.startswith(("[", "{")):
        validate_balanced_toml_value(value)
        return value
    if re.fullmatch(r"[0-9TtZz:+_.-]+", value):
        return value
    raise ValueError("unsupported or invalid value")


def set_toml_value(root: dict[str, Any], path: list[str], value: Any) -> None:
    current = root
    for part in path[:-1]:
        existing = current.setdefault(part, {})
        if not isinstance(existing, dict):
            raise TypeError("key/table conflict")
        current = existing
    if path[-1] in current:
        raise ValueError("duplicate key")
    current[path[-1]] = value


def parse_toml_fallback(raw_config: str) -> dict[str, Any]:
    config: dict[str, Any] = {}
    current_table: list[str] = []
    for raw_line in raw_config.splitlines():
        line = strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("["):
            if not line.endswith("]") or line.startswith("[[") or line.endswith("]]"):
                raise ValueError("invalid table")
            current_table = parse_toml_name(line[1:-1])
            table: dict[str, Any] = config
            for part in current_table:
                existing = table.setdefault(part, {})
                if not isinstance(existing, dict):
                    raise TypeError("key/table conflict")
                table = existing
            continue
        raw_key, raw_value = split_toml_assignment(line)
        set_toml_value(
            config,
            [*current_table, *parse_toml_name(raw_key)],
            parse_toml_value(raw_value),
        )
    return config


def validate_auth(auth_path: Path) -> None:
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail(f"Codex auth.json 不是有效 UTF-8 JSON：{auth_path}")
    if not isinstance(payload, dict):
        fail("Codex auth.json 顶层必须是 JSON 对象")
    api_key = payload.get("OPENAI_API_KEY")
    if not isinstance(api_key, str) or not api_key.strip():
        fail("Codex auth.json 缺少非空 OPENAI_API_KEY")


def validate_credentials(codex_home: Path, personal_codex_home: Path) -> Path:
    resolved_home = require_no_symlink_components(codex_home, kind="CODEX_AI_CI_HOME")
    personal_home = personal_codex_home.expanduser().resolve(strict=False)
    if resolved_home == personal_home or personal_home in resolved_home.parents:
        fail(f"CODEX_AI_CI_HOME 不得使用个人 Codex 配置目录或其子目录：{resolved_home}")

    require_private_path(
        resolved_home, expected_mode=0o700, kind="Codex AI CI 凭据目录"
    )
    expected_names = {"config.toml", "auth.json"}
    try:
        actual_names = {entry.name for entry in resolved_home.iterdir()}
    except OSError:
        fail(f"无法读取 Codex AI CI 凭据目录：{resolved_home}")
    missing_names = sorted(expected_names - actual_names)
    unexpected_names = sorted(actual_names - expected_names)
    if missing_names:
        fail(f"Codex AI CI 凭据目录缺少文件：{', '.join(missing_names)}")
    if unexpected_names:
        fail(f"Codex AI CI 凭据目录包含多余文件：{', '.join(unexpected_names)}")

    files: dict[str, Path] = {}
    for name in sorted(expected_names):
        path = resolved_home / name
        metadata = require_private_path(
            path, expected_mode=0o600, kind=f"Codex AI CI 凭据文件 {name}"
        )
        if metadata.st_nlink != 1:
            fail(f"Codex AI CI 凭据文件不得是硬链接：{path}")
        if path.resolve(strict=True).parent != resolved_home:
            fail(f"Codex AI CI 凭据文件必须直接位于独立凭据目录：{path}")
        files[name] = path

    validate_config(files["config.toml"])
    validate_auth(files["auth.json"])
    return resolved_home


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 Codex AI CI 独立静态凭据")
    parser.add_argument("--codex-home", required=True)
    parser.add_argument(
        "--personal-codex-home",
        default=str(Path.home() / ".codex"),
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    try:
        resolved_home = validate_credentials(
            Path(args.codex_home), Path(args.personal_codex_home)
        )
    except CredentialValidationError as exc:
        print(f"Codex AI CI 凭据校验失败：{exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"Codex AI CI 独立凭据校验通过：{resolved_home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
