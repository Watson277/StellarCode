"""Non-interactive StellarCode adapter for SWE-bench inference.

The outer evaluation runner owns repository checkout, answer collection, and the
private Docker evaluation. This module exposes only repository-scoped coding
tools to one Agent invocation and never receives gold patches or test metadata.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from stellarcode.agent import Agent
from stellarcode.benchmark import _is_within, _relative_workspace_path
from stellarcode.llm import create_chat_client
from stellarcode.tools import build_default_registry
from stellarcode.tools.builtin import _execute_command
from stellarcode.tools.registry import ToolDefinition, ToolExecutionError, ToolRegistry


ALLOWED_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "apply_patch",
        "list_dir",
        "glob_files",
        "grep_code",
        "execute_command",
    }
)
PATH_TOOL_NAMES = ALLOWED_TOOL_NAMES - {"execute_command"}
SWEBENCH_LLM_TIMEOUT_SECONDS = 180.0
SWEBENCH_SYSTEM_PROMPT = """You are StellarCode running a SWE-bench evaluation.

Resolve the supplied software issue in the current repository. Inspect the code, make the
smallest correct implementation change, and run relevant existing tests when practical.
Work only inside the current repository. Do not use the network, search for the original
GitHub issue or pull request, or attempt to locate reference patches or hidden tests. The
outer evaluator will collect your Git diff and run the private tests after you finish.
Do not merely describe a patch: edit the repository with the available tools. When done,
give a concise summary of the implementation and tests you ran. Package installation and
network commands are unavailable; work with the dependencies already present.
"""

FORBIDDEN_COMMAND_PATTERNS = (
    re.compile(r"(?i)\b(?:python(?:\.exe)?\s+-m\s+)?pip\d*\s+(?:install|uninstall|download|wheel)\b"),
    re.compile(r"(?i)\bconda\s+(?:install|remove|update|create)\b"),
    re.compile(r"(?i)\b(?:uv\s+pip|poetry|npm|pnpm|yarn)\s+(?:install|add|remove|update)\b"),
    re.compile(r"(?i)\b(?:apt(?:-get)?|apk|dnf|yum|choco|winget)\s+(?:install|remove|upgrade)\b"),
    re.compile(r"(?i)\b(?:curl|wget|invoke-webrequest|start-bitstransfer)\b"),
    re.compile(r"(?i)\bgit\s+(?:clone|fetch|pull)\b"),
)


@dataclass(frozen=True)
class SweBenchAgentOptions:
    workspace: Path
    prompt_file: Path
    result_file: Path
    max_iterations: int
    temperature: float

    @classmethod
    def parse(cls, argv: list[str]) -> "SweBenchAgentOptions":
        parser = argparse.ArgumentParser(
            prog="stellarcode swebench-agent",
            description="Run StellarCode once in a clean SWE-bench repository checkout.",
        )
        parser.add_argument("--workspace", required=True, type=Path)
        parser.add_argument("--prompt-file", required=True, type=Path)
        parser.add_argument("--result-file", required=True, type=Path)
        parser.add_argument("--max-iterations", type=_positive_int, default=40)
        parser.add_argument("--temperature", type=_temperature, default=0.2)
        args = parser.parse_args(argv)

        workspace = args.workspace.resolve()
        if not workspace.is_dir():
            parser.error(f"--workspace is not a directory: {workspace}")
        prompt_file = args.prompt_file.resolve()
        if not prompt_file.is_file():
            parser.error(f"--prompt-file is not a file: {prompt_file}")
        result_file = args.result_file.resolve()
        if _is_within(workspace, prompt_file) or _is_within(workspace, result_file):
            parser.error("prompt and result files must be outside --workspace")
        return cls(
            workspace=workspace,
            prompt_file=prompt_file,
            result_file=result_file,
            max_iterations=args.max_iterations,
            temperature=args.temperature,
        )


def run_swebench_agent(
    options: SweBenchAgentOptions,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    events: list[dict[str, Any]] = []
    model_name = "unknown"
    try:
        prompt = options.prompt_file.read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError(f"prompt file is empty: {options.prompt_file}")
        client = (
            client_factory()
            if client_factory is not None
            else create_chat_client(timeout_seconds=SWEBENCH_LLM_TIMEOUT_SECONDS)
        )
        model_name = str(getattr(client, "model", "unknown"))
        agent = Agent(
            llm_client=client,
            tool_registry=build_swebench_registry(options.workspace),
            system_prompt=SWEBENCH_SYSTEM_PROMPT,
            max_iterations=options.max_iterations,
            temperature=options.temperature,
            workspace=options.workspace,
            stream_output=True,
            progress_callback=lambda _message: None,
            event_callback=lambda event, data: events.append({"event": event, "data": data}),
        )
        response = agent.run(prompt)
        status = "completed"
        termination = "agent_finished"
        error = None
    except Exception as exc:  # Always persist a machine-readable failure result.
        response = ""
        status = "error"
        termination = "agent_error"
        error = f"{type(exc).__name__}: {exc}"

    result: dict[str, Any] = {
        "status": status,
        "termination": termination,
        "model": model_name,
        "temperature": options.temperature,
        "started_at": started_at.isoformat(),
        "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
        "response": response,
        "tool_events": events,
        "allowed_tools": sorted(ALLOWED_TOOL_NAMES),
    }
    if error is not None:
        result["error"] = error
    _write_json_atomically(options.result_file, result)
    return result


def build_swebench_registry(workspace: Path) -> ToolRegistry:
    """Build a repository-scoped coding surface without web, MCP, or memory tools."""

    raw_registry = build_default_registry(workspace)
    guarded = ToolRegistry(
        max_parallel_tools=raw_registry.max_parallel_tools,
        batch_timeout_seconds=raw_registry.batch_timeout_seconds,
    )
    for definition in raw_registry.list_tools():
        if definition.name not in ALLOWED_TOOL_NAMES:
            continue
        if definition.name == "execute_command":
            guarded.register(_safe_command_definition(definition, workspace))
            continue
        guarded.register(_repository_guarded_definition(definition, workspace))
    return guarded


def _repository_guarded_definition(
    definition: ToolDefinition,
    workspace: Path,
) -> ToolDefinition:
    def guard(arguments: dict[str, Any]) -> None:
        if definition.name not in PATH_TOOL_NAMES:
            return
        path_value = arguments.get("path", ".")
        if not isinstance(path_value, str):
            raise ValueError("path must be a string")
        _relative_workspace_path(workspace, path_value)

    def handler(**kwargs: Any) -> Any:
        guard(kwargs)
        return definition.handler(**kwargs)

    def previewer(**kwargs: Any) -> dict[str, Any]:
        guard(kwargs)
        if definition.previewer is None:
            return {}
        return definition.previewer(**kwargs)

    return ToolDefinition(
        name=definition.name,
        description=definition.description,
        parameters=definition.parameters,
        handler=handler,
        previewer=previewer if definition.previewer is not None else None,
    )


def _safe_command_definition(definition: ToolDefinition, workspace: Path) -> ToolDefinition:
    """Keep repository commands useful without exposing provider credentials."""

    def handler(command: str | list[str], timeout_seconds: int = 30) -> Any:
        _validate_benchmark_command(command)
        return _execute_command(
            workspace,
            command,
            timeout_seconds,
            environment=_benchmark_command_environment(),
        )

    return ToolDefinition(
        name=definition.name,
        description=definition.description,
        parameters=definition.parameters,
        handler=handler,
    )


def _validate_benchmark_command(command: str | list[str]) -> None:
    rendered = command if isinstance(command, str) else " ".join(str(item) for item in command)
    if any(pattern.search(rendered) for pattern in FORBIDDEN_COMMAND_PATTERNS):
        raise ToolExecutionError(
            "SWE-bench policy forbids package installation, dependency mutation, and network "
            "commands. Use only dependencies already available in the evaluation environment."
        )


def _benchmark_command_environment() -> dict[str, str]:
    sensitive_fragments = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(fragment in key.upper() for fragment in sensitive_fragments)
    }
    environment.pop("AUTHORIZATION", None)
    environment.pop("STELLARCODE_RUNTIME_PYTHONPATH", None)
    tool_pythonpath = environment.pop("STELLARCODE_TOOL_PYTHONPATH", None)
    if tool_pythonpath:
        environment["PYTHONPATH"] = tool_pythonpath
    else:
        environment.pop("PYTHONPATH", None)
    return environment


def _protect_process_secrets() -> None:
    """Prevent repository commands from reading the Agent's model key via procfs."""

    if not sys.platform.startswith("linux"):
        return
    pr_set_dumpable = 4
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(pr_set_dumpable, 0, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _temperature(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 2.0:
        raise argparse.ArgumentTypeError("temperature must be between 0 and 2")
    return parsed


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    _protect_process_secrets()
    try:
        options = SweBenchAgentOptions.parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return int(exc.code)
    result = run_swebench_agent(options)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
