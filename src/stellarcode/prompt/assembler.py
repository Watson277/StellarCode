"""One system-prompt assembly path shared by ReAct, Plan, and Team agents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import platform
from typing import Any, Iterable

from stellarcode.llm.types import estimate_request_tokens

from stellarcode.prompt.policies import (
    CORE_POLICY,
    ENGINEERING_POLICY,
    capability_sections,
    rag_mode_section,
)
from stellarcode.prompt.context_messages import ContextKind, untrusted_context_message


PROMPT_VERSION = "stellarcode.prompt/v1"


class PromptMode(str, Enum):
    """The role whose model-facing system prompt is being assembled."""

    REACT = "react"
    PLAN_BUILDER = "plan-builder"
    PLAN_EXECUTOR = "plan-executor"
    TEAM_PLANNER = "team-planner"
    TEAM_WORKER = "team-worker"
    TEAM_REVIEWER = "team-reviewer"

    @property
    def tools_enabled(self) -> bool:
        return self in {
            PromptMode.REACT,
            PromptMode.PLAN_EXECUTOR,
            PromptMode.TEAM_WORKER,
        }


@dataclass(frozen=True, slots=True)
class PromptContext:
    """Typed prompt layers supplied by one runtime turn.

    ``base_prompt`` contains the stable role policy. Skill metadata is rebuilt in that
    policy, while query-sensitive Memory becomes a separate user-role context message
    that stays attached to the turn which retrieved it.
    """

    base_prompt: str
    runtime_context: str = ""
    skill_index: str = ""
    memory_context: str = ""
    available_tools: frozenset[str] = frozenset()
    rag_auto_retrieval: bool | None = None


@dataclass(frozen=True, slots=True)
class PromptAssembly:
    """Stable system instructions plus lower-trust contextual user messages."""

    system_prompt: str
    context_messages: tuple[dict[str, object], ...] = ()
    layers: tuple["PromptLayer", ...] = ()

    def snapshot(
        self,
        mode: PromptMode,
        *,
        additional_layers: Iterable["PromptLayer"] = (),
    ) -> "PromptSnapshot":
        """Create an immutable, auditable view of the exact prompt layers."""

        system_layers = tuple(layer for layer in self.layers if layer.role == "system")
        context_layers = tuple(layer for layer in self.layers if layer.role != "system")
        return PromptSnapshot(
            version=PROMPT_VERSION,
            mode=mode.value,
            generated_at=datetime.now(timezone.utc).isoformat(),
            layers=(*system_layers, *tuple(additional_layers), *context_layers),
        )


@dataclass(frozen=True, slots=True)
class PromptLayer:
    """One named model-facing prompt layer.

    ``sensitive`` is enforced by serialization, not merely by the desktop UI. This
    keeps Memory and compacted conversation summaries out of a default snapshot.
    """

    name: str
    role: str
    content: str
    sensitive: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "char_count": len(self.content),
            "estimated_tokens": _estimate_layer_tokens(self.role, self.content),
            "sha256": _sha256(self.content),
            "sensitive": self.sensitive,
        }


@dataclass(frozen=True, slots=True)
class PromptSnapshot:
    """Versioned Prompt observability data shared by Trace and the desktop UI."""

    version: str
    mode: str
    generated_at: str
    layers: tuple[PromptLayer, ...]

    def trace_metadata(self) -> dict[str, Any]:
        """Return content-free metrics that are safe to add to every Trace event."""

        system_prompt = "\n\n".join(
            layer.content for layer in self.layers if layer.role == "system"
        )
        messages = ([{"role": "system", "content": system_prompt}] if system_prompt else [])
        messages.extend(
            {"role": layer.role, "content": layer.content}
            for layer in self.layers
            if layer.role != "system"
        )
        canonical_prompt = json.dumps(
            messages,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return {
            "version": self.version,
            "mode": self.mode,
            "generated_at": self.generated_at,
            "total": {
                "char_count": sum(len(str(message["content"])) for message in messages),
                "estimated_tokens": estimate_request_tokens(messages),
                "sha256": _sha256(canonical_prompt),
            },
            "system": {
                "char_count": len(system_prompt),
                "estimated_tokens": _estimate_layer_tokens("system", system_prompt),
                "sha256": _sha256(system_prompt),
            },
            "layers": [layer.metadata() for layer in self.layers],
        }

    def to_dict(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        """Serialize the snapshot, redacting sensitive layer content by default."""

        metadata = self.trace_metadata()
        serialized_layers: list[dict[str, Any]] = []
        preview_sections: list[str] = []
        for layer, layer_metadata in zip(self.layers, metadata["layers"]):
            hidden = layer.sensitive and not include_sensitive
            content = "[REDACTED]" if hidden else layer.content
            serialized_layers.append(
                {
                    **layer_metadata,
                    "content_hidden": hidden,
                    "content": content,
                }
            )
            preview_sections.append(
                f"[{layer.role.upper()} · {layer.name}]\n{content}"
            )
        return {
            "available": True,
            **metadata,
            "memory_hidden": any(
                layer.sensitive and not include_sensitive for layer in self.layers
            ),
            "layers": serialized_layers,
            "assembled_preview": "\n\n".join(preview_sections),
        }


class PromptAssembler:
    """Compose system-prompt layers in one deterministic order."""

    _NO_TOOLS_SECTION = """## Tool availability

No tools are available in this prompt mode. Do not claim to have read files, executed
commands, changed the workspace, browsed the web, or called MCP tools. Return only the
structured planning or review response required by the role prompt."""

    def assemble(self, mode: PromptMode, context: PromptContext) -> PromptAssembly:
        if not isinstance(mode, PromptMode):
            mode = PromptMode(mode)
        base_prompt = context.base_prompt.strip()
        if not base_prompt:
            raise ValueError("base_prompt must not be empty")

        layers = [
            PromptLayer("core_policy", "system", CORE_POLICY),
            PromptLayer("role_policy", "system", base_prompt),
        ]
        if mode.tools_enabled:
            layers.append(PromptLayer("engineering_policy", "system", ENGINEERING_POLICY))
            capability_policy = "\n\n".join(capability_sections(context.available_tools))
            if capability_policy:
                layers.append(
                    PromptLayer("capability_policy", "system", capability_policy)
                )
            if "search_code" in context.available_tools and context.rag_auto_retrieval is not None:
                layers.append(
                    PromptLayer(
                        "rag_policy",
                        "system",
                        rag_mode_section(context.rag_auto_retrieval),
                    )
                )
        else:
            layers.append(
                PromptLayer("tool_availability", "system", self._NO_TOOLS_SECTION)
            )
        if context.runtime_context.strip():
            layers.append(
                PromptLayer("runtime_context", "system", context.runtime_context.strip())
            )
        if context.skill_index.strip():
            layers.append(PromptLayer("skill_index", "system", context.skill_index.strip()))
        context_messages: list[dict[str, object]] = []
        memory_message = untrusted_context_message(
            ContextKind.RETRIEVED_MEMORY,
            context.memory_context,
        )
        if memory_message is not None:
            context_messages.append(memory_message)
            layers.append(
                PromptLayer(
                    "retrieved_memory",
                    "user",
                    str(memory_message["content"]),
                    sensitive=True,
                )
            )
        return PromptAssembly(
            system_prompt="\n\n".join(
                layer.content for layer in layers if layer.role == "system"
            ),
            context_messages=tuple(context_messages),
            layers=tuple(layers),
        )


def publish_prompt_snapshot(client: object, snapshot: PromptSnapshot) -> None:
    """Publish a snapshot when the active client supports Prompt observability."""

    callback = getattr(client, "record_prompt_snapshot", None)
    if callable(callback):
        callback(snapshot)


def _estimate_layer_tokens(role: str, content: str) -> int:
    if not content:
        return 0
    return estimate_request_tokens([{"role": role, "content": content}])


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def runtime_context(now: datetime | None = None) -> str:
    """Return the shared date context used by every prompt mode."""

    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    timezone_name = local_now.tzname() or str(local_now.utcoffset() or "local")
    operating_system = platform.system() or "unknown"
    command_shell = "PowerShell" if operating_system == "Windows" else "POSIX shell"
    return (
        "Runtime context:\n"
        f"- Current local date: {local_now.date().isoformat()}\n"
        f"- Current time zone: {timezone_name}\n"
        f"- Operating system: {operating_system}\n"
        f"- Command shell for string commands: {command_shell}\n"
        "- Treat words such as today, latest, current, and recently relative to this date."
    )
