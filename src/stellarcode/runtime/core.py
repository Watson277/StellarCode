"""Project Runtime composition root.

``RuntimeSession`` owns project-scoped services (MCP, RAG, long-term memory and
workspace protection) and creates isolated conversation runtimes.  It is intentionally
the boundary between durable project state and short-lived Agent/Plan/Team instances.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from stellarcode.agent import Agent
from stellarcode.cancellation import TaskCancelledError
from stellarcode.browser import (
    BrowserController,
    BrowserGuard,
    BrowserSession,
    register_browser_tools,
)
from stellarcode.diagnostics import DiagnosticsService, LspConfig
from stellarcode.hitl import ACCESS_MODES
from stellarcode.llm import TokenUsage, UsageLedger, create_chat_client
from stellarcode.llm.types import current_llm_scope, llm_runtime_scope
from stellarcode.llm.message_history import repair_tool_message_history
from stellarcode.memory import LongTermMemory, MemoryManager, ProjectMemoryService
from stellarcode.mcp import (
    McpConfigError,
    McpConfigLoader,
    McpServerConfig,
    McpServerManager,
)
from stellarcode.multi_agent import AgentOrchestrator
from stellarcode.plan import ExecutionPlan, PlanExecuteAgent, Task, TaskStatus, TaskType
from stellarcode.path_utils import subprocess_safe_path
from stellarcode.protection import WorkspaceProtectionService
from stellarcode.rag import EmbeddingClient, RagService, RagSourceStore
from stellarcode.runtime.hitl import RuntimeHitlHandler, task_approval_scope
from stellarcode.runtime.attachments import PreparedAttachments, prepare_attachments
from stellarcode.runtime.recovery import EventJournal, TaskCheckpointStore
from stellarcode.skill import (
    BundledSkillManager,
    SkillContextBuffer,
    SkillRegistry,
    SkillStateStore,
    explicit_skill_context,
    register_skill_tools,
)
from stellarcode.task_workspace import task_workspace_scope
from stellarcode.tools import build_default_registry
from stellarcode.trace import ScopedTraceRecorder, TracingChatClient


def _load_runtime_environment(workspace: Path) -> None:
    """Load user and project `.env` files without relying on process CWD.

    A packaged desktop build passes an app-data `.env` explicitly. Development
    keeps using the repository `.env`; the process environment is never
    overridden in either case.
    """

    configured = os.getenv("STELLARCODE_ENV_FILE", "").strip()
    if configured:
        load_dotenv(Path(configured), override=False)
    load_dotenv(workspace / ".env", override=False)


@dataclass(frozen=True)
class RuntimeSettings:
    workspace: Path
    project_id: str = "unregistered"
    data_dir: Path | None = None
    worktree_dir: Path | None = None
    mode: str = "react"
    access_mode: str = "restricted"
    max_iterations: int = 8
    max_parallel_tools: int = 4
    tool_batch_timeout: float = 90
    plan_workers: int = 4
    team_workers: int = 2
    team_retries: int = 2
    context_window: int = 200_000
    rag_auto_retrieval: bool = True
    diagnostics_lsp_enabled: bool = False
    diagnostics_lsp_command: str = ""
    diagnostics_lsp_args: tuple[str, ...] = ()
    diagnostics_lsp_timeout: float = 20.0


@dataclass
class ConversationRuntime:
    id: str
    title: str
    mode: str
    access_mode: str
    created_at: str
    updated_at: str
    title_is_custom: bool
    trace_enabled: bool
    trace_path: str | None
    transcript: list[dict[str, Any]]
    event_floor_sequence: int
    memory_manager: MemoryManager
    agent: Agent
    plan_agent: PlanExecuteAgent
    team_agent: AgentOrchestrator
    usage_ledger: UsageLedger
    skill_context_buffer: SkillContextBuffer

    def metadata(self) -> dict[str, Any]:
        history = self.agent.history_snapshot()
        return {
            "id": self.id,
            "title": self.title,
            "mode": self.mode,
            "access_mode": self.access_mode,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.transcript),
            "trace_enabled": self.trace_enabled,
            "trace_path": self.trace_path,
            "event_floor_sequence": self.event_floor_sequence,
            "usage": self.usage_ledger.snapshot(),
            "history": history,
        }


class RuntimeSession:
    """One workspace Runtime with shared tools and isolated conversation contexts."""

    def __init__(
        self,
        settings: RuntimeSettings,
        emit: Any,
        event_journal: EventJournal | None = None,
        user_memory: LongTermMemory | None = None,
    ) -> None:
        self.settings = settings
        self.workspace = subprocess_safe_path(settings.workspace)
        _load_runtime_environment(self.workspace)
        self.project_id = settings.project_id
        self.access_mode = settings.access_mode
        self._emit = emit
        self._active_conversation_id: str | None = None
        self._task_cancel_events: dict[str, threading.Event] = {}
        self._task_conversations: dict[str, str] = {}
        self._conversation_tasks: dict[str, str] = {}
        self._task_lock = threading.RLock()
        self._persistence_lock = threading.RLock()
        self._trace_conversation_id: str | None = None
        root_data_dir = settings.data_dir or (Path.home() / ".stellarcode" / "desktop-runtime")
        self.project_data_dir = root_data_dir.resolve() / "projects" / self.project_id
        self.conversation_dir = self.project_data_dir / "conversations"
        self.conversation_dir.mkdir(parents=True, exist_ok=True)
        self.user_memory = user_memory or LongTermMemory(
            root_data_dir.resolve() / "memory" / "user"
        )
        self.project_memory = ProjectMemoryService(
            self.project_data_dir / "memory",
            context_window=settings.context_window,
            user_long_term=self.user_memory,
        )
        self.diagnostics_service = DiagnosticsService(
            self.workspace,
            self.project_data_dir / "diagnostics",
            lsp_config=LspConfig(
                enabled=settings.diagnostics_lsp_enabled,
                command=settings.diagnostics_lsp_command,
                args=settings.diagnostics_lsp_args,
                timeout_seconds=settings.diagnostics_lsp_timeout,
            ),
        )
        self.workspace_protection = WorkspaceProtectionService(
            self.workspace,
            self.project_data_dir / "workspace-protection",
            worktree_root=(
                settings.worktree_dir.resolve() / "projects" / self.project_id / "w"
                if settings.worktree_dir is not None
                else None
            ),
        )
        self.event_journal = event_journal or EventJournal(self.project_data_dir / "events.jsonl")
        self.task_checkpoints = TaskCheckpointStore(
            self.project_data_dir / "recovery" / "active-task.json"
        )

        trace_dir = self.workspace / ".stellarcode" / "traces"
        self.trace_recorder = ScopedTraceRecorder(trace_dir)
        self.rag_service = RagService(self.workspace, embedding_client=EmbeddingClient())
        self.rag_source_store = RagSourceStore(self.project_data_dir / "rag" / "sources.json")
        self.rag_service.set_readiness_check(self._rag_unavailable_reason)
        self.browser_session = BrowserSession()
        self.browser_guard = BrowserGuard(self.browser_session)
        self.hitl_handler = RuntimeHitlHandler(
            lambda event_type, data: self._emit_event(event_type, data),
            access_mode=settings.access_mode,
            is_task_cancelled=self._is_task_cancelled,
        )
        self.registry = build_default_registry(
            self.workspace,
            rag_service=self.rag_service,
            hitl_handler=self.hitl_handler,
            max_parallel_tools=settings.max_parallel_tools,
            tool_batch_timeout_seconds=settings.tool_batch_timeout,
            trace_recorder=self.trace_recorder,
        )
        state_path = Path.home() / ".stellarcode" / "skills.json"
        user_skills_dir = Path.home() / ".stellarcode" / "skills"
        self.skill_state_store = SkillStateStore(state_path)
        self.skill_upgrade_manager = BundledSkillManager(
            user_skills_dir,
            self.skill_state_store,
        )
        skill_bootstrap_warnings = self.skill_upgrade_manager.bootstrap()
        self.skill_registry = SkillRegistry(
            user_dir=user_skills_dir,
            project_dir=self.workspace / ".stellarcode" / "skills",
            state_store=self.skill_state_store,
            startup_warnings=skill_bootstrap_warnings,
        )
        self.skill_registry.reload()
        self.skill_context_buffer = SkillContextBuffer()
        register_skill_tools(self.registry, self.skill_registry, self.skill_context_buffer)

        config_loader = McpConfigLoader(self.workspace)
        config_loader.bootstrap_chrome_devtools()
        self.mcp_manager = McpServerManager(
            self.registry,
            self.workspace,
            config_loader=config_loader,
            browser_guard=self.browser_guard,
            status_callback=self._mcp_status_changed,
        )
        try:
            self.mcp_manager.load_configured_servers()
            self.mcp_manager.start_all(progress=self._progress)
        except McpConfigError as exc:
            self._progress(f"MCP configuration error: {exc}")

        self.browser_controller = BrowserController.create(
            self.browser_session,
            self.mcp_manager,
            self.registry,
        )
        register_browser_tools(self.registry, self.browser_controller)
        self.conversations: dict[str, ConversationRuntime] = {}
        base_client = create_chat_client()
        self.provider_name = base_client.provider_name
        self.model = base_client.model
        self.llm_client = TracingChatClient(
            base_client,
            self.trace_recorder,
            usage_callback=self._record_usage,
        )
        self.project_memory.migrate_legacy_project_memory(
            [path.stem for path in self.conversation_dir.glob("*.json")]
        )
        self._load_conversations()

    @property
    def active_conversation_id(self) -> str | None:
        return self._active_conversation_id

    def active_tasks(self) -> list[dict[str, str]]:
        self._ensure_task_state()
        with self._task_lock:
            return [
                {"task_id": task_id, "session_id": conversation_id}
                for task_id, conversation_id in self._task_conversations.items()
            ]

    def active_task_for_conversation(self, conversation_id: str) -> str | None:
        self._ensure_task_state()
        with self._task_lock:
            return self._conversation_tasks.get(conversation_id)

    def list_conversations(self) -> list[dict[str, Any]]:
        values = [conversation.metadata() for conversation in self.conversations.values()]
        return sorted(values, key=lambda item: item["updated_at"], reverse=True)

    def mcp_snapshot(self) -> dict[str, Any]:
        return self.mcp_manager.snapshot()

    def memory_snapshot(
        self,
        conversation_id: str,
        *,
        scope: str = "user",
        query: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        return self.project_memory.snapshot(
            conversation_id,
            scope=scope,
            query=query,
            limit=limit,
        )

    def prompt_snapshot(
        self,
        conversation_id: str,
        *,
        include_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Return the latest actual Prompt, or a mode-correct preflight preview."""

        conversation = self._get_conversation(conversation_id)
        latest = self.llm_client.prompt_snapshot(
            conversation_id,
            include_sensitive=include_sensitive,
        )
        compatible_modes = {
            "react": {"react"},
            "plan": {"plan-builder", "plan-executor"},
            "team": {"team-planner", "team-worker", "team-reviewer"},
        }
        if latest is not None and latest.get("mode") in compatible_modes[conversation.mode]:
            snapshot = latest
        elif conversation.mode == "plan":
            snapshot = conversation.plan_agent.planner.prompt_snapshot(
                include_sensitive=include_sensitive
            )
        elif conversation.mode == "team":
            snapshot = conversation.team_agent.planner.prompt_snapshot(
                include_sensitive=include_sensitive
            )
        else:
            snapshot = conversation.agent.prompt_snapshot(
                include_sensitive=include_sensitive
            )
        return {
            **snapshot,
            "session_id": conversation_id,
            "conversation_title": conversation.title,
            "requested_mode": conversation.mode,
        }

    def save_memory(
        self,
        conversation_id: str,
        content: str,
        *,
        scope: str = "user",
    ) -> dict[str, Any]:
        entry, created = self.project_memory.save(
            content,
            conversation_id=conversation_id,
            scope=scope,
        )
        return {
            **self.memory_snapshot(conversation_id, scope=scope),
            "entry": entry.to_dict(),
            "created": created,
        }

    def pending_memory_extraction_count(self, conversation_id: str) -> int:
        conversation = self._get_conversation(conversation_id)
        return conversation.memory_manager.pending_user_message_count()

    def extract_conversation_memory(self, conversation_id: str) -> dict[str, Any]:
        """Explicitly extract durable memory without waiting for compression."""

        conversation = self._get_conversation(conversation_id)
        # Attribute model usage to the correct conversation even if the user switches
        # projects while this background management job is running.
        with llm_runtime_scope(conversation_id, ""):
            result = conversation.memory_manager.extract_current_user_memories()
        conversation.updated_at = _timestamp()
        self._save_conversation(conversation)
        return result

    def delete_memory(
        self,
        conversation_id: str,
        entry_id: str,
        *,
        scope: str = "user",
    ) -> dict[str, Any]:
        if not self.project_memory.delete(
            entry_id,
            conversation_id=conversation_id,
            scope=scope,
        ):
            raise ValueError(f"Memory entry not found: {entry_id}")
        return {
            **self.memory_snapshot(conversation_id, scope=scope),
            "deleted_id": entry_id,
        }

    def clear_memory(
        self,
        conversation_id: str,
        *,
        scope: str = "user",
    ) -> dict[str, Any]:
        self.project_memory.clear(conversation_id=conversation_id, scope=scope)
        return self.memory_snapshot(conversation_id, scope=scope)

    def skill_snapshot(self) -> dict[str, Any]:
        disabled = self.skill_state_store.disabled()
        bundled_statuses = self.skill_upgrade_manager.statuses()
        skills = [
            self._skill_data(
                skill,
                enabled=skill.name not in disabled,
                bundled_status=(
                    bundled_statuses.get(skill.name)
                    if self._is_active_bundled_user_skill(
                        skill,
                        frozenset(bundled_statuses),
                    )
                    else None
                ),
            )
            for skill in self.skill_registry.all_skills()
        ]
        warnings = list(
            dict.fromkeys(
                (
                    *self.skill_registry.warnings(),
                    *self.skill_state_store.warnings(),
                    *self.skill_upgrade_manager.warnings(),
                )
            )
        )
        return {
            "skills": skills,
            "enabled_count": sum(1 for skill in skills if skill["enabled"]),
            "total_count": len(skills),
            "bundled_count": sum(1 for skill in skills if skill["builtin"]),
            "updates_available": sum(
                1 for skill in skills if skill["update_available"]
            ),
            "warnings": warnings,
            "user_dir": str(self.skill_registry.user_dir or ""),
            "project_dir": str(self.skill_registry.project_dir or ""),
            "state_path": str(self.skill_state_store.file),
        }

    def skill_detail(self, name: str) -> dict[str, Any]:
        skill = self.skill_registry.find_any_skill(name)
        if skill is None:
            raise ValueError(f"Skill not found: {name}")
        bundled_statuses = self.skill_upgrade_manager.statuses()
        return {
            **self._skill_data(
                skill,
                enabled=skill.name not in self.skill_state_store.disabled(),
                bundled_status=(
                    bundled_statuses.get(skill.name)
                    if self._is_active_bundled_user_skill(
                        skill,
                        frozenset(bundled_statuses),
                    )
                    else None
                ),
            ),
            "body": skill.body,
        }

    def set_skill_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        if self.skill_registry.find_any_skill(name) is None:
            raise ValueError(f"Skill not found: {name}")
        updated = (
            self.skill_state_store.enable(name) if enabled else self.skill_state_store.disable(name)
        )
        if not updated:
            warnings = self.skill_state_store.warnings()
            detail = warnings[-1] if warnings else "could not persist Skill state"
            raise RuntimeError(detail)
        return self.skill_snapshot()

    def reload_skills(self) -> dict[str, Any]:
        if self.skill_registry.user_dir is not None:
            self.skill_upgrade_manager.bootstrap()
        self.skill_registry.reload()
        return self.skill_snapshot()

    def skill_diff(self, name: str, max_chars: int = 120_000) -> dict[str, Any]:
        if not self._is_active_bundled_name(name):
            raise ValueError(f"Active Skill is not an editable bundled Skill: {name}")
        return self.skill_upgrade_manager.diff(name, max_chars=max_chars)

    def update_bundled_skill(
        self,
        name: str,
        *,
        action: str,
        expected_current_hash: str,
        expected_builtin_hash: str,
    ) -> dict[str, Any]:
        if not self._is_active_bundled_name(name):
            raise ValueError(f"Active Skill is not an editable bundled Skill: {name}")
        if action == "update":
            self.skill_upgrade_manager.update(
                name,
                expected_current_hash=expected_current_hash,
                expected_builtin_hash=expected_builtin_hash,
            )
        elif action == "keep_custom":
            self.skill_upgrade_manager.keep_custom(
                name,
                expected_current_hash=expected_current_hash,
                expected_builtin_hash=expected_builtin_hash,
            )
        elif action == "restore_default":
            self.skill_upgrade_manager.restore_default(
                name,
                expected_current_hash=expected_current_hash,
                expected_builtin_hash=expected_builtin_hash,
            )
        else:
            raise ValueError(f"unsupported bundled Skill action: {action}")
        self.skill_registry.reload()
        return self.skill_snapshot()

    def _is_active_bundled_name(self, name: str) -> bool:
        skill = self.skill_registry.find_any_skill(name)
        return skill is not None and self._is_active_bundled_user_skill(skill)

    def _is_active_bundled_user_skill(
        self,
        skill: Any,
        bundled_names: frozenset[str] | None = None,
    ) -> bool:
        user_dir = self.skill_registry.user_dir
        names = (
            bundled_names
            if bundled_names is not None
            else frozenset(self.skill_upgrade_manager.statuses())
        )
        return bool(
            user_dir is not None
            and skill.source.value == "user"
            and skill.skill_md_path.parent == user_dir / skill.name
            and skill.name in names
        )

    @staticmethod
    def _skill_data(
        skill: Any,
        *,
        enabled: bool,
        bundled_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        upgrade = bundled_status or {
            "builtin": False,
            "builtin_version": "",
            "builtin_hash": "",
            "installed_version": "",
            "installed_hash": "",
            "current_version": skill.version or "",
            "current_hash": "",
            "customized": False,
            "update_available": False,
            "update_acknowledged": False,
            "upgrade_state": "not_bundled",
            "error": "",
        }
        return {
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
            "author": skill.author,
            "tags": list(skill.tags),
            "source": skill.source.value,
            "enabled": enabled,
            "skill_md_path": str(skill.skill_md_path),
            "references_path": (
                str(skill.references_dir) if skill.references_dir is not None else None
            ),
            **upgrade,
        }

    def browser_snapshot(self) -> dict[str, Any]:
        server = self.mcp_manager.server("chrome-devtools")
        server_data = self.mcp_manager.server_snapshot(server) if server is not None else None
        return {
            "mode": self.browser_session.mode.value,
            "browser_url": self.browser_session.browser_url,
            "last_navigated_url": self.browser_session.last_navigated_url,
            "agent_opened_pages": list(self.browser_session.agent_opened_pages),
            "chrome_server": {
                "status": (
                    str(server_data["status"]) if server_data is not None else "not_configured"
                ),
                "error": str(server_data["error"]) if server_data is not None else "",
                "tool_count": (int(server_data["tool_count"]) if server_data is not None else 0),
            },
        }

    def probe_browser(self, port: int) -> dict[str, Any]:
        probe = self.browser_controller.connectivity.probe(port)
        return {"port": port, **asdict(probe)}

    def connect_browser(self, port: int | None = None) -> dict[str, Any]:
        message = self.browser_controller.connect(port)
        if self.browser_session.mode.value != "shared":
            raise RuntimeError(message)
        return {"message": message, "snapshot": self.browser_snapshot()}

    def disconnect_browser(self) -> dict[str, Any]:
        message = self.browser_controller.disconnect()
        if self.browser_session.mode.value != "isolated":
            raise RuntimeError(message)
        return {"message": message, "snapshot": self.browser_snapshot()}

    def browser_tabs(self) -> dict[str, Any]:
        return {"output": self.browser_controller.tabs()}

    def diagnostics_snapshot(self) -> dict[str, Any]:
        return _desktop_diagnostics_snapshot(self.diagnostics_service.snapshot())

    def run_diagnostics(
        self,
        profile: str,
        progress_callback: Any = None,
        cancel_event: threading.Event | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        service_profile = {"safe": "auto", "build": "build"}.get(profile, profile)
        result = self.diagnostics_service.run(
            service_profile,
            progress=progress_callback,
            cancel_event=cancel_event,
            run_id=run_id,
        )
        return _desktop_diagnostics_snapshot(result)

    def rag_snapshot(self) -> dict[str, Any]:
        metadata = self.rag_source_store.snapshot()
        stats = self.rag_service.stats()
        provider = self.rag_service.embedding_client.provider
        model = self.rag_service.embedding_client.model
        indexed_provider = str(metadata.get("embedding_provider") or "")
        indexed_model = str(metadata.get("embedding_model") or "")
        sources = list(metadata.get("sources") or [])
        return {
            "workspace": str(self.workspace),
            "sources": sources,
            "source_count": len(sources),
            "indexed_file_count": stats.file_count,
            "chunk_count": stats.chunk_count,
            "relation_count": stats.relation_count,
            "last_indexed_at": metadata.get("last_indexed_at"),
            "last_result": metadata.get("last_result"),
            "embedding_provider": provider,
            "embedding_model": model,
            "embedding_base_url": self.rag_service.embedding_client.base_url,
            "embedding_api_key_configured": bool(self.rag_service.embedding_client.api_key),
            "needs_rebuild": bool(sources)
            and (
                stats.chunk_count == 0
                or not metadata.get("last_indexed_at")
                or indexed_provider != provider
                or indexed_model != model
            ),
            "storage_path": str(
                Path(
                    self.rag_service.storage_dir
                    or os.getenv("STELLARCODE_RAG_DIR")
                    or Path.home() / ".stellarcode" / "rag"
                ).resolve()
                / "codebase.db"
            ),
        }

    def add_rag_sources(self, paths: list[str | Path]) -> dict[str, Any]:
        self.rag_source_store.add(paths)
        return self.rag_snapshot()

    def remove_rag_source(self, path: str | Path) -> dict[str, Any]:
        sources = self.rag_source_store.remove(path)
        if not sources:
            self.rag_service.clear()
        return self.rag_snapshot()

    def rebuild_rag_index(self, progress_callback: Any = None) -> dict[str, Any]:
        sources = [item["path"] for item in self.rag_source_store.snapshot()["sources"]]
        if not sources:
            raise ValueError("add at least one file or folder before building the RAG index")
        result = self.rag_service.index_sources(sources, progress_callback=progress_callback)
        # Persist failed attempts too.  Otherwise a Settings refresh turns an
        # actionable indexing failure into an indistinguishable empty index.
        result_data = asdict(result)
        self.rag_source_store.record_index(
            result_data,
            embedding_provider=self.rag_service.embedding_client.provider,
            embedding_model=self.rag_service.embedding_client.model,
        )
        if result.chunk_count == 0 and result.error_count > 0:
            raise RuntimeError(result.message)
        return self.rag_snapshot()

    def clear_rag_index(self) -> dict[str, Any]:
        self.rag_service.clear()
        self.rag_source_store.clear_index_metadata()
        return self.rag_snapshot()

    def _rag_unavailable_reason(self) -> str | None:
        snapshot = self.rag_snapshot()
        if snapshot["needs_rebuild"]:
            return (
                "The desktop RAG source list or Embedding model changed. Rebuild the index "
                "in Settings > Code RAG before using search_code."
            )
        return None

    def install_mcp_server(
        self,
        name: str,
        config: McpServerConfig,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        self.mcp_manager.install(name, config, overwrite=overwrite)
        return self.mcp_snapshot()

    def set_mcp_server_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        self.mcp_manager.set_enabled(name, enabled)
        return self.mcp_snapshot()

    def restart_mcp_server(self, name: str) -> dict[str, Any]:
        if self.mcp_manager.server(name) is None:
            raise ValueError(f"MCP server not found: {name}")
        self.mcp_manager.restart(name)
        return self.mcp_snapshot()

    def remove_mcp_server(self, name: str) -> dict[str, Any]:
        self.mcp_manager.remove_project_server(name)
        return self.mcp_snapshot()

    def mcp_server_logs(self, name: str) -> dict[str, Any]:
        if self.mcp_manager.server(name) is None:
            raise ValueError(f"MCP server not found: {name}")
        return {"name": name, "logs": self.mcp_manager.logs(name)}

    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        title: str = "New conversation",
        mode: str | None = None,
    ) -> dict[str, Any]:
        active_mode = mode or self.settings.mode
        self._validate_mode(active_mode)
        identifier = conversation_id or f"session-{uuid.uuid4().hex}"
        if identifier in self.conversations:
            raise ValueError(f"conversation already exists: {identifier}")
        self.project_memory.migrate_legacy_project_memory([identifier])
        now = _timestamp()
        conversation = self._new_conversation(
            identifier,
            title=title.strip() or "New conversation",
            mode=active_mode,
            created_at=now,
            updated_at=now,
            title_is_custom=title.strip() not in {"", "New conversation"},
            trace_enabled=False,
            trace_path=None,
            transcript=[],
            access_mode=self.access_mode,
        )
        self.conversations[identifier] = conversation
        self._active_conversation_id = identifier
        self._sync_trace_recorder(conversation)
        self._save_conversation(conversation)
        return self.snapshot(identifier)

    def open_conversation(self, conversation_id: str) -> dict[str, Any]:
        conversation = self._get_conversation(conversation_id)
        self._active_conversation_id = conversation_id
        self._sync_trace_recorder(conversation)
        skill_buffer = getattr(
            conversation,
            "skill_context_buffer",
            getattr(self, "skill_context_buffer", None),
        )
        if skill_buffer is not None:
            skill_buffer.clear()
        self.hitl_handler.clear_approved_all()
        return self.snapshot(conversation_id)

    def snapshot(self, conversation_id: str) -> dict[str, Any]:
        conversation = self._get_conversation(conversation_id)
        return {
            **conversation.metadata(),
            "project_id": self.project_id,
            "workspace": str(self.workspace),
            "transcript": conversation.transcript,
        }

    def rename_conversation(self, conversation_id: str, title: str) -> dict[str, Any]:
        value = title.strip()
        if not value:
            raise ValueError("conversation title must not be empty")
        conversation = self._get_conversation(conversation_id)
        conversation.title = value[:80]
        conversation.title_is_custom = True
        conversation.updated_at = _timestamp()
        self._save_conversation(conversation)
        return conversation.metadata()

    def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        conversation = self._get_conversation(conversation_id)
        if self.active_task_for_conversation(conversation_id):
            raise RuntimeError("cannot delete a conversation while its task is running")
        self.conversations.pop(conversation_id)
        path = self._conversation_path(conversation_id)
        if path.exists():
            path.unlink()
        if self._active_conversation_id == conversation_id:
            if self._trace_conversation_id == conversation_id:
                self.trace_recorder.disable()
                self._trace_conversation_id = None
            self._active_conversation_id = None
        return conversation.metadata()

    def run(
        self,
        prompt: str,
        task_id: str,
        conversation_id: str,
        attachments: object = None,
        prepared: PreparedAttachments | None = None,
    ) -> str:
        conversation = self._get_conversation(conversation_id)
        if prepared is None:
            prepared = self.prepare_task(
                task_id,
                conversation_id,
                prompt,
                attachments,
            )
        with self._task_lock:
            if self._task_conversations.get(task_id) != conversation_id:
                raise RuntimeError("task was not prepared for this conversation")
            cancellation_event = self._task_cancel_events.get(task_id)
            if cancellation_event is None:
                raise RuntimeError("task cancellation state is unavailable")
        self.task_checkpoints.update(
            task_id,
            status="running",
            checkpoint_stage="agent_running",
            updated_at=_timestamp(),
        )
        try:
            task_workspace = self.workspace_protection.task_workspace(
                task_id,
                conversation_id,
            )
            with task_workspace_scope(self.workspace, task_workspace):
                with llm_runtime_scope(conversation_id, task_id):
                    with task_approval_scope(conversation.access_mode):
                        if conversation.mode == "plan":
                            answer = conversation.plan_agent.run(
                                prepared.agent_prompt,
                                cancellation_event,
                            )
                        elif conversation.mode == "team":
                            answer = conversation.team_agent.run(
                                prepared.agent_prompt,
                                cancellation_event,
                            )
                        else:
                            answer = conversation.agent.run(
                                prepared.agent_prompt,
                                cancellation_event,
                            )
            self.task_checkpoints.update(
                task_id,
                status="answer_ready",
                checkpoint_stage="answer_ready",
                answer=answer,
                updated_at=_timestamp(),
            )
            conversation.transcript.append(
                _transcript_entry("assistant", answer, task_id=task_id)
            )
            conversation.updated_at = _timestamp()
            self._save_conversation(conversation)
            return answer
        except TaskCancelledError:
            cancellation_message = "Task cancelled by user."
            if conversation.mode == "react":
                repaired, _ = repair_tool_message_history(conversation.agent.messages)
                conversation.agent.messages = repaired
                conversation.agent.messages.append(
                    {"role": "assistant", "content": cancellation_message}
                )
            conversation.memory_manager.add_assistant_message(cancellation_message)
            conversation.transcript.append(
                _transcript_entry("assistant", cancellation_message, task_id=task_id)
            )
            conversation.updated_at = _timestamp()
            self._save_conversation(conversation)
            # Persist terminal intent inside Runtime before control returns to
            # the Sidecar. A hard crash between this raise and Sidecar handling
            # must finalize the task, never resume Agent/tool execution.
            self.mark_task_finalize_pending(
                task_id,
                "cancelled",
                {"status": "cancelled", "reason": "user"},
            )
            raise
        except Exception as exc:
            self.mark_task_finalize_pending(
                task_id,
                "failed",
                {
                    "status": "failed",
                    "error_code": "task_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                    "recoverable": True,
                },
            )
            raise
        finally:
            self._release_task(task_id)

    def prepare_task(
        self,
        task_id: str,
        conversation_id: str,
        prompt: str,
        attachments: object = None,
    ) -> PreparedAttachments:
        """Durably accept a task before acknowledging task.submit."""

        # Snapshot/isolate before recording the user turn or acknowledging the
        # request.  This gives every accepted task one immutable merge baseline,
        # including tasks that fail before their first tool call.
        conversation = self._get_conversation(conversation_id)
        prepared = prepare_attachments(prompt, attachments)
        self._register_task(task_id, conversation_id)
        try:
            protection = self.workspace_protection.begin_task(
                task_id,
                conversation_id,
                isolated=True,
            )
            if not protection.get("protected"):
                raise RuntimeError(
                    "Workspace modification protection could not create a task snapshot: "
                    f"{protection.get('error') or 'unknown Git snapshot error'}"
                )
            self._emit_event("workspace.snapshot.created", protection)
            # The model must see the isolated worktree path, while user-facing
            # project metadata continues to name the canonical workspace.
            workspace_context = _task_workspace_prompt_context(
                self.workspace,
                protection.get("worktree_path"),
            )
            prepared = PreparedAttachments(
                agent_prompt=f"{workspace_context}\n\n{prepared.agent_prompt}",
                metadata=prepared.metadata,
            )
            conversation.skill_context_buffer.clear()
            # ``@skill-*`` and ``@mcp-*`` are explicit per-turn instructions.
            # They are deliberately prepended to the user request instead of
            # permanently expanding the system prompt for later turns.
            reference_context = _explicit_reference_context(
                prompt,
                self.skill_registry,
            )
            if reference_context:
                prepared = PreparedAttachments(
                    agent_prompt=(
                        f"{reference_context}\n\n---\nOriginal user request:\n"
                        f"{prepared.agent_prompt}"
                    ),
                    metadata=prepared.metadata,
                )
            if not conversation.title_is_custom and not conversation.transcript:
                conversation.title = _automatic_title(prompt)
            conversation.transcript.append(
                _transcript_entry("user", prompt, attachments=prepared.metadata)
            )
            conversation.updated_at = _timestamp()
            self._save_conversation(conversation)
            now = _timestamp()
            self.task_checkpoints.write(
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "session_id": conversation_id,
                    "project_id": self.project_id,
                    "mode": conversation.mode,
                    "prompt": prompt,
                    "agent_prompt": prepared.agent_prompt,
                    "attachments": prepared.metadata,
                    "status": "prepared",
                    "checkpoint_stage": "accepted",
                    "recovery_attempts": 0,
                    "started_at": now,
                    "updated_at": now,
                    "workspace_snapshot": protection,
                }
            )
            return prepared
        except Exception:
            try:
                self.workspace_protection.finalize_task(task_id, "rejected")
            except Exception:
                pass
            self._release_task(task_id)
            raise

    def pending_recovery(self) -> dict[str, Any] | None:
        recoveries = self.pending_recoveries()
        return recoveries[0] if recoveries else None

    def pending_recoveries(self) -> list[dict[str, Any]]:
        recoveries: list[dict[str, Any]] = []
        for checkpoint in self.task_checkpoints.load_all():
            recovery = self._checkpoint_recovery(checkpoint)
            if recovery is not None:
                recoveries.append(recovery)
        return sorted(recoveries, key=lambda item: str(item.get("started_at") or ""))

    def _checkpoint_recovery(self, checkpoint: dict[str, Any]) -> dict[str, Any] | None:
        session_id = str(checkpoint.get("session_id") or "")
        task_id = str(checkpoint.get("task_id") or "")
        if not session_id or not task_id or session_id not in self.conversations:
            return None
        if checkpoint.get("status") in {"completed", "failed", "cancelled"}:
            return None
        return {
            "task_id": task_id,
            "session_id": session_id,
            "mode": str(checkpoint.get("mode") or "react"),
            "status": str(checkpoint.get("status") or "prepared"),
            "checkpoint_stage": str(checkpoint.get("checkpoint_stage") or "accepted"),
            "recovery_attempts": int(checkpoint.get("recovery_attempts") or 0),
            "started_at": str(checkpoint.get("started_at") or _timestamp()),
            "prompt_preview": str(checkpoint.get("prompt") or "")[:240],
            "terminal_outcome": str(checkpoint.get("terminal_outcome") or ""),
        }

    def prepare_recovery(self, task_id: str, conversation_id: str) -> dict[str, Any]:
        checkpoint = self.task_checkpoints.load(task_id)
        if checkpoint is None:
            raise RuntimeError("there is no unfinished task to recover")
        if str(checkpoint.get("task_id") or "") != task_id:
            raise RuntimeError("the recovery task no longer matches")
        if str(checkpoint.get("session_id") or "") != conversation_id:
            raise RuntimeError("the recovery session no longer matches")
        if str(checkpoint.get("status") or "") == "finalize_pending":
            raise RuntimeError(
                "the task answer is complete and only its final workspace snapshot may be retried"
            )
        checkpoint_snapshot = checkpoint.get("workspace_snapshot")
        snapshot_id = (
            str(checkpoint_snapshot.get("snapshot_id") or "")
            if isinstance(checkpoint_snapshot, dict)
            else ""
        )
        try:
            self.workspace_protection.validate_recovery_baseline(
                task_id,
                conversation_id,
                snapshot_id,
            )
        except Exception as exc:
            raise RuntimeError(
                "The interrupted task cannot resume safely because its workspace "
                f"baseline is unavailable: {exc}"
            ) from exc
        self._register_task(task_id, conversation_id)
        attempts = int(checkpoint.get("recovery_attempts") or 0) + 1
        previous_status = str(checkpoint.get("status") or "prepared")
        checkpoint.update(
            status="answer_ready" if previous_status == "answer_ready" else "recovering",
            resume_from_status=previous_status,
            checkpoint_stage="runtime_restarted",
            recovery_attempts=attempts,
            updated_at=_timestamp(),
        )
        self.task_checkpoints.write(checkpoint)
        return checkpoint

    def mark_task_finalize_pending(
        self,
        task_id: str,
        outcome: str,
        terminal_data: dict[str, Any],
        error: Exception | None = None,
    ) -> None:
        """Durably record terminal intent before any POST snapshot work.

        This is the first phase of task finalization. Once written, restart must
        only finish the workspace snapshot/event commit and must never run the
        Agent or tools again.
        """

        updated = self.task_checkpoints.update(
            task_id,
            status="finalize_pending",
            checkpoint_stage="terminal_intent_recorded",
            terminal_outcome=outcome,
            terminal_data=terminal_data,
            finalize_error=(f"{type(error).__name__}: {error}" if error else ""),
            updated_at=_timestamp(),
        )
        if updated is None:
            raise RuntimeError("unfinished task checkpoint is unavailable") from error

    def finalize_pending_task(
        self,
        task_id: str,
        conversation_id: str,
    ) -> tuple[str, dict[str, Any]]:
        checkpoint = self.task_checkpoints.load(task_id)
        if checkpoint is None or str(checkpoint.get("task_id") or "") != task_id:
            raise RuntimeError("unfinished task checkpoint is unavailable")
        if str(checkpoint.get("session_id") or "") != conversation_id:
            raise RuntimeError("the recovery session no longer matches")
        if str(checkpoint.get("status") or "") != "finalize_pending":
            raise RuntimeError("the unfinished task is not waiting for workspace finalization")
        outcome = str(checkpoint.get("terminal_outcome") or "failed")
        if outcome not in {"completed", "failed", "cancelled"}:
            raise RuntimeError("the pending terminal outcome is invalid")
        terminal_data = checkpoint.get("terminal_data")
        if not isinstance(terminal_data, dict):
            terminal_data = {"status": outcome}
        if str(checkpoint.get("checkpoint_stage") or "") == "workspace_finalized" and isinstance(
            terminal_data.get("changes"),
            dict,
        ):
            return outcome, terminal_data
        changes = self.finalize_task_changes(task_id, outcome)
        if changes.get("merge_conflict"):
            outcome = "failed"
            terminal_data = {
                **terminal_data,
                "status": "failed",
                "error_code": "worktree_merge_conflict",
                "message": str(
                    changes.get("error")
                    or "The isolated task changes conflict with newer project edits."
                ),
                "recoverable": False,
            }
            self._discard_unapplied_task_answer(
                checkpoint,
                conversation_id,
                str(terminal_data["message"]),
            )
        terminal_data = {**terminal_data, "changes": changes}
        updated = self.task_checkpoints.update(
            task_id,
            status="finalize_pending",
            checkpoint_stage="workspace_finalized",
            terminal_outcome=outcome,
            terminal_data=terminal_data,
            finalize_error="",
            updated_at=_timestamp(),
        )
        if updated is None:
            raise RuntimeError("unfinished task checkpoint disappeared after finalization")
        return outcome, terminal_data

    def _discard_unapplied_task_answer(
        self,
        checkpoint: dict[str, Any],
        conversation_id: str,
        reason: str,
    ) -> None:
        """Remove a success claim when an isolated patch cannot be merged."""

        answer = str(checkpoint.get("answer") or "")
        if not answer:
            return
        conversation = self._get_conversation(conversation_id)
        for index in range(len(conversation.transcript) - 1, -1, -1):
            entry = conversation.transcript[index]
            if entry.get("role") == "user":
                break
            if entry.get("role") == "assistant" and entry.get("content") == answer:
                conversation.transcript.pop(index)
                break
        correction = (
            "Workspace finalization failed, so the generated answer and isolated file "
            f"changes were not applied to the project. Reason: {reason}"
        )
        conversation.agent.messages.append({"role": "assistant", "content": correction})
        conversation.memory_manager.add_assistant_message(correction)
        conversation.updated_at = _timestamp()
        self._save_conversation(conversation)

    def resume_task(self, task_id: str, conversation_id: str) -> str:
        self._ensure_task_state()
        conversation = self._get_conversation(conversation_id)
        checkpoint = self.task_checkpoints.load(task_id)
        if checkpoint is None or str(checkpoint.get("task_id") or "") != task_id:
            raise RuntimeError("unfinished task checkpoint is unavailable")
        with self._task_lock:
            if self._task_conversations.get(task_id) != conversation_id:
                raise RuntimeError("recovery task was not prepared")
            cancellation_event = self._task_cancel_events.get(task_id)
            if cancellation_event is None:
                raise RuntimeError("recovery cancellation state is unavailable")

        if checkpoint.get("status") == "answer_ready" and checkpoint.get("answer") is not None:
            answer = str(checkpoint.get("answer") or "")
            if not conversation.transcript or not (
                conversation.transcript[-1].get("role") == "assistant"
                and conversation.transcript[-1].get("content") == answer
            ):
                conversation.transcript.append(
                    _transcript_entry("assistant", answer, task_id=task_id)
                )
                conversation.updated_at = _timestamp()
                self._save_conversation(conversation)
            self._release_task(task_id)
            return answer

        original_prompt = str(checkpoint.get("agent_prompt") or checkpoint.get("prompt") or "")
        try:
            task_workspace = self.workspace_protection.task_workspace(
                task_id,
                conversation_id,
            )
            with task_workspace_scope(self.workspace, task_workspace):
                with llm_runtime_scope(conversation_id, task_id):
                    with task_approval_scope(conversation.access_mode):
                        if conversation.mode == "react":
                            answer = conversation.agent.resume(
                                original_prompt,
                                cancellation_event,
                            )
                        elif conversation.mode == "plan":
                            restored_plan = self._restore_execution_plan(conversation, task_id)
                            if restored_plan is not None:
                                answer = conversation.plan_agent.execute_plan(
                                    restored_plan,
                                    cancellation_event,
                                )
                            else:
                                answer = conversation.plan_agent.run(
                                    self._mode_recovery_prompt(conversation, original_prompt),
                                    cancellation_event,
                                )
                        else:
                            recovery_prompt = self._mode_recovery_prompt(
                                conversation,
                                original_prompt,
                            )
                            answer = conversation.team_agent.run(
                                recovery_prompt,
                                cancellation_event,
                            )
            self.task_checkpoints.update(
                task_id,
                status="answer_ready",
                checkpoint_stage="answer_ready",
                answer=answer,
                updated_at=_timestamp(),
            )
            conversation.transcript.append(
                _transcript_entry("assistant", answer, task_id=task_id)
            )
            conversation.updated_at = _timestamp()
            self._save_conversation(conversation)
            return answer
        except TaskCancelledError:
            self.mark_task_finalize_pending(
                task_id,
                "cancelled",
                {"status": "cancelled", "reason": "user"},
            )
            raise
        except Exception as exc:
            self.mark_task_finalize_pending(
                task_id,
                "failed",
                {
                    "status": "failed",
                    "error_code": "task_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                    "recoverable": True,
                },
            )
            raise
        finally:
            self._release_task(task_id)

    def complete_task_checkpoint(self, task_id: str) -> None:
        self.task_checkpoints.clear(task_id)

    def task_protection_status(self, task_id: str) -> dict[str, Any]:
        return self.workspace_protection.task_status(task_id)

    def pending_rollback_recoveries(self) -> list[dict[str, Any]]:
        return self.workspace_protection.pending_rollback_recoveries()

    def acknowledge_rollback_recovery(self, task_id: str) -> None:
        self.workspace_protection.acknowledge_rollback_recovery(task_id)

    def finalize_task_changes(self, task_id: str, outcome: str) -> dict[str, Any]:
        # ToolRegistry may return promptly from cooperative cancellation while a
        # generic/MCP handler is still unwinding in a daemon worker.  The task's
        # POST snapshot must be taken only after all such handlers have stopped,
        # otherwise they could write after the recorded task boundary.
        self.registry.wait_for_quiescence(task_id=task_id)
        changes = self.workspace_protection.finalize_task(task_id, outcome)
        if changes.get("has_changes"):
            # Prevent search_code from serving a pre-change semantic index.
            self.rag_source_store.clear_index_metadata()
        return changes

    def rollback_task_changes(
        self,
        task_id: str,
        conversation_id: str,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        self._get_conversation(conversation_id)
        result = self.workspace_protection.rollback_task(
            task_id,
            conversation_id,
            snapshot_id,
        )
        self.rag_source_store.clear_index_metadata()
        return result

    def task_change_diff(
        self,
        task_id: str,
        conversation_id: str,
        max_chars: int = 80_000,
    ) -> dict[str, Any]:
        self._get_conversation(conversation_id)
        return self.workspace_protection.task_diff(
            task_id,
            conversation_id,
            max_chars=max_chars,
        )

    def _mode_recovery_prompt(
        self,
        conversation: ConversationRuntime,
        original_prompt: str,
    ) -> str:
        plan_entries = [
            item.get("plan")
            for item in conversation.transcript
            if item.get("role") == "plan" and isinstance(item.get("plan"), dict)
        ]
        evidence = json.dumps(plan_entries[-1:], ensure_ascii=False, indent=2)
        return (
            "[RUNTIME_RECOVERY] Resume an interrupted desktop task. Do not blindly "
            "repeat completed side effects. Inspect the current state first and continue "
            "only unfinished work.\n\n"
            f"Original task:\n{original_prompt}\n\n"
            f"Last durable plan evidence:\n{evidence}"
        )

    def _restore_execution_plan(
        self,
        conversation: ConversationRuntime,
        task_id: str,
    ) -> ExecutionPlan | None:
        plan_data = next(
            (
                item.get("plan")
                for item in reversed(conversation.transcript)
                if item.get("role") == "plan"
                and isinstance(item.get("plan"), dict)
                and item["plan"].get("task_id") == task_id
            ),
            None,
        )
        if not isinstance(plan_data, dict):
            return None
        plan = ExecutionPlan(
            id=f"recovered-{task_id}",
            goal=str(plan_data.get("goal") or "Resume interrupted plan"),
            summary=str(plan_data.get("summary") or ""),
        )
        for raw_step in plan_data.get("steps") or []:
            if not isinstance(raw_step, dict):
                continue
            try:
                task_type = TaskType(str(raw_step.get("task_type") or "ANALYSIS"))
            except ValueError:
                task_type = TaskType.ANALYSIS
            raw_status = str(raw_step.get("status") or "pending").lower()
            description = str(raw_step.get("description") or "")
            task = Task(
                id=str(raw_step.get("id") or ""),
                description=description,
                type=task_type,
                dependencies=[str(value) for value in raw_step.get("dependencies") or []],
            )
            if not task.id:
                continue
            if raw_status == "completed":
                task.status = TaskStatus.COMPLETED
                task.result = str(raw_step.get("result_preview") or "")
            elif raw_status == "failed":
                task.status = TaskStatus.FAILED
                task.error = str(raw_step.get("error") or "Interrupted plan step failed.")
            elif raw_status == "skipped":
                task.status = TaskStatus.SKIPPED
                task.error = str(raw_step.get("error") or "Dependency was not completed.")
            elif raw_status == "running":
                task.description = (
                    "[Recovery: this step was interrupted and its side effects are "
                    "unconfirmed. Inspect current state before doing anything again.] "
                    f"{description}"
                )
            plan.add_task(task)
        if not plan.tasks:
            return None
        plan.execution_order = [str(value) for value in plan_data.get("execution_order") or []]
        return plan

    def cancel_task(self, task_id: str) -> bool:
        self._ensure_task_state()
        with self._task_lock:
            cancel_event = self._task_cancel_events.get(task_id)
            if cancel_event is None:
                return False
            cancel_event.set()
        self.hitl_handler.reject_task(task_id, "Task cancelled by user.")
        return True

    def _is_task_cancelled(self, task_id: str) -> bool:
        self._ensure_task_state()
        with self._task_lock:
            cancel_event = self._task_cancel_events.get(task_id)
            return bool(cancel_event and cancel_event.is_set())

    def _register_task(self, task_id: str, conversation_id: str) -> None:
        self._ensure_task_state()
        with self._task_lock:
            existing_task = self._conversation_tasks.get(conversation_id)
            if existing_task and existing_task != task_id:
                raise RuntimeError("another task is already running in this conversation")
            existing_conversation = self._task_conversations.get(task_id)
            if existing_conversation and existing_conversation != conversation_id:
                raise RuntimeError("task id is already active in another conversation")
            self._task_conversations[task_id] = conversation_id
            self._conversation_tasks[conversation_id] = task_id
            self._task_cancel_events.setdefault(task_id, threading.Event())

    def _release_task(self, task_id: str) -> None:
        self._ensure_task_state()
        with self._task_lock:
            conversation_id = self._task_conversations.pop(task_id, None)
            self._task_cancel_events.pop(task_id, None)
            if conversation_id and self._conversation_tasks.get(conversation_id) == task_id:
                self._conversation_tasks.pop(conversation_id, None)
            if getattr(self, "_active_task_id", None) == task_id:
                self._active_task_id = None
                self._task_cancel_event = None

    def _ensure_task_state(self) -> None:
        """Initialize the concurrent task maps for legacy embedded runtimes.

        RuntimeSession instances created normally always initialize these maps
        in ``__init__``.  The lazy bridge keeps older integrations that restore
        or construct a RuntimeSession through its former single-task fields
        operational while the desktop protocol moves to per-conversation tasks.
        """

        if hasattr(self, "_task_cancel_events"):
            return
        self._task_cancel_events = {}
        self._task_conversations = {}
        self._conversation_tasks = {}
        legacy_task_id = getattr(self, "_active_task_id", None)
        legacy_conversation_id = getattr(self, "_active_conversation_id", None)
        legacy_cancel_event = getattr(self, "_task_cancel_event", None)
        if legacy_task_id:
            self._task_cancel_events[legacy_task_id] = (
                legacy_cancel_event
                if isinstance(legacy_cancel_event, threading.Event)
                else threading.Event()
            )
        if legacy_task_id and legacy_conversation_id:
            self._task_conversations[legacy_task_id] = legacy_conversation_id
            self._conversation_tasks[legacy_conversation_id] = legacy_task_id

    def reset(self, conversation_id: str) -> int:
        conversation = self._get_conversation(conversation_id)
        cleared = max(0, len(conversation.agent.messages) - 1)
        conversation.event_floor_sequence = self.event_journal.sequence_snapshot().get(
            conversation_id,
            0,
        )
        conversation.agent.reset()
        conversation.team_agent.reset()
        conversation.memory_manager.clear_short_term()
        conversation.usage_ledger = UsageLedger(self.settings.context_window)
        conversation.transcript.clear()
        conversation.updated_at = _timestamp()
        self.hitl_handler.clear_approved_all()
        skill_buffer = getattr(
            conversation,
            "skill_context_buffer",
            getattr(self, "skill_context_buffer", None),
        )
        if skill_buffer is not None:
            skill_buffer.clear()
        self._save_conversation(conversation)
        return cleared

    def set_mode(self, conversation_id: str, mode: str) -> None:
        self._validate_mode(mode)
        conversation = self._get_conversation(conversation_id)
        conversation.mode = mode
        conversation.updated_at = _timestamp()
        self._save_conversation(conversation)

    def set_trace(self, conversation_id: str, enabled: bool) -> dict[str, Any]:
        conversation = self._get_conversation(conversation_id)
        if self._active_conversation_id != conversation_id:
            raise RuntimeError("open the conversation before changing trace recording")
        conversation.trace_enabled = enabled
        conversation.updated_at = _timestamp()
        if enabled:
            self._sync_trace_recorder(conversation)
            self.trace_recorder.record(
                "trace_setting_changed",
                conversation_id=conversation.id,
                enabled=True,
            )
        else:
            self.trace_recorder.record(
                "trace_setting_changed",
                conversation_id=conversation.id,
                enabled=False,
            )
            self._sync_trace_recorder(conversation)
        self._save_conversation(conversation)
        return {
            "enabled": conversation.trace_enabled,
            "path": conversation.trace_path,
        }

    def record_runtime_event(
        self,
        event_type: str,
        data: dict[str, Any],
        session_id: str,
        task_id: str | None,
    ) -> None:
        self.trace_recorder.record_for_session(
            session_id,
            "runtime_event",
            event_type=event_type,
            session_id=session_id,
            task_id=task_id,
            data=data,
        )
        conversation = self.conversations.get(session_id)
        if conversation and _record_plan_event(
            conversation.transcript,
            event_type,
            data,
            task_id,
        ):
            conversation.updated_at = _timestamp()
            self._save_conversation(conversation)
        elif conversation and event_type == "history.compacted":
            conversation.updated_at = _timestamp()
            self._save_conversation(conversation)
        if task_id and event_type in {
            "plan.created",
            "plan.step.started",
            "plan.step.completed",
            "plan.step.failed",
            "plan.step.skipped",
        }:
            self.task_checkpoints.update(
                task_id,
                checkpoint_stage=event_type,
                updated_at=_timestamp(),
            )

    def get_mode(self, conversation_id: str) -> str:
        return self._get_conversation(conversation_id).mode

    def set_access_mode(self, mode: str) -> None:
        if mode not in ACCESS_MODES:
            raise ValueError(f"unsupported access mode: {mode}")
        self.hitl_handler.clear_approved_all()
        self.hitl_handler.set_access_mode(mode)
        self.access_mode = mode

    def set_conversation_access_mode(self, conversation_id: str, mode: str) -> dict[str, Any]:
        if mode not in ACCESS_MODES:
            raise ValueError(f"unsupported access mode: {mode}")
        conversation = self._get_conversation(conversation_id)
        if self.active_task_for_conversation(conversation_id):
            raise RuntimeError("cannot change access mode while this conversation is running")
        conversation.access_mode = mode
        return {"session_id": conversation_id, "mode": mode}

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        effective_arguments: dict[str, Any] | None,
        *,
        before_release: Callable[[], None] | None = None,
    ) -> bool:
        return self.hitl_handler.resolve(
            approval_id,
            decision,
            effective_arguments,
            before_release=before_release,
        )

    def safe_approval_arguments(
        self,
        approval_id: str,
        effective_arguments: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return self.hitl_handler.safe_effective_arguments(
            approval_id,
            effective_arguments,
        )

    def approval_context(self, approval_id: str) -> tuple[str, str] | None:
        return self.hitl_handler.context(approval_id)

    def close(self) -> None:
        with self._task_lock:
            for cancel_event in self._task_cancel_events.values():
                cancel_event.set()
        try:
            self.hitl_handler.reject_all("Runtime is shutting down.")
            for conversation in self.conversations.values():
                self._save_conversation(conversation)
        finally:
            try:
                self.mcp_manager.close()
            finally:
                try:
                    self.trace_recorder.close()
                finally:
                    # The project Side-Git index/refs have one cross-process owner.
                    # Release it deterministically on workspace switch/shutdown,
                    # even when another service failed to close cleanly.
                    self.workspace_protection.close()

    def _new_conversation(
        self,
        identifier: str,
        *,
        title: str,
        mode: str,
        created_at: str,
        updated_at: str,
        title_is_custom: bool,
        trace_enabled: bool,
        trace_path: str | None,
        transcript: list[dict[str, Any]],
        event_floor_sequence: int = 0,
        agent_messages: list[dict[str, Any]] | None = None,
        history: dict[str, Any] | None = None,
        short_term_memory: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        access_mode: str | None = None,
    ) -> ConversationRuntime:
        memory_manager = self.project_memory.create_conversation_manager(
            identifier,
            llm_client=self.llm_client,
        )
        skill_context_buffer = SkillContextBuffer()
        restored_short_term = False
        if short_term_memory is not None:
            try:
                memory_manager.restore_short_term(short_term_memory)
                restored_short_term = True
            except (KeyError, TypeError, ValueError) as exc:
                self._progress(
                    f"Short-term memory restore failed for conversation {identifier}; "
                    f"rebuilding from transcript: {exc}"
                )
        if not restored_short_term:
            # Compatibility path for schema-v2 and older conversations. This can invoke
            # compression; new snapshots restore directly and never repeat LLM work.
            for item in transcript:
                role = item.get("role")
                content = str(item.get("content") or "")
                if role == "user":
                    memory_manager.add_user_message(content)
                elif role == "assistant":
                    memory_manager.add_assistant_message(content)

        def event_callback(event_type: str, data: dict[str, Any]) -> None:
            self._emit_event(event_type, data)

        common = {
            "llm_client": self.llm_client,
            "tool_registry": self.registry,
            "memory_manager": memory_manager,
            "progress_callback": self._progress,
            "skill_registry": self.skill_registry,
            "workspace": self.workspace,
            "context_window": self.settings.context_window,
            "rag_auto_retrieval": self.settings.rag_auto_retrieval,
        }
        history_data = history or {}
        agent = Agent(
            **common,
            max_iterations=self.settings.max_iterations,
            event_callback=event_callback,
            checkpoint_callback=lambda stage, conversation_id=identifier: (
                self._checkpoint_conversation(conversation_id, stage)
            ),
            skill_context_buffer=skill_context_buffer,
            history_summary=str(history_data.get("summary") or ""),
            history_compaction_count=int(history_data.get("compaction_count") or 0),
            history_last_compacted_at=(
                str(history_data["last_compacted_at"])
                if history_data.get("last_compacted_at")
                else None
            ),
        )
        if agent_messages:
            agent.messages = agent_messages
        plan_agent = PlanExecuteAgent(
            **common,
            max_iterations_per_task=self.settings.max_iterations,
            max_parallel_tasks=self.settings.plan_workers,
            skill_context_buffer=skill_context_buffer,
            event_callback=event_callback,
        )
        team_agent = AgentOrchestrator(
            **common,
            worker_count=self.settings.team_workers,
            max_retries_per_step=self.settings.team_retries,
            max_iterations_per_agent=self.settings.max_iterations,
            event_callback=event_callback,
            # Mailboxes survive a Sidecar restart without cluttering user code.
            message_bus_dir=(
                getattr(self, "project_data_dir", self.workspace / ".stellarcode" / "runtime")
                / "team-message-bus"
                / identifier
            ),
        )
        return ConversationRuntime(
            id=identifier,
            title=title,
            mode=mode,
            access_mode=(
                access_mode
                if access_mode in ACCESS_MODES
                else getattr(self, "access_mode", "restricted")
            ),
            created_at=created_at,
            updated_at=updated_at,
            title_is_custom=title_is_custom,
            trace_enabled=trace_enabled,
            trace_path=trace_path,
            transcript=transcript,
            event_floor_sequence=event_floor_sequence,
            memory_manager=memory_manager,
            agent=agent,
            plan_agent=plan_agent,
            team_agent=team_agent,
            usage_ledger=UsageLedger(self.settings.context_window, usage),
            skill_context_buffer=skill_context_buffer,
        )

    def _load_conversations(self) -> None:
        for path in self.conversation_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                identifier = str(payload["id"])
                mode = str(payload.get("mode") or "react")
                self._validate_mode(mode)
                agent_messages, repair_count = repair_tool_message_history(
                    list(payload.get("agent_messages") or [])
                )
                conversation = self._new_conversation(
                    identifier,
                    title=str(payload.get("title") or "New conversation"),
                    mode=mode,
                    created_at=str(payload.get("created_at") or _timestamp()),
                    updated_at=str(payload.get("updated_at") or _timestamp()),
                    title_is_custom=bool(payload.get("title_is_custom")),
                    trace_enabled=bool(payload.get("trace_enabled")),
                    trace_path=(str(payload["trace_path"]) if payload.get("trace_path") else None),
                    transcript=list(payload.get("transcript") or []),
                    event_floor_sequence=_nonnegative_int(payload.get("event_floor_sequence")),
                    agent_messages=agent_messages,
                    history=(
                        payload.get("history") if isinstance(payload.get("history"), dict) else None
                    ),
                    short_term_memory=(
                        payload.get("short_term_memory")
                        if isinstance(payload.get("short_term_memory"), dict)
                        else None
                    ),
                    usage=(
                        payload.get("usage") if isinstance(payload.get("usage"), dict) else None
                    ),
                    access_mode=getattr(self, "access_mode", "restricted"),
                )
                self.conversations[identifier] = conversation
                needs_schema_upgrade = _nonnegative_int(payload.get("schema_version")) < 3
                if repair_count:
                    self._progress(
                        f"Repaired {repair_count} interrupted tool message(s) "
                        f"in conversation {identifier}."
                    )
                if repair_count or needs_schema_upgrade:
                    self._save_conversation(conversation)
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._progress(f"Conversation load failed for {path.name}: {exc}")

    def _save_conversation(self, conversation: ConversationRuntime) -> None:
        persisted_messages, _ = repair_tool_message_history(conversation.agent.messages)
        payload = {
            "schema_version": 3,
            "id": conversation.id,
            "project_id": self.project_id,
            "title": conversation.title,
            "title_is_custom": conversation.title_is_custom,
            "mode": conversation.mode,
            "trace_enabled": conversation.trace_enabled,
            "trace_path": conversation.trace_path,
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at,
            "event_floor_sequence": conversation.event_floor_sequence,
            "transcript": conversation.transcript,
            "agent_messages": persisted_messages,
            "history": conversation.agent.history_snapshot(),
            "short_term_memory": conversation.memory_manager.short_term_snapshot(),
            "usage": conversation.usage_ledger.snapshot(),
        }
        with self._persistence_lock:
            path = self._conversation_path(conversation.id)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)

    def _checkpoint_conversation(self, conversation_id: str, stage: str) -> None:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            return
        conversation.updated_at = _timestamp()
        self._save_conversation(conversation)
        with self._task_lock:
            task_id = self._conversation_tasks.get(conversation_id)
        if task_id:
            self.task_checkpoints.update(
                task_id,
                checkpoint_stage=stage,
                updated_at=_timestamp(),
            )

    def _conversation_path(self, conversation_id: str) -> Path:
        safe_id = conversation_id.replace("/", "_").replace("\\", "_")
        return self.conversation_dir / f"{safe_id}.json"

    def _get_conversation(self, conversation_id: str) -> ConversationRuntime:
        try:
            return self.conversations[conversation_id]
        except KeyError as exc:
            raise KeyError(f"conversation not found: {conversation_id}") from exc

    def _validate_mode(self, mode: str) -> None:
        if mode not in {"react", "plan", "team"}:
            raise ValueError(f"unsupported agent mode: {mode}")

    def _sync_trace_recorder(self, conversation: ConversationRuntime) -> None:
        configure = getattr(self.trace_recorder, "configure", None)
        if callable(configure):
            self.trace_recorder.select(conversation.id)
            path = configure(
                conversation.id,
                conversation.trace_enabled,
                workspace=str(self.workspace),
                project_id=self.project_id,
                conversation_id=conversation.id,
                conversation_title=conversation.title,
                source="desktop",
            )
        elif conversation.trace_enabled:
            path = self.trace_recorder.enable(
                workspace=str(self.workspace),
                project_id=self.project_id,
                conversation_id=conversation.id,
                conversation_title=conversation.title,
                source="desktop",
            )
        else:
            path = self.trace_recorder.disable()
        conversation.trace_path = str(path) if path is not None else conversation.trace_path
        self._trace_conversation_id = conversation.id if conversation.trace_enabled else None

    def _progress(self, message: str) -> None:
        self._emit_event(
            "assistant.thinking",
            {"status": "active", "summary": message},
        )

    def _mcp_status_changed(self, server: Any) -> None:
        if server.name == "chrome-devtools":
            controller = getattr(self, "browser_controller", None)
            if controller is not None:
                controller.sync_from_server()
        self._emit(
            "mcp.status_changed",
            self.mcp_manager.server_snapshot(server),
            f"workspace-{self.project_id}",
            None,
        )

    def _record_usage(
        self,
        usage: TokenUsage,
        provider: str,
        model: str,
        operation: str,
        session_id: str,
        task_id: str,
    ) -> None:
        with self._task_lock:
            active_conversation_id = self._active_conversation_id or ""
            active_for_session = self._conversation_tasks.get(session_id or "") or ""
        if task_id and self._task_conversations.get(task_id) != session_id:
            # A cancelled synchronous HTTP request may finish in a detached thread. Its
            # usage must not be attributed to a newer task.
            return
        conversation_id = session_id or active_conversation_id
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            return
        effective_task_id = task_id or active_for_session
        payload = conversation.usage_ledger.record(
            usage,
            provider=provider,
            model=model,
            operation=operation,
            task_id=effective_task_id,
        )
        conversation.updated_at = _timestamp()
        self._save_conversation(conversation)
        self._emit(
            "usage.updated",
            payload,
            conversation_id,
            effective_task_id or None,
        )

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        session_id, task_id = current_llm_scope()
        if not session_id:
            session_id = self._active_conversation_id or f"workspace-{self.project_id}"
        if not task_id:
            with self._task_lock:
                task_id = self._conversation_tasks.get(session_id) or ""
        self._emit(
            event_type,
            data,
            session_id,
            task_id or None,
        )


def _explicit_reference_context(prompt: str, skill_registry: SkillRegistry) -> str:
    sections: list[str] = []
    skill_context = explicit_skill_context(prompt, skill_registry)
    if skill_context:
        sections.append(skill_context)

    mcp_names = list(
        dict.fromkeys(
            re.findall(
                r"(?<![\w@])@mcp:(mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+)",
                prompt,
            )
        )
    )[:8]
    if mcp_names:
        rendered = "\n".join(f"- {name}" for name in mcp_names)
        sections.append(
            "## Explicitly referenced MCP tools\n"
            f"{rendered}\n"
            "Prefer or consider these exact tools when they are relevant to the "
            "request. Do not call them blindly, invent arguments, or bypass normal "
            "approval and security policy."
        )
    return "\n\n".join(sections)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _desktop_diagnostics_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Normalize the diagnostics service schema for the stable desktop protocol."""
    counts = snapshot.get("counts") if isinstance(snapshot.get("counts"), dict) else {}
    raw_status = str(snapshot.get("status") or "idle")
    status = "not_run" if raw_status == "idle" else raw_status
    providers = []
    for raw in snapshot.get("providers") or []:
        if not isinstance(raw, dict):
            continue
        detail = str(raw.get("reason") or raw.get("version") or raw.get("executable") or "")
        kind = str(raw.get("kind") or "lint")
        normalized_kind = {
            "compiler": "build",
            "language-server": "lsp",
            "linter": "lint",
        }.get(kind, kind)
        providers.append(
            {
                "id": str(raw.get("id") or "unknown"),
                "label": str(raw.get("name") or raw.get("id") or "Unknown"),
                "kind": normalized_kind,
                "available": bool(raw.get("available")),
                "detail": detail,
            }
        )
    # The current Python Runtime does not bundle an LSP process. Reporting this
    # explicitly prevents syntax/Ruff results from being misrepresented as LSP data.
    if not any(provider["kind"] == "lsp" for provider in providers):
        providers.append(
            {
                "id": "lsp",
                "label": "Language Server Protocol",
                "kind": "lsp",
                "available": False,
                "detail": "No project Language Server is configured.",
            }
        )
    return {
        "workspace": str(snapshot.get("workspace") or ""),
        "status": status,
        "run_id": str(snapshot.get("run_id") or "") or None,
        "profile": "build" if snapshot.get("profile") == "build" else "safe",
        "problems": [
            {
                **problem,
                "severity": (
                    "information" if problem.get("severity") == "info" else problem.get("severity")
                ),
            }
            for problem in snapshot.get("diagnostics") or []
            if isinstance(problem, dict)
        ],
        "error_count": _nonnegative_int(counts.get("error")),
        "warning_count": _nonnegative_int(counts.get("warning")),
        "information_count": _nonnegative_int(counts.get("info")),
        "providers": providers,
        "detected_projects": [
            {
                "kind": str(project.get("kind") or "unknown"),
                "root": str(project.get("root") or ""),
                "markers": list(project.get("markers") or []),
            }
            for project in snapshot.get("detected_projects") or []
            if isinstance(project, dict)
        ],
        "started_at": snapshot.get("started_at"),
        "finished_at": snapshot.get("finished_at"),
        "stale": bool(snapshot.get("stale")),
        "error": str(snapshot.get("error") or "") or None,
        "files_scanned": _nonnegative_int(snapshot.get("files_scanned")),
        "provider_messages": list(snapshot.get("provider_messages") or []),
        "storage_path": str(snapshot.get("storage_path") or ""),
    }


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _task_workspace_prompt_context(
    project_workspace: str | Path,
    task_workspace: object,
) -> str:
    """Describe task isolation without letting the model expose it as the project path."""

    canonical = str(Path(project_workspace).resolve())
    isolated = str(Path(str(task_workspace)).resolve()) if task_workspace else ""
    return "\n".join(
        (
            "<stellarcode_workspace_context>",
            f"canonical_project_root={json.dumps(canonical, ensure_ascii=False)}",
            f"ephemeral_task_worktree={json.dumps(isolated, ensure_ascii=False)}",
            "All relative file and command operations are routed to the ephemeral worktree.",
            "The worktree is an internal implementation detail and is deleted after merge.",
            "When the user asks for the project or file location, report a path under "
            "canonical_project_root, never ephemeral_task_worktree.",
            "Do not start detached/background processes from the ephemeral worktree.",
            "</stellarcode_workspace_context>",
        )
    )


def _transcript_entry(
    role: str,
    content: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": f"message-{uuid.uuid4().hex}",
        "role": role,
        "content": content,
        "timestamp": _timestamp(),
    }
    if attachments:
        entry["attachments"] = attachments
    if task_id:
        entry["task_id"] = task_id
    return entry


def _record_plan_event(
    transcript: list[dict[str, Any]],
    event_type: str,
    data: dict[str, Any],
    task_id: str | None,
) -> bool:
    if event_type == "plan.created" and task_id:
        tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
        plan = {
            "task_id": task_id,
            "goal": str(data.get("goal") or ""),
            "summary": str(data.get("summary") or ""),
            "execution_order": list(data.get("execution_order") or []),
            "steps": [
                {
                    "id": str(item.get("id") or ""),
                    "description": str(item.get("description") or ""),
                    "task_type": str(item.get("task_type") or "ANALYSIS"),
                    "dependencies": list(item.get("dependencies") or []),
                    "status": "pending",
                }
                for item in tasks
                if isinstance(item, dict)
            ],
        }
        entry = next(
            (
                item
                for item in reversed(transcript)
                if item.get("role") == "plan"
                and isinstance(item.get("plan"), dict)
                and item["plan"].get("task_id") == task_id
            ),
            None,
        )
        if entry is None:
            entry = _transcript_entry("plan", plan["goal"])
            transcript.append(entry)
        else:
            entry["content"] = plan["goal"]
            entry["timestamp"] = _timestamp()
        entry["plan"] = plan
        return True

    if not event_type.startswith("plan.step.") or not task_id:
        if event_type not in {"task.failed", "task.cancelled"} or not task_id:
            return False

    entry = next(
        (
            item
            for item in reversed(transcript)
            if item.get("role") == "plan"
            and isinstance(item.get("plan"), dict)
            and item["plan"].get("task_id") == task_id
        ),
        None,
    )
    if entry is None:
        return False
    plan = entry["plan"]

    if event_type in {"task.failed", "task.cancelled"}:
        terminal_status = "failed" if event_type == "task.failed" else "cancelled"
        for step in plan.get("steps", []):
            if step.get("status") in {"pending", "running"}:
                step["status"] = terminal_status
        return True

    step_id = str(data.get("step_id") or "")
    step = next((item for item in plan.get("steps", []) if item.get("id") == step_id), None)
    if step is None:
        return False
    if event_type == "plan.step.started":
        step["status"] = "running"
        step.pop("result_preview", None)
        step.pop("error", None)
    elif event_type == "plan.step.completed":
        step["status"] = "completed"
        step["result_preview"] = str(data.get("result_preview") or "")
        step.pop("error", None)
    elif event_type == "plan.step.failed":
        step["status"] = "failed"
        step["error"] = str(data.get("error") or "")
    elif event_type == "plan.step.skipped":
        step["status"] = "skipped"
        step["error"] = str(data.get("reason") or "")
    else:
        return False
    return True


def _automatic_title(prompt: str) -> str:
    first_line = next(
        (line.strip() for line in prompt.splitlines() if line.strip()), "New conversation"
    )
    return first_line[:48]
