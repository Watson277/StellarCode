"""Skill loading tools and task-local buffers for progressive prompt injection.

The Skill index belongs in the system prompt; full guidance is bounded and inserted into
the next model round only after explicit reference or ``load_skill``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import re

from stellarcode.skill.context import SkillContextBuffer
from stellarcode.skill.registry import SkillRegistry
from stellarcode.tools import ToolDefinition, ToolRegistry


MAX_SKILL_BODY_CHARACTERS = 5 * 1024
_EXPLICIT_SKILL_REFERENCE = re.compile(r"(?<![\w@])@skill:([A-Za-z0-9][A-Za-z0-9._-]*)")
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


def render_skill_guidance(skill_name: str, skill: object) -> tuple[str, int]:
    """Render the bounded Skill context shared by tools and desktop references."""

    body = str(getattr(skill, "body", ""))
    original_length = len(body)
    references_dir = getattr(skill, "references_dir", None)
    if references_dir:
        body = f"Reference directory: {references_dir.resolve()}\n\n{body}"
    if len(body) > MAX_SKILL_BODY_CHARACTERS:
        body = (
            f"{body[:MAX_SKILL_BODY_CHARACTERS]}\n\n"
            "...(skill body truncated; open its SKILL.md and references when more "
            f"detail is needed: {skill_name})"
        )
    return body, original_length


def explicit_skill_context(text: str, skill_registry: SkillRegistry) -> str:
    """Return guidance for enabled Skills explicitly referenced by the user.

    References are de-duplicated and capped by ``SkillContextBuffer``'s normal
    three-Skill budget. Unknown or disabled names remain ordinary user text.
    """

    names = list(dict.fromkeys(_EXPLICIT_SKILL_REFERENCE.findall(text)))[:3]
    sections: list[str] = []
    for name in names:
        skill = skill_registry.find_skill(name)
        if skill is None:
            continue
        body, _ = render_skill_guidance(name, skill)
        sections.append(f"## Explicitly referenced Skill: {name}\n{body.strip()}")
    if not sections:
        return ""
    return "\n\n".join(sections)


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

        body, original_length = render_skill_guidance(name, skill)
        active_buffer = _ACTIVE_CONTEXT_BUFFER.get()
        target_buffer = active_buffer if active_buffer is not None else context_buffer
        if target_buffer is None:
            return "load_skill failed: no active Agent skill context is available"
        target_buffer.push(name, body)
        return (
            f"Loaded skill '{name}' ({original_length} characters). Its full guidance "
            f"will be supplied to the next model round under '## 已加载 Skill：{name}'."
        )

    tool_registry.register(
        ToolDefinition(
            name="load_skill",
            description=(
                "Load the full SKILL.md guidance for a skill listed in the system "
                "prompt's available Skills section. Pass the exact kebab-case name. "
                "The body is supplied to the next model round; do not repeatedly "
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
