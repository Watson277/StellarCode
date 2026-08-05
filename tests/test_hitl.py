from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path

from rich.console import Console

from stellarcode.agent import Agent
from stellarcode.command_policy import is_safe_read_only_command
from stellarcode.hitl import (
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalResult,
    Decision,
    TerminalHitlHandler,
)
from stellarcode.tools import ToolDefinition, build_default_registry


class StubHitlHandler:
    def __init__(
        self,
        result: ApprovalResult,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.result = result
        self.requests: list[ApprovalRequest] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def clear_approved_all(self) -> None:
        pass

    def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        self.requests.append(request)
        return self.result


def test_hitl_and_tools_import_in_a_fresh_process_without_cycles():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from stellarcode.hitl import TerminalHitlHandler; "
                "from stellarcode.tools import build_default_registry; "
                "assert TerminalHitlHandler and build_default_registry"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_approval_policy_uses_static_tool_risk_levels():
    assert ApprovalPolicy.requires_approval("write_file")
    assert ApprovalPolicy.requires_approval("delete_file")
    assert ApprovalPolicy.requires_approval("execute_command")
    assert ApprovalPolicy.requires_approval("create_project")
    assert not ApprovalPolicy.requires_approval("web_search")
    assert not ApprovalPolicy.requires_approval("web_fetch")
    assert not ApprovalPolicy.requires_approval("read_file")
    assert not ApprovalPolicy.requires_approval("search_code")
    assert not ApprovalPolicy.requires_approval(
        "mcp__chrome-devtools__navigate_page"
    )
    assert ApprovalPolicy.requires_approval("mcp__filesystem__write_file")
    assert ApprovalPolicy.danger_level("execute_command") == "high"
    assert ApprovalPolicy.danger_level("delete_file") == "high"
    assert ApprovalPolicy.danger_level("write_file") == "medium"
    assert ApprovalPolicy.danger_level("web_search") == "safe"
    assert ApprovalPolicy.danger_level("web_fetch") == "safe"
    assert ApprovalPolicy.danger_level("read_file") == "safe"
    assert (
        ApprovalPolicy.danger_level("mcp__chrome-devtools__take_snapshot")
        == "safe"
    )


def test_read_only_environment_commands_are_safe_without_approval():
    commands = [
        "conda env list",
        "conda info --json",
        "conda run -n llm1 python --version",
        "conda run --name llm1 pip list",
        "python --version",
        "where.exe python",
        "nvidia-smi",
        (
            "Get-Command conda -ErrorAction SilentlyContinue | Select-Object Source; "
            "Get-Command python -ErrorAction SilentlyContinue | Select-Object Source"
        ),
    ]

    for command in commands:
        arguments = {"command": command}
        assert is_safe_read_only_command(command), command
        assert not ApprovalPolicy.requires_approval("execute_command", arguments)
        assert ApprovalPolicy.danger_level("execute_command", arguments) == "safe"


def test_commands_with_code_execution_or_side_effects_stay_high_risk():
    commands = [
        'python -c "print(1)"',
        "conda run -n llm1 python -c \"import torch\"",
        "pip install torch",
        "conda install pytorch",
        "Get-Command python; Remove-Item data.txt",
        "python --version > version.txt",
        "Get-Command $(Remove-Item data.txt)",
    ]

    for command in commands:
        arguments = {"command": command}
        assert not is_safe_read_only_command(command), command
        assert ApprovalPolicy.requires_approval("execute_command", arguments)
        assert ApprovalPolicy.danger_level("execute_command", arguments) == "high"


def test_restricted_registry_bypasses_hitl_for_safe_version_check(tmp_path):
    handler = StubHitlHandler(ApprovalResult.rejected("should not be requested"))
    registry = build_default_registry(tmp_path, hitl_handler=handler)

    result = registry.execute("execute_command", {"command": "python --version"})

    assert "exit_code: 0" in result
    assert "Python" in result
    assert handler.requests == []


def test_chrome_devtools_mcp_tools_bypass_hitl_in_restricted_mode(tmp_path):
    handler = StubHitlHandler(ApprovalResult.rejected("should not be requested"))
    registry = build_default_registry(tmp_path, hitl_handler=handler)
    registry.register(
        ToolDefinition(
            name="mcp__chrome-devtools__take_snapshot",
            description="Read the current browser page.",
            parameters={"type": "object"},
            handler=lambda: "snapshot",
        )
    )

    assert registry.execute("mcp__chrome-devtools__take_snapshot", {}) == "snapshot"
    assert handler.requests == []


def test_approval_request_formats_risk_and_truncates_long_arguments():
    request = ApprovalRequest.create(
        "write_file",
        '{"path":"notes.txt","content":"' + ("x" * 180) + '"}',
        caller_context="worker-1",
    )

    display = request.to_display_text()

    assert "工具: write_file" in display
    assert "等级: 🟡 中危" in display
    assert "worker-1" in display
    assert "字符）" in display
    assert len(display) < 600


def test_approval_request_renders_as_a_structured_terminal_panel():
    from stellarcode.hitl.render import build_approval_panel

    request = ApprovalRequest.create(
        "write_file",
        json.dumps(
            {
                "path": "/Users/itwanger/project/config.json",
                "content": '{"version":"2.0","data":"' + ("x" * 180) + '"}',
            },
            ensure_ascii=False,
        ),
    )
    output = StringIO()
    console = Console(file=output, width=72, force_terminal=False, color_system=None)

    console.print(build_approval_panel(request))
    rendered = output.getvalue()

    assert "需要审批" in rendered
    assert "工具:" in rendered and "write_file" in rendered
    assert "等级:" in rendered and "中危" in rendered
    assert "风险:" in rendered and "写入或覆盖" in rendered
    assert "path:" in rendered and "config.json" in rendered
    assert "content:" in rendered and "字符" in rendered
    assert "┌" in rendered and "└" in rendered


def test_approval_result_selects_modified_arguments():
    original = '{"path":"old.txt"}'
    modified = '{"path":"new.txt"}'

    assert ApprovalResult.approved().effective_arguments(original) == original
    assert ApprovalResult.modified(modified).effective_arguments(original) == modified
    assert ApprovalResult.rejected("no").is_rejected
    assert ApprovalResult.skipped().is_skipped


def test_terminal_handler_supports_approve_all_and_clear():
    answers = iter(["a", "n", "keep commands gated"])
    output: list[str] = []
    handler = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: next(answers),
        output_func=output.append,
    )
    request = ApprovalRequest.create("write_file", '{"path":"one.txt"}')

    first = handler.request_approval(request)
    second = handler.request_approval(request)
    command = handler.request_approval(
        ApprovalRequest.create("execute_command", '{"command":["python","--version"]}')
    )

    assert first.decision == Decision.APPROVED_ALL
    assert second.decision == Decision.APPROVED
    assert command.decision == Decision.REJECTED
    assert command.reason == "keep commands gated"
    assert handler.approved_all_tools == ("write_file",)
    assert any("本会话已持续允许" in line for line in output)

    handler.clear_approved_all()
    assert handler.approved_all_tools == ()


def test_terminal_handler_can_approve_an_entire_mcp_server_for_session():
    answers = iter(["a", "s"])
    handler = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: next(answers),
        output_func=lambda _message: None,
    )

    first = handler.request_approval(
        ApprovalRequest.create("mcp__chrome-devtools__navigate_page", "{}")
    )
    second = handler.request_approval(
        ApprovalRequest.create("mcp__chrome-devtools__take_snapshot", "{}")
    )

    assert first.decision == Decision.APPROVED_ALL
    assert second.decision == Decision.APPROVED
    assert handler.approved_all_servers == ("chrome-devtools",)
    assert handler.approved_all_tools == ()

    handler.clear_approved_all_for_server("chrome-devtools")
    assert handler.approved_all_servers == ()


def test_terminal_handler_defaults_mcp_approve_all_scope_to_current_tool():
    answers = iter(["a", ""])
    handler = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: next(answers),
        output_func=lambda _message: None,
    )

    handler.request_approval(
        ApprovalRequest.create("mcp__chrome-devtools__navigate_page", "{}")
    )

    assert handler.approved_all_tools == ("mcp__chrome-devtools__navigate_page",)
    assert handler.approved_all_servers == ()


def test_terminal_handler_supports_reject_skip_modify_and_fail_closed():
    reject_answers = iter(["n", "wrong target"])
    reject = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: next(reject_answers),
        output_func=lambda _message: None,
    ).request_approval(ApprovalRequest.create("write_file", "{}"))
    assert reject.decision == Decision.REJECTED
    assert reject.reason == "wrong target"

    skip = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: "s",
        output_func=lambda _message: None,
    ).request_approval(ApprovalRequest.create("write_file", "{}"))
    assert skip.decision == Decision.SKIPPED

    modify_answers = iter(["m", '{"path":"safe.txt","content":"ok"}'])
    modified = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: next(modify_answers),
        output_func=lambda _message: None,
    ).request_approval(ApprovalRequest.create("write_file", "{}"))
    assert modified.decision == Decision.MODIFIED
    assert modified.modified_arguments == '{"path":"safe.txt","content":"ok"}'

    invalid_answers = iter(["?", "?", "?", "?", "?"])
    failed = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: next(invalid_answers),
        output_func=lambda _message: None,
    ).request_approval(ApprovalRequest.create("write_file", "{}"))
    assert failed.decision == Decision.REJECTED
    assert "Too many" in str(failed.reason)


def test_hitl_registry_bypasses_safe_tools_and_intercepts_dangerous_tools(
    tmp_path: Path,
):
    source = tmp_path / "source.txt"
    source.write_text("hello", encoding="utf-8")
    handler = StubHitlHandler(ApprovalResult.approved())
    registry = build_default_registry(tmp_path, hitl_handler=handler)

    assert registry.execute("read_file", {"path": "source.txt"}) == "hello"
    assert handler.requests == []

    result = registry.execute(
        "write_file",
        {"path": "approved.txt", "content": "approved"},
    )

    assert "Wrote" in result
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "approved"
    assert handler.requests[0].tool_name == "write_file"


def test_hitl_registry_returns_rejection_to_agent_without_executing(tmp_path: Path):
    handler = StubHitlHandler(ApprovalResult.rejected("Use docs/output.txt instead."))
    registry = build_default_registry(tmp_path, hitl_handler=handler)

    result = registry.execute(
        "write_file",
        {"path": "blocked.txt", "content": "should not exist"},
    )

    assert result == "[HITL] Operation rejected: Use docs/output.txt instead."
    assert not (tmp_path / "blocked.txt").exists()


def test_rejection_reason_is_returned_to_agent_for_replanning(tmp_path: Path):
    class RejectAwareClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, tools=None, temperature=0.2):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {"path": "blocked.txt", "content": "no"}
                                ),
                            },
                        }
                    ],
                }
            assert messages[-1]["role"] == "tool"
            assert "Use docs/output.txt instead" in messages[-1]["content"]
            return {"role": "assistant", "content": "I will use the requested path."}

    handler = StubHitlHandler(ApprovalResult.rejected("Use docs/output.txt instead."))
    agent = Agent(
        RejectAwareClient(),
        build_default_registry(tmp_path, hitl_handler=handler),
    )

    answer = agent.run("write a file")

    assert answer == "I will use the requested path."
    assert not (tmp_path / "blocked.txt").exists()


def test_hitl_registry_executes_modified_arguments(tmp_path: Path):
    handler = StubHitlHandler(
        ApprovalResult.modified('{"path":"changed.txt","content":"updated"}')
    )
    registry = build_default_registry(tmp_path, hitl_handler=handler)

    registry.execute(
        "write_file",
        {"path": "original.txt", "content": "original"},
    )

    assert not (tmp_path / "original.txt").exists()
    assert (tmp_path / "changed.txt").read_text(encoding="utf-8") == "updated"


def test_disabled_hitl_registry_has_zero_approval_prompts(tmp_path: Path):
    handler = StubHitlHandler(ApprovalResult.rejected("should not be used"), enabled=False)
    registry = build_default_registry(tmp_path, hitl_handler=handler)

    registry.execute("write_file", {"path": "direct.txt", "content": "ok"})

    assert (tmp_path / "direct.txt").read_text(encoding="utf-8") == "ok"
    assert handler.requests == []


def test_approval_allows_writing_outside_working_directory(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    handler = StubHitlHandler(ApprovalResult.approved())
    registry = build_default_registry(workspace, hitl_handler=handler)

    result = registry.execute(
        "write_file",
        {"path": str(target), "content": "approved"},
    )

    assert "Wrote" in result
    assert target.read_text(encoding="utf-8") == "approved"
    assert handler.requests[0].tool_name == "write_file"


def test_terminal_handler_serializes_concurrent_worker_prompts():
    state_lock = threading.Lock()
    active_inputs = 0
    maximum_active_inputs = 0

    def approve_after_delay(_prompt: str) -> str:
        nonlocal active_inputs, maximum_active_inputs
        with state_lock:
            active_inputs += 1
            maximum_active_inputs = max(maximum_active_inputs, active_inputs)
        time.sleep(0.02)
        with state_lock:
            active_inputs -= 1
        return "y"

    handler = TerminalHitlHandler(
        enabled=True,
        input_func=approve_after_delay,
        output_func=lambda _message: None,
    )
    requests = [
        ApprovalRequest.create("write_file", f'{{"path":"{index}.txt"}}')
        for index in range(4)
    ]

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(handler.request_approval, requests))

    assert all(result.decision == Decision.APPROVED for result in results)
    assert maximum_active_inputs == 1
