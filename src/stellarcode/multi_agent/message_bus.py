"""Durable JSONL mailboxes used by Team-mode agents.

The bus deliberately keeps mailbox files append-only.  A consumer receives a
short lease and must acknowledge the message after it has durably produced its
reply.  This avoids the unsafe "read then delete" pattern: a sidecar crash can
leave a message eligible for another consumer instead of silently losing work.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MessageBusError(RuntimeError):
    """Raised when a mailbox operation cannot preserve its delivery contract."""


@dataclass(frozen=True)
class BusMessage:
    """One immutable message stored as a single JSONL line in a recipient inbox."""

    id: str
    sender: str
    recipient: str
    kind: str
    payload: dict[str, Any]
    task_id: str | None
    correlation_id: str
    parent_message_id: str | None
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "kind": self.kind,
            "payload": self.payload,
            "task_id": self.task_id,
            "correlation_id": self.correlation_id,
            "parent_message_id": self.parent_message_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BusMessage":
        try:
            message_id = str(raw["id"])
            sender = str(raw["sender"])
            recipient = str(raw["recipient"])
            kind = str(raw["kind"])
            correlation_id = str(raw["correlation_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MessageBusError("Mailbox entry is missing required fields.") from exc
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise MessageBusError("Mailbox entry payload must be a JSON object.")
        task_id = raw.get("task_id")
        parent_message_id = raw.get("parent_message_id")
        return cls(
            id=message_id,
            sender=sender,
            recipient=recipient,
            kind=kind,
            payload=payload,
            task_id=str(task_id) if task_id is not None else None,
            correlation_id=correlation_id,
            parent_message_id=(str(parent_message_id) if parent_message_id else None),
            created_at=float(raw.get("created_at") or 0),
        )


@dataclass(frozen=True)
class ClaimedMessage:
    """A mailbox message plus the opaque lease needed for ack/nack."""

    message: BusMessage
    consumer_id: str
    lease_token: str
    attempt: int
    lease_expires_at: float


class _MailboxFileLock:
    """Small cross-process lock shared by mailbox appends and acknowledgement state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: Any | None = None

    def __enter__(self) -> "_MailboxFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0, os.SEEK_END)
                if self._stream.tell() == 0:
                    self._stream.write(b"0")
                    self._stream.flush()
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._stream.close()
            self._stream = None
            raise
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self._stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


class FileMessageBus:
    """A per-team-run MessageBus backed by durable append-only JSONL mailboxes.

    The class is safe for multiple Runtime processes sharing one run directory.
    It provides at-least-once delivery; consumers must make task side effects
    idempotent using ``message.id``/``correlation_id`` where that matters.
    """

    STATE_VERSION = 1

    def __init__(
        self,
        root: str | Path,
        *,
        default_lease_seconds: float = 120.0,
        max_attempts: int = 3,
    ) -> None:
        if default_lease_seconds <= 0:
            raise ValueError("default_lease_seconds must be positive.")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        self.root = Path(root).resolve()
        self.mailbox_dir = self.root / "mailboxes"
        self.state_dir = self.root / "state"
        self.dead_letter_path = self.root / "dead-letter.jsonl"
        self.lock_path = self.root / "message-bus.lock"
        self.default_lease_seconds = default_lease_seconds
        self.max_attempts = max_attempts
        self._thread_lock = threading.RLock()
        self.mailbox_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        kind: str,
        payload: dict[str, Any],
        task_id: str | None = None,
        correlation_id: str | None = None,
        parent_message_id: str | None = None,
    ) -> BusMessage:
        """Append a message and fsync it before making it observable to consumers."""
        sender = _validate_actor_name(sender)
        recipient = _validate_actor_name(recipient)
        if not kind or not kind.strip():
            raise ValueError("Message kind cannot be empty.")
        if not isinstance(payload, dict):
            raise TypeError("Message payload must be a dictionary.")
        message = BusMessage(
            id=f"msg-{uuid.uuid4().hex}",
            sender=sender,
            recipient=recipient,
            kind=kind.strip(),
            payload=payload,
            task_id=str(task_id) if task_id else None,
            correlation_id=correlation_id or f"corr-{uuid.uuid4().hex}",
            parent_message_id=parent_message_id,
            created_at=time.time(),
        )
        with self._locked():
            _append_jsonl(self._mailbox_path(recipient), message.to_dict())
        return message

    def claim_next(
        self,
        recipient: str,
        *,
        consumer_id: str,
        lease_seconds: float | None = None,
    ) -> ClaimedMessage | None:
        """Claim the oldest available recipient message without deleting its log line."""
        return self._claim(
            recipient,
            consumer_id=consumer_id,
            lease_seconds=lease_seconds,
            predicate=None,
        )

    def claim_matching(
        self,
        recipient: str,
        *,
        consumer_id: str,
        predicate: Callable[[BusMessage], bool],
        lease_seconds: float | None = None,
    ) -> ClaimedMessage | None:
        """Claim the oldest available message selected by a non-mutating predicate."""
        return self._claim(
            recipient,
            consumer_id=consumer_id,
            lease_seconds=lease_seconds,
            predicate=predicate,
        )

    def _claim(
        self,
        recipient: str,
        *,
        consumer_id: str,
        lease_seconds: float | None,
        predicate: Callable[[BusMessage], bool] | None,
    ) -> ClaimedMessage | None:
        recipient = _validate_actor_name(recipient)
        consumer_id = _validate_actor_name(consumer_id)
        effective_lease = lease_seconds or self.default_lease_seconds
        if effective_lease <= 0:
            raise ValueError("lease_seconds must be positive.")
        with self._locked():
            state = self._load_state(recipient)
            now = time.time()
            self._release_expired_leases(state, now)
            self._move_exhausted_to_dead_letter(recipient, state)
            for message in self._read_mailbox(recipient):
                if message.id in state["acknowledged"] or message.id in state["leases"]:
                    continue
                if predicate is not None and not predicate(message):
                    continue
                attempt = int(state["attempts"].get(message.id, 0)) + 1
                token = uuid.uuid4().hex
                expires_at = now + effective_lease
                state["attempts"][message.id] = attempt
                state["leases"][message.id] = {
                    "consumer_id": consumer_id,
                    "token": token,
                    "expires_at": expires_at,
                }
                self._append_state_event(
                    recipient,
                    {
                        "op": "claim",
                        "message_id": message.id,
                        "consumer_id": consumer_id,
                        "token": token,
                        "expires_at": expires_at,
                        "attempt": attempt,
                    },
                )
                return ClaimedMessage(message, consumer_id, token, attempt, expires_at)
        return None

    def acknowledge(self, claimed: ClaimedMessage) -> None:
        """Durably confirm a consumed message after the consumer produced its reply."""
        with self._locked():
            state = self._load_state(claimed.message.recipient)
            lease = state["leases"].get(claimed.message.id)
            self._assert_lease(claimed, lease)
            state["leases"].pop(claimed.message.id, None)
            state["acknowledged"][claimed.message.id] = time.time()
            self._append_state_event(
                claimed.message.recipient,
                {
                    "op": "ack",
                    "message_id": claimed.message.id,
                    "token": claimed.lease_token,
                },
            )

    def release(self, claimed: ClaimedMessage) -> None:
        """Return a leased message to its mailbox so it can be retried immediately."""
        with self._locked():
            state = self._load_state(claimed.message.recipient)
            lease = state["leases"].get(claimed.message.id)
            self._assert_lease(claimed, lease)
            state["leases"].pop(claimed.message.id, None)
            self._append_state_event(
                claimed.message.recipient,
                {
                    "op": "release",
                    "message_id": claimed.message.id,
                    "token": claimed.lease_token,
                },
            )

    def mailbox_messages(self, recipient: str) -> list[BusMessage]:
        """Return an audit view of the append-only inbox, including acknowledged lines."""
        recipient = _validate_actor_name(recipient)
        with self._locked():
            return self._read_mailbox(recipient)

    def pending_count(self, recipient: str) -> int:
        """Return currently deliverable messages; expired leases are reclaimed first."""
        recipient = _validate_actor_name(recipient)
        with self._locked():
            state = self._load_state(recipient)
            self._release_expired_leases(state, time.time())
            self._move_exhausted_to_dead_letter(recipient, state)
            return sum(
                1
                for message in self._read_mailbox(recipient)
                if message.id not in state["acknowledged"]
                and message.id not in state["leases"]
            )

    def _locked(self) -> "_CombinedLock":
        return _CombinedLock(self._thread_lock, self.lock_path)

    def _mailbox_path(self, recipient: str) -> Path:
        return self.mailbox_dir / f"{recipient}.jsonl"

    def _state_path(self, recipient: str) -> Path:
        return self.state_dir / f"{recipient}.jsonl"

    def _read_mailbox(self, recipient: str) -> list[BusMessage]:
        path = self._mailbox_path(recipient)
        if not path.exists():
            return []
        messages: list[BusMessage] = []
        with path.open("r", encoding="utf-8") as stream:
            lines = stream.readlines()
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("entry is not an object")
                messages.append(BusMessage.from_dict(raw))
            except (json.JSONDecodeError, MessageBusError, ValueError) as exc:
                # A torn final append has no trailing newline and is not
                # deliverable; leave it for inspection rather than guessing.
                if line_number == len(lines) and not line.endswith("\n"):
                    continue
                raise MessageBusError(
                    f"Malformed mailbox record in {path.name} at line {line_number}."
                ) from exc
        return messages

    def _load_state(self, recipient: str) -> dict[str, Any]:
        path = self._state_path(recipient)
        if not path.exists():
            return self._empty_state()
        state = self._empty_state()
        with path.open("r", encoding="utf-8") as stream:
            lines = stream.readlines()
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("state event is not an object")
                self._apply_state_event(state, raw)
            except (json.JSONDecodeError, MessageBusError, ValueError) as exc:
                if line_number == len(lines) and not line.endswith("\n"):
                    continue
                raise MessageBusError(
                    f"Malformed mailbox state in {path.name} at line {line_number}."
                ) from exc
        return state

    def _append_state_event(self, recipient: str, event: dict[str, Any]) -> None:
        _append_jsonl(
            self._state_path(recipient),
            {"version": self.STATE_VERSION, "timestamp": time.time(), **event},
        )

    @staticmethod
    def _apply_state_event(state: dict[str, Any], event: dict[str, Any]) -> None:
        if event.get("version") != FileMessageBus.STATE_VERSION:
            raise MessageBusError("Unsupported mailbox state event version.")
        operation = event.get("op")
        message_id = str(event.get("message_id") or "")
        if not message_id:
            raise MessageBusError("Mailbox state event is missing message_id.")
        if operation == "claim":
            consumer_id = str(event.get("consumer_id") or "")
            token = str(event.get("token") or "")
            try:
                attempt = int(event["attempt"])
                expires_at = float(event["expires_at"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MessageBusError("Invalid mailbox claim event.") from exc
            if not consumer_id or not token or attempt < 1:
                raise MessageBusError("Invalid mailbox claim event.")
            state["attempts"][message_id] = attempt
            state["leases"][message_id] = {
                "consumer_id": consumer_id,
                "token": token,
                "expires_at": expires_at,
            }
            return
        if operation == "release":
            FileMessageBus._remove_matching_lease(state, message_id, event.get("token"))
            return
        if operation in {"ack", "dead_letter"}:
            FileMessageBus._remove_matching_lease(state, message_id, event.get("token"))
            state["acknowledged"][message_id] = float(event.get("timestamp") or 0)
            return
        raise MessageBusError(f"Unsupported mailbox state operation: {operation!r}.")

    @staticmethod
    def _remove_matching_lease(state: dict[str, Any], message_id: str, token: object) -> None:
        lease = state["leases"].get(message_id)
        if lease is None or token is None or lease.get("token") == token:
            state["leases"].pop(message_id, None)

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": self.STATE_VERSION,
            "acknowledged": {},
            "attempts": {},
            "leases": {},
        }

    @staticmethod
    def _release_expired_leases(state: dict[str, Any], now: float) -> None:
        for message_id, lease in list(state["leases"].items()):
            if not isinstance(lease, dict) or float(lease.get("expires_at") or 0) <= now:
                state["leases"].pop(message_id, None)

    def _move_exhausted_to_dead_letter(
        self,
        recipient: str,
        state: dict[str, Any],
    ) -> None:
        messages = {message.id: message for message in self._read_mailbox(recipient)}
        for message_id, attempts in list(state["attempts"].items()):
            if message_id in state["acknowledged"] or message_id in state["leases"]:
                continue
            if int(attempts) < self.max_attempts:
                continue
            message = messages.get(message_id)
            if message:
                _append_jsonl(
                    self.dead_letter_path,
                    {
                        "message": message.to_dict(),
                        "attempts": int(attempts),
                        "dead_lettered_at": time.time(),
                    },
                )
            state["acknowledged"][message_id] = time.time()
            self._append_state_event(
                recipient,
                {"op": "dead_letter", "message_id": message_id, "token": None},
            )

    @staticmethod
    def _assert_lease(claimed: ClaimedMessage, lease: Any) -> None:
        if not isinstance(lease, dict):
            raise MessageBusError("Message lease is no longer active.")
        if (
            lease.get("consumer_id") != claimed.consumer_id
            or lease.get("token") != claimed.lease_token
        ):
            raise MessageBusError("Message lease belongs to another consumer.")


class _CombinedLock:
    """Acquire the cheap in-process lock before the project-wide file lock."""

    def __init__(self, thread_lock: threading.RLock, path: Path) -> None:
        self.thread_lock = thread_lock
        self.file_lock = _MailboxFileLock(path)

    def __enter__(self) -> "_CombinedLock":
        self.thread_lock.acquire()
        try:
            self.file_lock.__enter__()
        except Exception:
            self.thread_lock.release()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.file_lock.__exit__(exc_type, exc, traceback)
        finally:
            self.thread_lock.release()


def _validate_actor_name(value: str) -> str:
    name = str(value).strip()
    if not name or name in {".", ".."} or any(part in name for part in ("/", "\\", ":")):
        raise ValueError("Agent mailbox name must be a simple non-empty identifier.")
    return name


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _repair_jsonl_tail(path)
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    with path.open("ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _repair_jsonl_tail(path: Path) -> None:
    """Prevent a crash-torn final JSON object from corrupting the next append."""
    if not path.exists():
        return
    with path.open("r+b") as stream:
        content = stream.read()
        if not content or content.endswith(b"\n"):
            return
        last_newline = content.rfind(b"\n")
        prefix_end = last_newline + 1
        tail = content[prefix_end:]
        try:
            parsed = json.loads(tail.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("final JSONL record is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            stream.seek(prefix_end)
            stream.truncate()
        else:
            stream.seek(0, os.SEEK_END)
            stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())

