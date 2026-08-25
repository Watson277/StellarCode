"""Non-interactive adapter for repository coding benchmarks.

The benchmark runner owns the private test suite.  This module only gives an
Agent a cleaned workspace, a task prompt, and a small, workspace-scoped tool
surface.  It writes a machine-readable result for the runner to combine with
test outcomes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from stellarcode.agent import Agent
from stellarcode.llm import create_chat_client
from stellarcode.tools import build_default_registry
from stellarcode.tools.registry import (
    ToolDefinition,
    ToolExecutionError,
    ToolRegistry,
)


ALLOWED_TOOL_NAMES = frozenset(
    {"read_file", "write_file", "apply_patch", "list_dir", "glob_files", "grep_code"}
)
WRITABLE_TOOL_NAMES = frozenset({"write_file", "apply_patch"})
BENCHMARK_SYSTEM_PROMPT = """You are StellarCode running a repository coding benchmark.

Implement the supplied task in the current workspace. You may inspect files and edit only
the declared editable files. The workspace deliberately does not contain tests or reference
solutions, and no network, shell, browser, MCP, memory, or project-generation tools are
available. Do not claim that an edit succeeded until its tool result confirms it. When the
implementation is complete, give a concise final summary."""


@dataclass(frozen=True)
class BenchmarkOptions:
    workspace: Path
    prompt_file: Path
    editable_files: tuple[str, ...]
    result_file: Path
    max_iterations: int

    @classmethod
    def parse(cls, argv: list[str]) -> "BenchmarkOptions":
        parser = argparse.ArgumentParser(
            prog="stellarcode benchmark-agent",
            description="Run StellarCode once in a clean benchmark workspace.",
        )
        parser.add_argument("--workspace", required=True, type=Path)
        parser.add_argument("--prompt-file", required=True, type=Path)
        parser.add_argument(
            "--editable-file",
            action="append",
            default=[],
            help="Workspace-relative source file the Agent may modify; repeat for each file.",
        )
        parser.add_argument("--result-file", required=True, type=Path)
        parser.add_argument("--max-iterations", type=_positive_int, default=30)
        args = parser.parse_args(argv)
        if not args.editable_file:
            parser.error("at least one --editable-file is required")

        workspace = args.workspace.resolve()
        if not workspace.is_dir():
            parser.error(f"--workspace is not a directory: {workspace}")
        prompt_file = args.prompt_file.resolve()
        if not _is_within(workspace, prompt_file) or not prompt_file.is_file():
            parser.error("--prompt-file must be an existing file inside --workspace")

        try:
            editable_files = tuple(_normalise_relative_path(value) for value in args.editable_file)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
        if len(set(editable_files)) != len(editable_files):
            parser.error("--editable-file values must not repeat")
        for relative_path in editable_files:
            if not (workspace / relative_path).is_file():
                parser.error(f"editable file does not exist: {relative_path}")
        return cls(
            workspace=workspace,
            prompt_file=prompt_file,
            editable_files=editable_files,
            result_file=args.result_file.resolve(),
            max_iterations=args.max_iterations,
        )


@dataclass(frozen=True)
class ChangedFile:
    path: str
    before_sha256: str
    after_sha256: str | None


def run_benchmark_agent(
    options: BenchmarkOptions,
    *,
    client_factory: Callable[[], Any] = create_chat_client,
) -> dict[str, Any]:
    """Run one benchmark case and return the result persisted for its runner."""

    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    before = _snapshot_files(options.workspace, options.editable_files)
    events: list[dict[str, Any]] = []
    result: dict[str, Any]
    try:
        prompt = options.prompt_file.read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError(f"prompt file is empty: {options.prompt_file}")
        registry = build_benchmark_registry(options.workspace, options.editable_files)
        agent = Agent(
            llm_client=client_factory(),
            tool_registry=registry,
            system_prompt=BENCHMARK_SYSTEM_PROMPT,
            max_iterations=options.max_iterations,
            workspace=options.workspace,
            stream_output=False,
            progress_callback=lambda _message: None,
            event_callback=lambda event, data: events.append({"event": event, "data": data}),
        )
        response = agent.run(prompt)
        status = "completed"
        termination = "agent_finished"
        error = None
    except Exception as exc:  # result JSON is required even for startup/provider failures
        response = ""
        status = "error"
        termination = "agent_error"
        error = f"{type(exc).__name__}: {exc}"

    result = {
        "status": status,
        "termination": termination,
        "started_at": started_at.isoformat(),
        "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
        "response": response,
        "changed_files": [asdict(item) for item in _changed_files(options.workspace, before)],
        "tool_events": events,
        "allowed_tools": sorted(ALLOWED_TOOL_NAMES),
    }
    if error is not None:
        result["error"] = error
    _write_json_atomically(options.result_file, result)
    return result


def build_benchmark_registry(workspace: Path, editable_files: tuple[str, ...]) -> ToolRegistry:
    """Return a default registry filtered and guarded for one benchmark case."""

    raw_registry = build_default_registry(workspace)
    guarded = ToolRegistry(
        max_parallel_tools=raw_registry.max_parallel_tools,
        batch_timeout_seconds=raw_registry.batch_timeout_seconds,
    )
    editable = frozenset(editable_files)
    for definition in raw_registry.list_tools():
        if definition.name not in ALLOWED_TOOL_NAMES:
            continue
        guarded.register(_guarded_definition(definition, workspace, editable))
    return guarded


def _guarded_definition(
    definition: ToolDefinition,
    workspace: Path,
    editable_files: frozenset[str],
) -> ToolDefinition:
    def guard(arguments: dict[str, Any]) -> None:
        path_value = arguments.get("path", ".")
        if not isinstance(path_value, str):
            raise ToolExecutionError("path must be a string")
        relative_path = _relative_workspace_path(workspace, path_value)
        if definition.name in WRITABLE_TOOL_NAMES and relative_path not in editable_files:
            raise ToolExecutionError(
                "Benchmark policy permits edits only to declared editable files: "
                + ", ".join(sorted(editable_files))
            )

    def handler(**kwargs: Any) -> str:
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


def _snapshot_files(workspace: Path, editable_files: tuple[str, ...]) -> dict[str, str]:
    return {relative_path: _sha256(workspace / relative_path) for relative_path in editable_files}


def _changed_files(workspace: Path, before: dict[str, str]) -> list[ChangedFile]:
    changed: list[ChangedFile] = []
    for relative_path, before_sha256 in before.items():
        target = workspace / relative_path
        after_sha256 = _sha256(target) if target.is_file() else None
        if after_sha256 != before_sha256:
            changed.append(ChangedFile(relative_path, before_sha256, after_sha256))
    return changed


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalise_relative_path(value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise argparse.ArgumentTypeError("editable files must be relative paths inside --workspace")
    return candidate.as_posix()


def _relative_workspace_path(workspace: Path, user_path: str) -> str:
    candidate = Path(user_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if not _is_within(workspace, resolved):
        raise ToolExecutionError("Benchmark policy forbids paths outside the workspace")
    return resolved.relative_to(workspace).as_posix()


def _is_within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
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
    """CLI entry point; returns a process exit code for testability."""

    load_dotenv()
    try:
        options = BenchmarkOptions.parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return int(exc.code)
    result = run_benchmark_agent(options)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
