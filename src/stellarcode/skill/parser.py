"""Parse and validate SKILL.md frontmatter before a file becomes available to agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FrontmatterResult:
    frontmatter: dict[str, Any]
    body: str
    warnings: tuple[str, ...]


def parse_frontmatter(full_text: str | None) -> FrontmatterResult:
    if full_text is None:
        return FrontmatterResult({}, "", ("SKILL.md content is null",))

    normalized = full_text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return FrontmatterResult({}, normalized, ("missing frontmatter opening marker ---",))

    lines = normalized.split("\n")
    try:
        end_index = lines.index("---", 1)
    except ValueError:
        return FrontmatterResult({}, normalized, ("missing frontmatter closing marker ---",))

    warnings: list[str] = []
    frontmatter = _parse_fields(lines[1:end_index], warnings)
    body = "\n".join(lines[end_index + 1 :])
    return FrontmatterResult(frontmatter, body, tuple(warnings))


def _parse_fields(lines: list[str], warnings: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue

        colon = _find_unquoted_colon(line)
        if colon < 0:
            warnings.append(f"cannot parse frontmatter line: {line}")
            index += 1
            continue

        key = line[:colon].strip()
        raw_value = line[colon + 1 :].strip()
        if not key:
            warnings.append(f"frontmatter line is missing a key: {line}")
            index += 1
            continue
        if not raw_value:
            warnings.append(f"frontmatter field '{key}' has no value or uses unsupported nesting")
            index += 1
            continue
        if raw_value.startswith("{"):
            warnings.append(f"frontmatter field '{key}' uses an unsupported nested object")
            index += 1
            continue

        if raw_value.startswith("|"):
            index += 1
            value_lines: list[str] = []
            base_indent: int | None = None
            while index < len(lines):
                next_line = lines[index]
                if not next_line.strip():
                    value_lines.append("")
                    index += 1
                    continue
                indent = len(next_line) - len(next_line.lstrip(" "))
                if indent == 0:
                    break
                if base_indent is None:
                    base_indent = indent
                if indent < base_indent:
                    break
                value_lines.append(next_line[base_indent:])
                index += 1
            result[key] = re.sub(r"\s+", " ", "\n".join(value_lines)).strip()
            continue

        if raw_value.startswith("[") and raw_value.endswith("]"):
            inner = raw_value[1:-1].strip()
            result[key] = (
                [_unquote(part.strip()) for part in inner.split(",") if part.strip()]
                if inner
                else []
            )
            index += 1
            continue

        result[key] = _unquote(raw_value)
        index += 1
    return result


def _find_unquoted_colon(line: str) -> int:
    in_single = False
    in_double = False
    for index, character in enumerate(line):
        if character == "'" and not in_double:
            in_single = not in_single
        elif character == '"' and not in_single:
            in_double = not in_double
        elif character == ":" and not in_single and not in_double:
            return index
    return -1


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
