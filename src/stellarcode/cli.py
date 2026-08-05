from __future__ import annotations

import argparse
import atexit
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from stellarcode import __version__
from stellarcode.agent import Agent
from stellarcode.browser import (
    BrowserController,
    BrowserGuard,
    BrowserSession,
    register_browser_tools,
)
from stellarcode.hitl import ApprovalRequest, TerminalHitlHandler
from stellarcode.llm import create_chat_client
from stellarcode.memory import MemoryManager
from stellarcode.mcp import McpConfigError, McpConfigLoader, McpServerManager, McpServerStatus
from stellarcode.multi_agent import AgentOrchestrator
from stellarcode.plan import PlanExecuteAgent, should_plan
from stellarcode.rag import EmbeddingClient, RagService, SearchResultFormatter
from stellarcode.skill import (
    SkillContextBuffer,
    SkillRegistry,
    SkillStateStore,
    builtin_skills_dir,
    format_skill_warnings,
    handle_skill_command,
    register_skill_tools,
    startup_summary,
)
from stellarcode.tools import build_default_registry
from stellarcode.trace import TraceRecorder, TracingChatClient


ACCESS_MODES = ("restricted", "full-access")


class _PlainConsole:
    def print(self, *values: object, **_: object) -> None:
        print(*values)


class _TracingConsole:
    def __init__(self, delegate: object, recorder: TraceRecorder) -> None:
        self._delegate = delegate
        self._recorder = recorder

    def print(self, *values: object, **kwargs: object) -> None:
        self._recorder.record(
            "console_output",
            text=" ".join(str(value) for value in values),
        )
        self._delegate.print(*values, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


def _console() -> object:
    try:
        from rich.console import Console
    except ModuleNotFoundError:
        return _PlainConsole()
    return Console()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _render_approval(console: object, request: ApprovalRequest) -> None:
    try:
        from stellarcode.hitl.render import build_approval_panel
    except ModuleNotFoundError:
        console.print(request.to_display_text(use_icons=False))
        return
    encoding = getattr(getattr(console, "file", None), "encoding", None) or "utf-8"
    try:
        "⚠️🟡".encode(encoding)
        use_icons = True
    except (LookupError, UnicodeEncodeError):
        use_icons = False
    console.print(build_approval_panel(request, use_icons=use_icons))


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="StellarCode Python MVP")
    parser.add_argument(
        "--workspace",
        default=".",
        help="Working directory used to resolve relative paths and run commands.",
    )
    parser.add_argument(
        "--mode",
        choices=ACCESS_MODES,
        default="restricted",
        help=(
            "Access mode: restricted requires approval for risky tools; "
            "full-access executes all tools without approval."
        ),
    )
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument("--memory-dir", default=None, help="Directory for long-term memory JSON.")
    parser.add_argument("--rag-dir", default=None, help="Directory for the SQLite code index.")
    parser.add_argument("--short-memory-tokens", type=int, default=8192)
    parser.add_argument("--team-workers", type=int, default=2)
    parser.add_argument("--team-retries", type=int, default=2)
    parser.add_argument(
        "--plan-workers",
        type=_positive_int,
        default=4,
        help="Maximum independent Plan DAG tasks to execute in parallel.",
    )
    parser.add_argument(
        "--max-parallel-tools",
        type=_positive_int,
        default=4,
        help="Maximum tool calls to execute concurrently in one LLM round.",
    )
    parser.add_argument(
        "--tool-batch-timeout",
        type=_positive_float,
        default=90,
        help="Timeout in seconds for a batch of parallel tool calls.",
    )
    parser.add_argument(
        "--auto-plan",
        action="store_true",
        help="Automatically use Plan-and-Execute for complex prompts.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Record complete Agent execution events to a JSONL session trace.",
    )
    parser.add_argument(
        "--trace-dir",
        default=None,
        help="Trace directory (default: <workspace>/.stellarcode/traces).",
    )
    return parser


def handle_trace_command(
    command: str,
    recorder: TraceRecorder,
    *,
    workspace: str | Path,
) -> str:
    parts = command.split()
    if parts == ["/trace"] or parts == ["/trace", "status"]:
        return recorder.status()
    if parts == ["/trace", "on"]:
        already_enabled = recorder.enabled
        path = recorder.enable(workspace=str(Path(workspace).resolve()), source="runtime")
        if already_enabled:
            return f"trace mode is already on; file: {path}"
        return f"trace mode enabled; file: {path}"
    if parts == ["/trace", "off"]:
        if not recorder.enabled:
            return recorder.status()
        path = recorder.disable()
        return f"trace mode disabled; file: {path}"
    return "usage: /trace | /trace on | /trace off | /trace status"


def switch_access_mode(
    current_mode: str,
    target_mode: str,
    hitl_handler: TerminalHitlHandler,
    confirmation_func: Callable[[str], str] = input,
) -> tuple[str, str]:
    if target_mode not in ACCESS_MODES:
        return current_mode, "usage: /mode restricted | /mode full-access"
    if target_mode == current_mode:
        return current_mode, f"access mode is already {current_mode}"

    if target_mode == "full-access":
        try:
            confirmation = confirmation_func(
                "Full access disables all StellarCode approval prompts. "
                "Type FULL ACCESS to continue: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            confirmation = ""
        if confirmation != "FULL ACCESS":
            return current_mode, "mode switch cancelled; access mode remains restricted"

    hitl_handler.clear_approved_all()
    hitl_handler.set_enabled(target_mode == "restricted")
    return target_mode, f"switched to {target_mode} mode"


def handle_mcp_command(command: str, manager: McpServerManager) -> str:
    parts = command.split()
    if parts == ["/mcp"]:
        return manager.format_status()
    if len(parts) != 3 or parts[0] != "/mcp":
        return (
            "usage: /mcp | /mcp restart <name> | /mcp logs <name> | "
            "/mcp disable <name> | /mcp enable <name>"
        )
    action, name = parts[1], parts[2]
    if action == "restart":
        return manager.restart(name)
    if action == "logs":
        return manager.logs(name)
    if action == "disable":
        return manager.disable(name)
    if action == "enable":
        return manager.enable(name)
    return (
        "usage: /mcp | /mcp restart <name> | /mcp logs <name> | "
        "/mcp disable <name> | /mcp enable <name>"
    )


def handle_browser_command(command: str, controller: BrowserController) -> str:
    parts = command.split()
    if parts in (["/browser"], ["/browser", "status"]):
        return controller.status()
    if parts == ["/browser", "connect"]:
        return controller.connect()
    if len(parts) == 3 and parts[:2] == ["/browser", "connect"]:
        try:
            port = int(parts[2])
        except ValueError:
            return "usage: /browser connect [port]; port must be an integer"
        return controller.connect(port)
    if parts == ["/browser", "disconnect"]:
        return controller.disconnect()
    if parts == ["/browser", "tabs"]:
        return controller.tabs()
    return (
        "usage: /browser | /browser status | /browser connect [port] | "
        "/browser tabs | /browser disconnect"
    )


def main() -> None:
    args = create_parser().parse_args()

    load_dotenv()
    workspace = Path(args.workspace).resolve()
    trace_dir = (
        Path(args.trace_dir).resolve()
        if args.trace_dir
        else workspace / ".stellarcode" / "traces"
    )
    trace_recorder = TraceRecorder(trace_dir)
    if args.trace:
        trace_recorder.enable(workspace=str(workspace), source="startup")
    atexit.register(trace_recorder.close)
    console = _TracingConsole(_console(), trace_recorder)

    embedding_client = EmbeddingClient()
    rag_service = RagService(
        workspace,
        storage_dir=args.rag_dir,
        embedding_client=embedding_client,
    )
    access_mode = args.mode
    browser_session = BrowserSession()
    browser_guard = BrowserGuard(browser_session)
    hitl_handler = TerminalHitlHandler(
        enabled=access_mode == "restricted",
        output_func=lambda message: console.print(message, markup=False),
        render_func=lambda request: _render_approval(console, request),
        trace_recorder=trace_recorder,
    )
    registry = build_default_registry(
        workspace,
        rag_service=rag_service,
        hitl_handler=hitl_handler,
        max_parallel_tools=args.max_parallel_tools,
        tool_batch_timeout_seconds=args.tool_batch_timeout,
        trace_recorder=trace_recorder,
    )
    skill_state_store = SkillStateStore(Path.home() / ".stellarcode" / "skills.json")
    skill_registry = SkillRegistry(
        builtin_dir=builtin_skills_dir(),
        user_dir=Path.home() / ".stellarcode" / "skills",
        project_dir=workspace / ".stellarcode" / "skills",
        state_store=skill_state_store,
    )
    skill_registry.reload()
    skill_context_buffer = SkillContextBuffer()
    register_skill_tools(registry, skill_registry, skill_context_buffer)
    mcp_config_loader = McpConfigLoader(workspace)
    bootstrap_message = mcp_config_loader.bootstrap_chrome_devtools()
    if bootstrap_message:
        console.print(bootstrap_message, markup=False)
    mcp_manager = McpServerManager(
        registry,
        workspace,
        config_loader=mcp_config_loader,
        browser_guard=browser_guard,
    )
    try:
        mcp_manager.load_configured_servers()
        mcp_manager.start_all(
            progress=lambda message: console.print(message, markup=False)
        )
    except McpConfigError as exc:
        console.print(f"MCP configuration error: {exc}", markup=False)
    atexit.register(mcp_manager.close)
    browser_controller = BrowserController.create(
        browser_session,
        mcp_manager,
        registry,
    )
    register_browser_tools(registry, browser_controller)
    base_llm_client = create_chat_client()
    llm_client = TracingChatClient(base_llm_client, trace_recorder)
    memory_manager = MemoryManager(
        storage_dir=args.memory_dir,
        short_term_tokens=args.short_memory_tokens,
    )
    agent = Agent(
        llm_client=llm_client,
        tool_registry=registry,
        max_iterations=args.max_iterations,
        memory_manager=memory_manager,
        progress_callback=lambda message: console.print(message, markup=False),
        skill_registry=skill_registry,
        skill_context_buffer=skill_context_buffer,
        workspace=workspace,
    )
    plan_agent = PlanExecuteAgent(
        llm_client=llm_client,
        tool_registry=registry,
        max_iterations_per_task=args.max_iterations,
        memory_manager=memory_manager,
        max_parallel_tasks=args.plan_workers,
        progress_callback=lambda message: console.print(message, markup=False),
        skill_registry=skill_registry,
        skill_context_buffer=skill_context_buffer,
        workspace=workspace,
    )
    team_agent = AgentOrchestrator(
        llm_client=llm_client,
        tool_registry=registry,
        memory_manager=memory_manager,
        worker_count=args.team_workers,
        max_retries_per_step=args.team_retries,
        max_iterations_per_agent=args.max_iterations,
        progress_callback=lambda message: console.print(message, markup=False),
        skill_registry=skill_registry,
        workspace=workspace,
    )
    plan_mode = False
    team_mode = False

    console.print(f"StellarCode Python v{__version__}")
    console.print(f"Working directory: {workspace}")
    console.print(
        f"Model: {base_llm_client.provider_name}/{base_llm_client.model}",
        markup=False,
    )
    if access_mode == "restricted":
        console.print("Access mode: restricted (risky operations require approval)")
    else:
        console.print(
            "Access mode: full-access (no StellarCode path, network, or approval restrictions)",
            markup=False,
        )
    console.print("Type /plan for Plan-and-Execute or /team for one Multi-Agent task.")
    console.print("Type /react to return to ReAct, /clear to reset the current context.")
    console.print(
        "Type /memory for memory status, /save <fact> to persist, "
        "/recall <query> to search."
    )
    console.print("Type /index, /search <query>, or /graph <name> for code RAG.")
    ready_mcp = sum(
        server.status == McpServerStatus.READY for server in mcp_manager.servers()
    )
    console.print(
        f"MCP servers: {ready_mcp}/{len(mcp_manager.servers())} ready; "
        "type /mcp for details."
    )
    console.print(
        "Type /browser status or /browser connect to reuse a logged-in Chrome session."
    )
    console.print(f"{startup_summary(skill_registry)}; type /skill for details.")
    console.print(
        'Attach images with @image:<path>, @image:"path with spaces", or @clipboard.'
    )
    skill_warnings = format_skill_warnings(skill_registry, skill_state_store)
    if skill_warnings:
        console.print(skill_warnings, markup=False)
    console.print("Type /mode to inspect or /mode <restricted|full-access> to switch access.")
    console.print("Type /trace on to record complete execution details; /trace shows status.")
    if trace_recorder.enabled:
        console.print(
            f"Trace mode: on ({trace_recorder.path}). Logs may contain conversation and code.",
            markup=False,
        )
    console.print("Type /exit or /quit to leave.")

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nbye")
            return

        if not user_input:
            continue
        trace_recorder.record("cli_input", text=user_input)
        if user_input in {"/exit", "/quit", "exit", "quit"}:
            console.print("bye")
            return
        if user_input == "/clear":
            agent.reset()
            team_agent.reset()
            memory_manager.clear_short_term()
            hitl_handler.clear_approved_all()
            skill_context_buffer.clear()
            plan_mode = False
            team_mode = False
            console.print("context cleared; long-term memory kept")
            continue
        if user_input == "/mode":
            console.print(f"access mode: {access_mode}")
            continue
        if user_input == "/trace" or user_input.startswith("/trace "):
            console.print(
                handle_trace_command(
                    user_input,
                    trace_recorder,
                    workspace=workspace,
                ),
                markup=False,
            )
            continue
        if user_input.startswith("/mode "):
            target_mode = user_input.removeprefix("/mode ").strip()
            access_mode, message = switch_access_mode(
                access_mode,
                target_mode,
                hitl_handler,
                confirmation_func=input,
            )
            console.print(message)
            continue
        if user_input == "/hitl" or user_input.startswith("/hitl "):
            console.print(
                "HITL is controlled by access mode; use /mode restricted or "
                "/mode full-access"
            )
            continue
        if user_input == "/memory":
            console.print(memory_manager.status())
            continue
        if user_input == "/mcp" or user_input.startswith("/mcp "):
            console.print(handle_mcp_command(user_input, mcp_manager), markup=False)
            continue
        if user_input == "/skill" or user_input.startswith("/skill "):
            console.print(
                handle_skill_command(
                    user_input,
                    skill_registry,
                    skill_state_store,
                ),
                markup=False,
            )
            continue
        if user_input == "/browser" or user_input.startswith("/browser "):
            console.print(
                handle_browser_command(user_input, browser_controller),
                markup=False,
            )
            continue
        if user_input.startswith("/save "):
            fact = user_input.removeprefix("/save ").strip()
            entry = memory_manager.save_fact(fact)
            console.print(f"saved to long-term memory: {entry.content}")
            continue
        if user_input.startswith("/recall "):
            query = user_input.removeprefix("/recall ").strip()
            results = memory_manager.search(query)
            if not results:
                console.print("no matching memory")
            else:
                console.print(
                    "\n".join(
                        f"- {entry.id} [{entry.type.value}] {entry.content}"
                        for entry in results
                    )
                )
            continue
        if user_input == "/index" or user_input.startswith("/index "):
            index_path = user_input.removeprefix("/index").strip() or None
            rag_service.index(
                index_path,
                progress_callback=lambda message: console.print(message, markup=False),
            )
            continue
        if user_input == "/search":
            console.print("usage: /search <natural-language code query>")
            continue
        if user_input.startswith("/search "):
            query = user_input.removeprefix("/search ").strip()
            stats = rag_service.stats()
            if stats.chunk_count == 0:
                console.print("code index is empty; run /index first")
            else:
                results = rag_service.search(query)
                console.print(
                    SearchResultFormatter.format_for_cli(query, results),
                    markup=False,
                )
            continue
        if user_input == "/graph":
            console.print("usage: /graph <class, function, or method name>")
            continue
        if user_input.startswith("/graph "):
            name = user_input.removeprefix("/graph ").strip()
            console.print(
                SearchResultFormatter.format_graph(name, rag_service.graph(name)),
                markup=False,
            )
            continue
        if user_input == "/plan":
            plan_mode = True
            team_mode = False
            console.print("switched to Plan-and-Execute mode")
            continue
        if user_input == "/team":
            team_mode = True
            plan_mode = False
            console.print("the next task will use Multi-Agent mode, then return to ReAct")
            continue
        if user_input == "/react":
            plan_mode = False
            team_mode = False
            console.print("switched to ReAct mode")
            continue
        if user_input.startswith("/preview-plan "):
            goal = user_input.removeprefix("/preview-plan ").strip()
            console.print(plan_agent.preview_plan(goal))
            continue
        if user_input.startswith("/plan "):
            goal = user_input.removeprefix("/plan ").strip()
            console.print(plan_agent.run(goal))
            continue
        if user_input.startswith("/team "):
            goal = user_input.removeprefix("/team ").strip()
            console.print(team_agent.run(goal), markup=False)
            continue

        if team_mode:
            execution_mode = "team"
            team_mode = False
            answer = team_agent.run(user_input)
        elif plan_mode or (args.auto_plan and should_plan(user_input)):
            execution_mode = "plan"
            answer = plan_agent.run(user_input)
        else:
            execution_mode = "react"
            answer = agent.run(user_input)
        trace_recorder.record(
            "task_result",
            mode=execution_mode,
            result=answer,
        )
        console.print(answer, markup=False)


if __name__ == "__main__":
    main()
