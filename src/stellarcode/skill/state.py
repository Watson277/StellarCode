"""Persist user-controlled Skill enablement independently from Skill source files."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class SkillStateStore:
    """Persist enablement plus trusted baselines for editable bundled Skills."""

    def __init__(self, file: str | Path) -> None:
        self.file = Path(file)
        self.lock_file = self.file.parent / "skills-state.lock"
        self._lock = threading.RLock()
        self._warnings: list[str] = []

    def disabled(self) -> frozenset[str]:
        with self._lock:
            values = self._read_data().get("disabled", [])
            return frozenset(
                value.strip()
                for value in values
                if isinstance(value, str) and value.strip()
            ) if isinstance(values, list) else frozenset()

    def bundled_records(self) -> dict[str, dict[str, str]]:
        """Return a defensive copy of validated bundled Skill baseline records."""

        with self._lock:
            raw = self._read_data().get("bundled", {})
            if not isinstance(raw, dict):
                return {}
            records: dict[str, dict[str, str]] = {}
            for name, value in raw.items():
                if not isinstance(name, str) or not isinstance(value, dict):
                    continue
                records[name] = {
                    str(key): item
                    for key, item in value.items()
                    if isinstance(key, str) and isinstance(item, str)
                }
            return records

    def set_bundled_record(self, name: str, record: dict[str, str]) -> bool:
        with self._lock, _exclusive_state_lock(self.lock_file):
            data = self._read_data()
            raw_bundled = data.get("bundled", {})
            bundled = dict(raw_bundled) if isinstance(raw_bundled, dict) else {}
            bundled[name] = {
                str(key): str(value)
                for key, value in record.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            data["bundled"] = bundled
            return self._write_data(data)

    def disable(self, name: str) -> bool:
        with self._lock, _exclusive_state_lock(self.lock_file):
            values = set(self.disabled())
            values.add(name)
            return self._write(values)

    def enable(self, name: str) -> bool:
        with self._lock, _exclusive_state_lock(self.lock_file):
            values = set(self.disabled())
            values.discard(name)
            return self._write(values)

    def warnings(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._warnings)

    def _write(self, disabled: set[str]) -> bool:
        data = self._read_data()
        data["disabled"] = sorted(disabled)
        return self._write_data(data)

    def _read_data(self) -> dict[str, object]:
        if not self.file.exists():
            return {}
        try:
            _reject_link_or_reparse(self.file)
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return dict(data) if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            self._warn(f"could not parse {self.file}; stored Skill state ignored: {exc}")
            return {}

    def _write_data(self, data: dict[str, object]) -> bool:
        temporary = self.file.with_name(
            f".{self.file.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            if self.file.exists():
                _reject_link_or_reparse(self.file)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(f"{content}\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.file)
            return True
        except OSError as exc:
            self._warn(f"could not write {self.file}: {exc}")
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _warn(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)


@contextmanager
def _exclusive_state_lock(path: Path, timeout_seconds: float = 10.0):
    """Serialize read-modify-write updates from desktop and CLI processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _reject_link_or_reparse(path)
    stream = path.open("a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise OSError("timed out waiting for Skill state lock") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _reject_link_or_reparse(path: Path) -> None:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
        raise OSError(f"links and reparse points are not allowed for Skill state: {path}")
