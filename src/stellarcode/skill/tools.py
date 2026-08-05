from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from stellarcode.skill.context import SkillContextBuffer
from stellarcode.skill.registry import SkillRegistry
from stellarcode.tools import ToolDefinition, ToolRegistry


MAX_SKILL_BODY_CHARACTERS = 5 * 1024
_ACTIVE_CONTEXT_BUFFER: ContextVar[SkillContextBuffer | None] = ContextVar(
    "stellarcode_skill_context_buffer",
    default=None,
)


@contextmanager
def activate_skill_context(
    context_buffer: SkillContextBuffer | None,
) -> Iterator[None]:
    token = _ACTIVE_CONTEXT_BUFFER.set(context_buffer)
    try:
        yield
    finally:
        _ACTIVE_CONTEXT_BUFFER.reset(token)


def register_skill_tools(
    tool_registry: ToolRegistry,
    skill_registry: SkillRegistry,
    context_buffer: SkillContextBuffer | None = None,
) -> None:
    def load_skill(name: str) -> str:
        if not name.strip():
            return "load_skill failed: name cannot be empty"
        skill = skill_registry.find_skill(name)
        if skill is None:
            if skill_registry.find_any_skill(name) is None:
                return f"Skill '{name}' was not found; use /skill list to inspect skills"
            return f"Skill '{name}' is disabled; use /skill on {name} to enable it"

        original_length = len(skill.body)
        body = skill.body
        if skill.references_dir:
            body = (
                f"Reference directory: {skill.references_dir.resolve()}\n\n{body}"
            )
        if len(body) > MAX_SKILL_BODY_CHARACTERS:
            body = (
                f"{body[:MAX_SKILL_BODY_CHARACTERS]}\n\n"
                f"...(skill body truncated; use /skill show {name} for the full content)"
            )
        active_buffer = _ACTIVE_CONTEXT_BUFFER.get()
        target_buffer = active_buffer if active_buffer is not None else context_buffer
        if target_buffer is None:
            return "load_skill failed: no active Agent skill context is available"
        target_buffer.push(name, body)
        return (
            f"Loaded skill '{name}' ({original_length} characters). Its full guidance "
            f"will appear in the next user message under '## 已加载 Skill：{name}'."
        )

    tool_registry.register(
        ToolDefinition(
            name="load_skill",
            description=(
                "Load the full SKILL.md guidance for a skill listed in the system "
                "prompt's available Skills section. Pass the exact kebab-case name. "
                "The body is injected into the next user message; do not repeatedly "
                "load the same skill."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact skill name, for example web-access.",
                    }
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=load_skill,
        )
    )
