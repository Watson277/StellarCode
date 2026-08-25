"""Reliability tests for the durable Team-mode JSONL MessageBus."""

from __future__ import annotations

import json

from stellarcode.multi_agent.message_bus import FileMessageBus


def test_message_survives_restart_and_is_not_removed_after_ack(tmp_path):
    root = tmp_path / "team-run"
    producer = FileMessageBus(root)
    sent = producer.send(
        sender="lead",
        recipient="worker-1",
        kind="task",
        payload={"instruction": "inspect the backend"},
        task_id="task-1",
    )

    consumer = FileMessageBus(root)
    claimed = consumer.claim_next("worker-1", consumer_id="worker-1")

    assert claimed is not None
    assert claimed.message == sent
    consumer.acknowledge(claimed)

    restarted = FileMessageBus(root)
    assert restarted.claim_next("worker-1", consumer_id="worker-1") is None
    assert restarted.mailbox_messages("worker-1") == [sent]
    assert '"id"' in (root / "mailboxes" / "worker-1.jsonl").read_text(encoding="utf-8")


def test_released_message_is_redelivered_with_an_incremented_attempt(tmp_path):
    bus = FileMessageBus(tmp_path / "team-run")
    bus.send(
        sender="lead",
        recipient="worker-1",
        kind="task",
        payload={"instruction": "retry me"},
    )

    first = bus.claim_next("worker-1", consumer_id="worker-1")
    assert first is not None
    assert first.attempt == 1
    bus.release(first)

    second = bus.claim_next("worker-1", consumer_id="worker-1")
    assert second is not None
    assert second.message.id == first.message.id
    assert second.attempt == 2


def test_exhausted_message_moves_to_dead_letter_without_deleting_source_mailbox(tmp_path):
    root = tmp_path / "team-run"
    bus = FileMessageBus(root, max_attempts=2)
    sent = bus.send(
        sender="lead",
        recipient="worker-1",
        kind="task",
        payload={"instruction": "always fail"},
    )

    first = bus.claim_next("worker-1", consumer_id="worker-1")
    assert first is not None
    bus.release(first)
    second = bus.claim_next("worker-1", consumer_id="worker-1")
    assert second is not None
    bus.release(second)

    assert bus.claim_next("worker-1", consumer_id="worker-1") is None
    dead_letter = json.loads(root.joinpath("dead-letter.jsonl").read_text(encoding="utf-8"))
    assert dead_letter["message"]["id"] == sent.id
    assert dead_letter["attempts"] == 2
    assert bus.mailbox_messages("worker-1") == [sent]


def test_new_send_repairs_a_crash_torn_final_jsonl_line(tmp_path):
    root = tmp_path / "team-run"
    mailbox = root / "mailboxes" / "worker-1.jsonl"
    mailbox.parent.mkdir(parents=True)
    mailbox.write_bytes(b'{"id":"torn"')
    bus = FileMessageBus(root)

    sent = bus.send(
        sender="lead",
        recipient="worker-1",
        kind="task",
        payload={"instruction": "safe after restart"},
    )

    assert bus.mailbox_messages("worker-1") == [sent]
    assert mailbox.read_bytes().startswith(b'{"id":"msg-')


def test_state_is_an_append_only_jsonl_event_log_without_temporary_files(tmp_path):
    bus = FileMessageBus(tmp_path / "team-run")
    bus.send(
        sender="lead",
        recipient="planner",
        kind="task",
        payload={"instruction": "plan"},
    )

    claimed = bus.claim_next("planner", consumer_id="planner")
    assert claimed is not None
    bus.acknowledge(claimed)

    state_path = bus.root / "state" / "planner.jsonl"
    events = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()]
    assert [event["op"] for event in events] == ["claim", "ack"]
    assert not list(state_path.parent.glob("*.tmp"))
    assert FileMessageBus(bus.root).pending_count("planner") == 0
