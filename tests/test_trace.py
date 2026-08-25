from __future__ import annotations

import json

from stellarcode.cli import create_parser, handle_trace_command
from stellarcode.hitl import ApprovalRequest, TerminalHitlHandler
from stellarcode.llm.types import llm_runtime_scope
from stellarcode.prompt import PromptAssembler, PromptContext, PromptMode
from stellarcode.tools import ToolDefinition, ToolOutput, ToolRegistry
from stellarcode.trace import ScopedTraceRecorder, TraceRecorder, TracingChatClient


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
        config={
            "env": {"CUSTOM_NAME": "unclassified-secret"},
            "headers": {"X-Custom": "another-secret"},
        },
        image="data:image/png;base64,QUJDRA==",
        attachment={"kind": "image", "data_base64": "QUJDRA=="},
    )
    recorder.disable()

    sample = next(entry for entry in _entries(recorder) if entry["event"] == "sample")
    data = sample["data"]
    assert data["api_key"] == "[REDACTED]"
    assert data["arguments"]["api_token"] == "[REDACTED]"
    assert "hidden-token" not in data["command"]
    assert data["config"]["env"] == {"CUSTOM_NAME": "[REDACTED]"}
    assert data["config"]["headers"] == {"X-Custom": "[REDACTED]"}
    assert "IMAGE DATA OMITTED" in data["image"]
    assert data["attachment"]["data_base64"] == "[IMAGE DATA OMITTED: 8 chars]"


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


def test_trace_records_prompt_metadata_without_sensitive_prompt_content(tmp_path):
    recorder = ScopedTraceRecorder(tmp_path)
    trace_path = recorder.configure("session-one", True)
    assert trace_path is not None
    client = TracingChatClient(object(), recorder)
    secret = "memory-secret-value"
    snapshot = PromptAssembler().assemble(
        PromptMode.REACT,
        PromptContext(base_prompt="ROLE", memory_context=secret),
    ).snapshot(PromptMode.REACT)

    with llm_runtime_scope("session-one", "task-one"):
        client.record_prompt_snapshot(snapshot)

    prompt_event = next(
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "prompt_assembled"
    )
    serialized = json.dumps(prompt_event, ensure_ascii=False)
    assert secret not in serialized
    assert prompt_event["data"]["prompt"]["version"] == "stellarcode.prompt/v1"
    assert isinstance(prompt_event["data"]["prompt"]["total"]["estimated_tokens"], int)
    assert prompt_event["data"]["prompt"]["layers"][-1]["sensitive"] is True
    assert client.prompt_snapshot("session-one")["memory_hidden"] is True
    assert secret in json.dumps(
        client.prompt_snapshot("session-one", include_sensitive=True),
        ensure_ascii=False,
    )


def test_captured_trace_target_does_not_write_into_another_conversation(tmp_path):
    recorder = TraceRecorder(tmp_path)
    first_path = recorder.enable(conversation_id="first")
    first_target = recorder.capture_target()
    recorder.disable()
    second_path = recorder.enable(conversation_id="second")

    recorder.record_for(first_target, "stale_response", content="wrong conversation")
    recorder.record("current_response", content="right conversation")

    first_events = [
        json.loads(line)["event"]
        for line in first_path.read_text(encoding="utf-8").splitlines()
    ]
    second_events = [
        json.loads(line)["event"]
        for line in second_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "stale_response" not in first_events
    assert "stale_response" not in second_events
    assert "current_response" in second_events


def test_scoped_trace_keeps_background_conversations_in_separate_files(tmp_path):
    recorder = ScopedTraceRecorder(tmp_path)
    first_path = recorder.configure("session-one", True, conversation_id="session-one")
    second_path = recorder.configure("session-two", True, conversation_id="session-two")
    assert first_path is not None
    assert second_path is not None

    recorder.select("session-two")
    with llm_runtime_scope("session-one", "task-one"):
        target = recorder.capture_target()
        recorder.record("tool_started", task_id="task-one")
    recorder.record("selected_event", task_id="task-two")
    recorder.record_for(target, "tool_finished", task_id="task-one")

    first_events = [
        json.loads(line)["event"]
        for line in first_path.read_text(encoding="utf-8").splitlines()
    ]
    second_events = [
        json.loads(line)["event"]
        for line in second_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "tool_started" in first_events
    assert "tool_finished" in first_events
    assert "selected_event" not in first_events
    assert "selected_event" in second_events


def test_scoped_trace_accepts_session_id_in_runtime_event_payload(tmp_path):
    recorder = ScopedTraceRecorder(tmp_path)
    trace_path = recorder.configure(
        "session-one",
        True,
        conversation_id="session-one",
    )
    assert trace_path is not None

    recorder.record_for_session(
        "session-one",
        "runtime_event",
        event_type="task.started",
        session_id="session-one",
        task_id="task-one",
    )

    runtime_event = next(
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "runtime_event"
    )
    assert runtime_event["data"]["session_id"] == "session-one"


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
