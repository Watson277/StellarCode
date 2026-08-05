from stellarcode.cli import create_parser, switch_access_mode
from stellarcode.hitl import ApprovalRequest, TerminalHitlHandler
from stellarcode.tools import build_default_registry


def test_cli_defaults_to_restricted_mode():
    args = create_parser().parse_args([])

    assert args.mode == "restricted"


def test_cli_supports_explicit_full_access_mode():
    args = create_parser().parse_args(
        [
            "--mode",
            "full-access",
            "--plan-workers",
            "3",
            "--max-parallel-tools",
            "2",
            "--tool-batch-timeout",
            "45",
        ]
    )

    assert args.mode == "full-access"
    assert args.plan_workers == 3
    assert args.max_parallel_tools == 2
    assert args.tool_batch_timeout == 45


def test_runtime_switch_to_full_access_requires_exact_confirmation():
    handler = TerminalHitlHandler(enabled=True)

    unchanged_mode, cancelled = switch_access_mode(
        "restricted",
        "full-access",
        handler,
        confirmation_func=lambda _prompt: "yes",
    )
    changed_mode, switched = switch_access_mode(
        "restricted",
        "full-access",
        handler,
        confirmation_func=lambda _prompt: "FULL ACCESS",
    )

    assert unchanged_mode == "restricted"
    assert "cancelled" in cancelled
    assert changed_mode == "full-access"
    assert "switched" in switched
    assert not handler.is_enabled()


def test_runtime_switch_to_restricted_is_immediate_and_clears_approvals():
    handler = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: "a",
        output_func=lambda _message: None,
    )
    handler.request_approval(ApprovalRequest.create("write_file", "{}"))
    handler.set_enabled(False)

    mode, message = switch_access_mode(
        "full-access",
        "restricted",
        handler,
    )

    assert mode == "restricted"
    assert "switched" in message
    assert handler.is_enabled()
    assert handler.approved_all_tools == ()


def test_shared_registry_observes_runtime_mode_changes(tmp_path):
    answers = iter(["n", "not now"])
    handler = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: next(answers),
        output_func=lambda _message: None,
    )
    registry = build_default_registry(tmp_path, hitl_handler=handler)

    rejected = registry.execute(
        "write_file",
        {"path": "runtime.txt", "content": "blocked"},
    )
    switch_access_mode(
        "restricted",
        "full-access",
        handler,
        confirmation_func=lambda _prompt: "FULL ACCESS",
    )
    executed = registry.execute(
        "write_file",
        {"path": "runtime.txt", "content": "allowed"},
    )

    assert "Operation rejected" in rejected
    assert "Wrote" in executed
    assert (tmp_path / "runtime.txt").read_text(encoding="utf-8") == "allowed"
