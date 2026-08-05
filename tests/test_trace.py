from __future__ import annotations

import json

from stellarcode.cli import create_parser, handle_trace_command
from stellarcode.hitl import ApprovalRequest, TerminalHitlHandler
from stellarcode.tools import ToolDefinition, ToolOutput, ToolRegistry
from stellarcode.trace import TraceRecorder, TracingChatClient


def _entries(recorder: TraceRecorder) -> list[dict[str, object]]:
    assert recorder.path is not None
    return [
        json.loads(line)
        for line in recorder.path.read_text(encoding="utf-8").splitlines()
    ]


def test_trace_recorder_is_opt_in_and_redacts_secrets(tmp_path):
    recorder = TraceRecorder(tmp_path)
    recorder.record("ignored", value="not written")
    assert list(tmp_path.glob("*.jsonl")) == []

    recorder.enable(workspace=tmp_path)
    recorder.record(
        "sample",
        api_key="secret-key",
        arguments='{"city":"北京","api_token":"secret-token"}',
        command="curl -H 'Authorization: Bearer hidden-token'",
        image="data:image/png;base64,QUJDRA==",
    )
    recorder.disable()

    sample = next(entry for entry in _entries(recorder) if entry["event"] == "sample")
    data = sample["data"]
    assert data["api_key"] == "[REDACTED]"
    assert data["arguments"]["api_token"] == "[REDACTED]"
    assert "hidden-token" not in data["command"]
    assert "IMAGE DATA OMITTED" in data["image"]


def test_tracing_chat_client_records_request_response_and_error(tmp_path):
    class FakeClient:
        model = "fake-model"

        def chat(self, messages, tools=None, temperature=0.2):
            return {"role": "assistant", "content": "done"}

    recorder = TraceRecorder(tmp_path)
    recorder.enable()
    client = TracingChatClient(FakeClient(), recorder)

    response = client.chat([{"role": "user", "content": "hello"}], tools=[])

    assert response["content"] == "done"
    events = [entry["event"] for entry in _entries(recorder)]
    assert "llm_request" in events
    assert "llm_response" in events


def test_tool_trace_keeps_untruncated_result(tmp_path):
    recorder = TraceRecorder(tmp_path)
    recorder.enable()
    registry = ToolRegistry(trace_recorder=recorder)
    full_result = "x" * 12000
    registry.register(
        ToolDefinition(
            name="large_output",
            description="test",
            parameters={"type": "object", "properties": {}},
            handler=lambda: ToolOutput("x" * 100, trace_text=full_result),
        )
    )

    visible = registry.execute("large_output", {})

    assert len(visible) == 100
    event = next(entry for entry in _entries(recorder) if entry["event"] == "tool_result")
    assert event["data"]["result"] == full_result


def test_trace_command_can_toggle_runtime_recording(tmp_path):
    recorder = TraceRecorder(tmp_path)

    enabled = handle_trace_command("/trace on", recorder, workspace=tmp_path)
    status = handle_trace_command("/trace", recorder, workspace=tmp_path)
    disabled = handle_trace_command("/trace off", recorder, workspace=tmp_path)

    assert "enabled" in enabled
    assert "trace mode: on" in status
    assert "disabled" in disabled
    assert not recorder.enabled


def test_trace_records_hitl_decision(tmp_path):
    recorder = TraceRecorder(tmp_path)
    recorder.enable()
    handler = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: "n",
        output_func=lambda _message: None,
        trace_recorder=recorder,
    )

    decision = handler.request_approval(ApprovalRequest.create("write_file", "{}"))

    assert decision.is_rejected
    events = _entries(recorder)
    assert any(entry["event"] == "approval_request" for entry in events)
    assert any(
        entry["event"] == "approval_decision"
        and entry["data"]["decision"] == "rejected"
        for entry in events
    )


def test_cli_accepts_trace_options(tmp_path):
    args = create_parser().parse_args(["--trace", "--trace-dir", str(tmp_path)])

    assert args.trace
    assert args.trace_dir == str(tmp_path)
