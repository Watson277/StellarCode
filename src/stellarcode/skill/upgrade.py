"""Safe lifecycle management for StellarCode's editable bundled Skills.

Bundled Skills are copied into the user layer so people can inspect and customize them.
This module keeps an immutable content-hash baseline for each copy: clean copies upgrade
automatically, while edited copies are never overwritten without an explicit confirmed
operation using optimistic hash checks.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stellarcode.skill.parser import parse_frontmatter
from stellarcode.skill.state import SkillStateStore


SKILL_TREE_HASH_SCHEMA = "stellarcode.skill-tree/v1"
MAX_SKILL_FILES = 1_024
MAX_SKILL_BYTES = 32 * 1024 * 1024
MAX_DIFF_FILE_BYTES = 2 * 1024 * 1024
MAX_SKILL_DEPTH = 12
MAX_SKILL_ENTRIES = 2_048
MAX_SKILL_RELATIVE_PATH = 512
_SAFE_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class SkillUpgradeError(RuntimeError):
    """A bundled Skill could not be inspected or changed safely."""


@dataclass(frozen=True, slots=True)
class BundledSkillDescriptor:
    name: str
    version: str
    content_hash: str
    source_dir: Path


class BundledSkillManager:
    """Install, inspect, and explicitly reconcile editable bundled Skill copies."""

    def __init__(
        self,
        user_dir: str | Path,
        state_store: SkillStateStore,
        bundled_dir: str | Path | None = None,
    ) -> None:
        self.user_dir = Path(user_dir)
        self.bundled_dir = (
            Path(bundled_dir) if bundled_dir is not None else bundled_skills_dir()
        )
        self.state_store = state_store
        self.lock_file = state_store.file.parent / "skill-upgrade.lock"
        self._lock = threading.RLock()
        self._warnings: list[str] = []

    def bootstrap(self) -> tuple[str, ...]:
        """Install missing Skills and automatically upgrade only proven-clean copies."""

        warnings: list[str] = []
        with self._lock, _exclusive_file_lock(self.lock_file):
            try:
                self._validate_roots(create_user_root=True)
                descriptors = self._descriptors()
            except (OSError, SkillUpgradeError) as exc:
                warnings.append(f"could not initialize bundled Skills: {exc}")
                self._set_warnings(warnings)
                return tuple(warnings)

            records = self.state_store.bundled_records()
            for name, descriptor in descriptors.items():
                target = self.user_dir / name
                try:
                    record = dict(records.get(name, {}))
                    self._bootstrap_one(descriptor, target, record)
                except (OSError, SkillUpgradeError) as exc:
                    warnings.append(f"could not reconcile bundled Skill {name}: {exc}")
        self._set_warnings(warnings)
        return tuple(warnings)

    def statuses(self) -> dict[str, dict[str, Any]]:
        """Return live upgrade status for every currently bundled Skill."""

        with self._lock, _exclusive_file_lock(self.lock_file):
            try:
                self._validate_roots(create_user_root=False)
                descriptors = self._descriptors()
            except (OSError, SkillUpgradeError) as exc:
                self._set_warnings([f"could not inspect bundled Skills: {exc}"])
                return {}
            records = self.state_store.bundled_records()
            result: dict[str, dict[str, Any]] = {}
            warnings: list[str] = []
            for name, descriptor in descriptors.items():
                target = self.user_dir / name
                try:
                    result[name] = self._status(descriptor, target, records.get(name, {}))
                except (OSError, SkillUpgradeError) as exc:
                    warnings.append(f"could not inspect bundled Skill {name}: {exc}")
                    result[name] = {
                        "builtin": True,
                        "builtin_version": descriptor.version,
                        "builtin_hash": descriptor.content_hash,
                        "installed_version": "",
                        "installed_hash": "",
                        "current_hash": "",
                        "customized": True,
                        "update_available": False,
                        "update_acknowledged": False,
                        "upgrade_state": "error",
                        "error": str(exc),
                    }
            self._set_warnings(warnings)
            return result

    def status(self, name: str) -> dict[str, Any]:
        descriptor = self._descriptor(name)
        target = self.user_dir / name
        return self._status(
            descriptor,
            target,
            self.state_store.bundled_records().get(name, {}),
        )

    def diff(self, name: str, *, max_chars: int = 120_000) -> dict[str, Any]:
        """Build a bounded unified diff from the current user copy to the bundled copy."""

        with self._lock, _exclusive_file_lock(self.lock_file):
            descriptor = self._descriptor(name)
            target = self.user_dir / name
            status_data = self._status(
                descriptor,
                target,
                self.state_store.bundled_records().get(name, {}),
            )
            limit = max(1_000, min(int(max_chars), 200_000))
            current_files = _read_diff_files(target)
            bundled_files = _read_diff_files(descriptor.source_dir)
            chunks: list[str] = []
            additions = 0
            deletions = 0
            for relative in sorted(set(current_files) | set(bundled_files)):
                current = current_files.get(relative)
                bundled = bundled_files.get(relative)
                if current == bundled:
                    continue
                rendered, added, removed = _file_diff(relative, current, bundled)
                chunks.append(rendered)
                additions += added
                deletions += removed
            full_diff = "\n".join(chunks) or "No differences."
            truncated = len(full_diff) > limit
            visible_diff = full_diff[:limit]
            if truncated:
                visible_diff += "\n\n[diff truncated]"
            return {
                "name": name,
                "diff": visible_diff,
                "truncated": truncated,
                "additions": additions,
                "deletions": deletions,
                "current_hash": status_data["current_hash"],
                "builtin_hash": descriptor.content_hash,
                "current_version": status_data["current_version"],
                "builtin_version": descriptor.version,
            }

    def update(
        self,
        name: str,
        *,
        expected_current_hash: str,
        expected_builtin_hash: str,
    ) -> dict[str, Any]:
        """Accept the available bundled version after a Diff-backed confirmation."""

        return self._replace_explicit(
            name,
            expected_current_hash=expected_current_hash,
            expected_builtin_hash=expected_builtin_hash,
            require_update=True,
        )

    def restore_default(
        self,
        name: str,
        *,
        expected_current_hash: str,
        expected_builtin_hash: str,
    ) -> dict[str, Any]:
        """Replace any customization with the latest bundled default."""

        return self._replace_explicit(
            name,
            expected_current_hash=expected_current_hash,
            expected_builtin_hash=expected_builtin_hash,
            require_update=False,
        )

    def keep_custom(
        self,
        name: str,
        *,
        expected_current_hash: str,
        expected_builtin_hash: str,
    ) -> dict[str, Any]:
        """Acknowledge one bundled release while preserving the customized copy."""

        with self._lock, _exclusive_file_lock(self.lock_file):
            descriptor = self._descriptor(name)
            self._check_builtin_hash(descriptor, expected_builtin_hash)
            target = self.user_dir / name
            current_hash = skill_tree_hash(target)
            self._check_current_hash(current_hash, expected_current_hash)
            record = dict(self.state_store.bundled_records().get(name, {}))
            status_data = self._status(descriptor, target, record)
            if not status_data["customized"]:
                raise SkillUpgradeError("the Skill is not customized")
            record.update(
                {
                    "builtin_version": descriptor.version,
                    "builtin_hash": descriptor.content_hash,
                    "kept_builtin_hash": descriptor.content_hash,
                    "kept_custom_hash": current_hash,
                }
            )
            self._save_record(name, record)
            return self._status(descriptor, target, record)

    def warnings(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._warnings)

    def _bootstrap_one(
        self,
        descriptor: BundledSkillDescriptor,
        target: Path,
        record: dict[str, str],
    ) -> None:
        if not target.exists():
            self._install_clean(descriptor, target, expected_current_hash=None)
            return
        _validate_skill_tree(target, "installed Skill")
        current_hash = skill_tree_hash(target)
        if current_hash == descriptor.content_hash:
            self._save_clean_record(descriptor)
            return

        installed_hash = _stored_hash(record.get("installed_hash"))
        if installed_hash and current_hash == installed_hash:
            self._install_clean(
                descriptor,
                target,
                expected_current_hash=current_hash,
            )
            return

        # Existing legacy or edited copies are preserved. An empty installed_hash
        # deliberately means "no trusted clean baseline", so future bootstraps can
        # never mistake this copy for an auto-upgrade candidate.
        if not record:
            record = {
                "schema_version": "1",
                "installed_version": _skill_version(target / "SKILL.md"),
                "installed_hash": "",
            }
        record.update(
            {
                "builtin_version": descriptor.version,
                "builtin_hash": descriptor.content_hash,
            }
        )
        self._save_record(descriptor.name, record)

    def _replace_explicit(
        self,
        name: str,
        *,
        expected_current_hash: str,
        expected_builtin_hash: str,
        require_update: bool,
    ) -> dict[str, Any]:
        with self._lock, _exclusive_file_lock(self.lock_file):
            descriptor = self._descriptor(name)
            self._check_builtin_hash(descriptor, expected_builtin_hash)
            target = self.user_dir / name
            record = self.state_store.bundled_records().get(name, {})
            status_data = self._status(descriptor, target, record)
            self._check_current_hash(
                str(status_data["current_hash"]),
                expected_current_hash,
            )
            if require_update and not status_data["update_available"]:
                raise SkillUpgradeError("no unacknowledged bundled update is available")
            self._install_clean(
                descriptor,
                target,
                expected_current_hash=expected_current_hash,
            )
            return self._status(
                descriptor,
                target,
                self.state_store.bundled_records().get(name, {}),
            )

    def _status(
        self,
        descriptor: BundledSkillDescriptor,
        target: Path,
        record: dict[str, str],
    ) -> dict[str, Any]:
        _validate_skill_tree(target, "installed Skill")
        current_hash = skill_tree_hash(target)
        current_version = _skill_version(target / "SKILL.md")
        installed_hash = _stored_hash(record.get("installed_hash"))
        customized = current_hash != descriptor.content_hash and (
            not installed_hash or current_hash != installed_hash
        )
        kept_builtin_hash = _stored_hash(record.get("kept_builtin_hash"))
        has_new_builtin = bool(
            descriptor.content_hash != installed_hash
            and descriptor.content_hash != kept_builtin_hash
        )
        acknowledged = bool(
            customized
            and kept_builtin_hash == descriptor.content_hash
            and _stored_hash(record.get("kept_custom_hash")) == current_hash
        )
        update_available = bool(customized and has_new_builtin and not acknowledged)
        if update_available:
            upgrade_state = "update_available"
        elif acknowledged:
            upgrade_state = "custom_kept"
        elif customized:
            upgrade_state = "customized"
        else:
            upgrade_state = "current"
        return {
            "builtin": True,
            "builtin_version": descriptor.version,
            "builtin_hash": descriptor.content_hash,
            "installed_version": str(record.get("installed_version") or ""),
            "installed_hash": installed_hash,
            "current_version": current_version,
            "current_hash": current_hash,
            "customized": customized,
            "update_available": update_available,
            "update_acknowledged": acknowledged,
            "upgrade_state": upgrade_state,
            "error": "",
        }

    def _descriptor(self, name: str) -> BundledSkillDescriptor:
        _validate_skill_name(name)
        descriptors = self._descriptors()
        descriptor = descriptors.get(name)
        if descriptor is None:
            raise SkillUpgradeError(f"bundled Skill not found: {name}")
        return descriptor

    def _descriptors(self) -> dict[str, BundledSkillDescriptor]:
        _validate_skill_tree(self.bundled_dir, "bundled Skill root", require_skill_md=False)
        descriptors: dict[str, BundledSkillDescriptor] = {}
        for source in sorted(self.bundled_dir.iterdir()):
            if not source.is_dir() or not (source / "SKILL.md").is_file():
                continue
            _validate_skill_name(source.name)
            _validate_skill_tree(source, "bundled Skill")
            skill_name, version = _skill_identity(source / "SKILL.md")
            if skill_name != source.name:
                raise SkillUpgradeError(
                    "bundled Skill frontmatter name must match its directory: "
                    f"{skill_name!r} != {source.name!r}"
                )
            if not version:
                raise SkillUpgradeError(
                    f"bundled Skill {source.name!r} must declare a non-empty version"
                )
            descriptors[source.name] = BundledSkillDescriptor(
                name=source.name,
                version=version,
                content_hash=skill_tree_hash(source),
                source_dir=source,
            )
        return descriptors

    def _install_clean(
        self,
        descriptor: BundledSkillDescriptor,
        target: Path,
        *,
        expected_current_hash: str | None,
    ) -> None:
        self.user_dir.mkdir(parents=True, exist_ok=True)
        stage = self.user_dir / f".{descriptor.name}.stage-{uuid.uuid4().hex}"
        backup = self.user_dir / f".{descriptor.name}.backup-{uuid.uuid4().hex}"
        had_target = target.exists()
        installed = False
        try:
            shutil.copytree(descriptor.source_dir, stage)
            if skill_tree_hash(stage) != descriptor.content_hash:
                raise SkillUpgradeError("staged bundled content failed hash verification")
            if had_target:
                target.replace(backup)
                try:
                    if expected_current_hash is not None:
                        moved_hash = skill_tree_hash(backup)
                        self._check_current_hash(moved_hash, expected_current_hash)
                    stage.replace(target)
                    installed = True
                except Exception:
                    if target.exists():
                        _remove_owned_tree(target)
                    backup.replace(target)
                    raise
            else:
                if expected_current_hash is not None:
                    raise SkillUpgradeError("the Skill was removed after its status was loaded")
                stage.replace(target)
                installed = True
            try:
                # Commit the trusted baseline while the previous tree is still
                # recoverable. A failed state write must never destroy custom work.
                self._save_clean_record(descriptor)
            except Exception:
                if installed and target.exists():
                    _remove_owned_tree(target)
                if had_target and backup.exists():
                    backup.replace(target)
                raise
            if backup.exists():
                _remove_owned_tree(backup)
        finally:
            if stage.exists():
                _remove_owned_tree(stage)
            if backup.exists() and target.exists():
                _remove_owned_tree(backup)

    def _save_clean_record(self, descriptor: BundledSkillDescriptor) -> None:
        self._save_record(
            descriptor.name,
            {
                "schema_version": "1",
                "installed_version": descriptor.version,
                "installed_hash": descriptor.content_hash,
                "builtin_version": descriptor.version,
                "builtin_hash": descriptor.content_hash,
                "kept_builtin_hash": "",
                "kept_custom_hash": "",
            },
        )

    def _save_record(self, name: str, record: dict[str, str]) -> None:
        if not self.state_store.set_bundled_record(name, record):
            warnings = self.state_store.warnings()
            detail = warnings[-1] if warnings else "could not persist bundled Skill state"
            raise SkillUpgradeError(detail)

    def _validate_roots(self, *, create_user_root: bool) -> None:
        if create_user_root:
            self.user_dir.mkdir(parents=True, exist_ok=True)
        _validate_directory(self.user_dir, "user Skill root")
        _validate_directory(self.bundled_dir, "bundled Skill root")

    @staticmethod
    def _check_current_hash(actual: str, expected: str) -> None:
        if not expected or actual != expected:
            raise SkillUpgradeError(
                "the installed Skill changed after the preview; reload and review the new Diff"
            )

    @staticmethod
    def _check_builtin_hash(
        descriptor: BundledSkillDescriptor,
        expected: str,
    ) -> None:
        if not expected or descriptor.content_hash != expected:
            raise SkillUpgradeError(
                "the bundled Skill changed after the preview; reload before continuing"
            )

    def _set_warnings(self, warnings: list[str]) -> None:
        with self._lock:
            self._warnings = list(dict.fromkeys(warnings))


def bundled_skills_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "skills"


def skill_tree_hash(root: str | Path) -> str:
    """Hash relative file names and bytes for the complete bounded Skill tree."""

    directory = Path(root)
    _validate_skill_tree(directory, "Skill")
    files = _tree_files(directory)
    digest = hashlib.sha256()
    digest.update(f"{SKILL_TREE_HASH_SCHEMA}\0".encode())
    total_bytes = 0
    for path in files:
        relative = path.relative_to(directory).as_posix()
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > MAX_SKILL_BYTES:
            raise SkillUpgradeError(
                f"Skill content exceeds the {MAX_SKILL_BYTES}-byte safety limit"
            )
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode())
        digest.update(b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _tree_files(root: Path) -> list[Path]:
    files: list[Path] = []
    entries = 0
    for current_root, directories, names in os.walk(root, followlinks=False):
        current = Path(current_root)
        for directory_name in directories:
            directory = current / directory_name
            _validate_relative_path(root, directory)
            _reject_reparse(directory)
            entries += 1
            if entries > MAX_SKILL_ENTRIES:
                raise SkillUpgradeError(
                    f"Skill contains more than {MAX_SKILL_ENTRIES} entries"
                )
        for file_name in names:
            path = current / file_name
            _validate_relative_path(root, path)
            _reject_reparse(path)
            if not path.is_file():
                raise SkillUpgradeError(f"Skill entry is not a regular file: {path}")
            files.append(path)
            entries += 1
            if len(files) > MAX_SKILL_FILES:
                raise SkillUpgradeError(
                    f"Skill contains more than {MAX_SKILL_FILES} files"
                )
            if entries > MAX_SKILL_ENTRIES:
                raise SkillUpgradeError(
                    f"Skill contains more than {MAX_SKILL_ENTRIES} entries"
                )
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _validate_relative_path(root: Path, path: Path) -> None:
    relative = path.relative_to(root)
    if len(relative.parts) > MAX_SKILL_DEPTH:
        raise SkillUpgradeError(
            f"Skill path exceeds the maximum depth of {MAX_SKILL_DEPTH}: {relative}"
        )
    if len(relative.as_posix()) > MAX_SKILL_RELATIVE_PATH:
        raise SkillUpgradeError(
            "Skill relative path exceeds the "
            f"{MAX_SKILL_RELATIVE_PATH}-character safety limit: {relative}"
        )


def _validate_skill_tree(
    root: Path,
    label: str,
    *,
    require_skill_md: bool = True,
) -> None:
    _validate_directory(root, label)
    if require_skill_md and not (root / "SKILL.md").is_file():
        raise SkillUpgradeError(f"{label} has no regular SKILL.md: {root}")
    _tree_files(root)


def _validate_directory(path: Path, label: str) -> None:
    if not path.exists() or not path.is_dir():
        raise SkillUpgradeError(f"{label} is not a directory: {path}")
    _reject_reparse(path)


def _reject_reparse(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise SkillUpgradeError(f"could not inspect Skill path {path}: {exc}") from exc
    if stat.S_ISLNK(mode):
        raise SkillUpgradeError(f"symbolic links are not allowed in bundled Skills: {path}")
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attributes & reparse_flag:
        raise SkillUpgradeError(f"reparse points are not allowed in bundled Skills: {path}")


def _validate_skill_name(name: str) -> None:
    if not _SAFE_SKILL_NAME.fullmatch(name) or name in {".", ".."}:
        raise SkillUpgradeError(f"unsafe bundled Skill name: {name!r}")


def _stored_hash(value: object) -> str:
    candidate = value.strip().lower() if isinstance(value, str) else ""
    return candidate if re.fullmatch(r"[0-9a-f]{64}", candidate) else ""


def _skill_version(skill_md: Path) -> str:
    return _skill_identity(skill_md)[1]


def _skill_identity(skill_md: Path) -> tuple[str, str]:
    try:
        parsed = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise SkillUpgradeError(f"could not read Skill metadata {skill_md}: {exc}") from exc
    name = parsed.frontmatter.get("name")
    value = parsed.frontmatter.get("version")
    return (
        name.strip() if isinstance(name, str) else "",
        value.strip() if isinstance(value, str) else "",
    )


def _read_diff_files(root: Path) -> dict[str, bytes]:
    _validate_skill_tree(root, "Skill")
    result: dict[str, bytes] = {}
    for path in _tree_files(root):
        size = path.stat().st_size
        relative = path.relative_to(root).as_posix()
        if size > MAX_DIFF_FILE_BYTES:
            digest = _file_sha256(path)
            result[relative] = (
                f"[large file omitted: {size} bytes, sha256={digest}]".encode()
            )
        else:
            result[relative] = path.read_bytes()
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_owned_tree(path: Path) -> None:
    """Remove only a validated real directory created inside the managed root."""

    _validate_directory(path, "temporary Skill directory")
    _tree_files(path)
    shutil.rmtree(path)


def _file_diff(
    relative: str,
    current: bytes | None,
    bundled: bytes | None,
) -> tuple[str, int, int]:
    if _is_binary(current) or _is_binary(bundled):
        return (
            f"--- current/{relative}\n+++ bundled/{relative}\n"
            "Binary files differ.",
            0,
            0,
        )
    current_lines = (current or b"").decode("utf-8").splitlines()
    bundled_lines = (bundled or b"").decode("utf-8").splitlines()
    lines = list(
        difflib.unified_diff(
            current_lines,
            bundled_lines,
            fromfile=f"current/{relative}" if current is not None else "/dev/null",
            tofile=f"bundled/{relative}" if bundled is not None else "/dev/null",
            lineterm="",
        )
    )
    additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return "\n".join(lines), additions, deletions


def _is_binary(content: bytes | None) -> bool:
    if content is None:
        return False
    if b"\0" in content:
        return True
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


@contextmanager
def _exclusive_file_lock(path: Path, timeout_seconds: float = 10.0):
    """Serialize user-level Skill upgrades across CLI and desktop processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _reject_reparse(path)
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
                    raise SkillUpgradeError(
                        "timed out waiting for another Skill upgrade process"
                    ) from exc
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
