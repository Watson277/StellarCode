from __future__ import annotations

import threading
from pathlib import Path

from stellarcode.skill.model import Skill, SkillSource
from stellarcode.skill.parser import parse_frontmatter
from stellarcode.skill.state import SkillStateStore


class SkillRegistry:
    """Loads builtin, user, and project skills with later layers overriding earlier ones."""

    def __init__(
        self,
        builtin_dir: str | Path | None,
        user_dir: str | Path | None,
        project_dir: str | Path | None,
        state_store: SkillStateStore | None = None,
    ) -> None:
        self.builtin_dir = Path(builtin_dir) if builtin_dir is not None else None
        self.user_dir = Path(user_dir) if user_dir is not None else None
        self.project_dir = Path(project_dir) if project_dir is not None else None
        self.state_store = state_store
        self._skills: dict[str, Skill] = {}
        self._warnings: list[str] = []
        self._lock = threading.RLock()

    def reload(self) -> None:
        loaded: dict[str, Skill] = {}
        warnings: list[str] = []
        for directory, source in (
            (self.builtin_dir, SkillSource.BUILTIN),
            (self.user_dir, SkillSource.USER),
            (self.project_dir, SkillSource.PROJECT),
        ):
            self._load_directory(directory, source, loaded, warnings)
        with self._lock:
            self._skills = loaded
            self._warnings = warnings

    def all_skills(self) -> list[Skill]:
        with self._lock:
            return sorted(self._skills.values(), key=lambda skill: skill.name)

    def enabled_skills(self) -> list[Skill]:
        disabled = self.state_store.disabled() if self.state_store else frozenset()
        return [skill for skill in self.all_skills() if skill.name not in disabled]

    def find_skill(self, name: str | None) -> Skill | None:
        if not name:
            return None
        if self.state_store and name in self.state_store.disabled():
            return None
        return self.find_any_skill(name)

    def find_any_skill(self, name: str | None) -> Skill | None:
        if not name:
            return None
        with self._lock:
            return self._skills.get(name)

    def warnings(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._warnings)

    @staticmethod
    def _load_directory(
        directory: Path | None,
        source: SkillSource,
        loaded: dict[str, Skill],
        warnings: list[str],
    ) -> None:
        if directory is None or not directory.is_dir():
            return
        try:
            entries = sorted(path for path in directory.iterdir() if path.is_dir())
        except OSError as exc:
            warnings.append(f"could not scan skill directory {directory}: {exc}")
            return

        for skill_dir in entries:
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                parsed = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            except OSError as exc:
                warnings.append(f"could not read {skill_md}: {exc}")
                continue
            warnings.extend(f"{skill_md}: {warning}" for warning in parsed.warnings)
            name = _string_field(parsed.frontmatter, "name") or skill_dir.name
            references_dir = skill_dir / "references"
            loaded[name] = Skill(
                name=name,
                description=_string_field(parsed.frontmatter, "description") or "",
                version=_string_field(parsed.frontmatter, "version"),
                author=_string_field(parsed.frontmatter, "author"),
                tags=_string_list_field(parsed.frontmatter, "tags"),
                source=source,
                body=parsed.body,
                skill_md_path=skill_md,
                references_dir=references_dir if references_dir.is_dir() else None,
            )


def builtin_skills_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "skills"


def _string_field(frontmatter: dict[str, object], key: str) -> str | None:
    value = frontmatter.get(key)
    return value if isinstance(value, str) else None


def _string_list_field(frontmatter: dict[str, object], key: str) -> tuple[str, ...]:
    value = frontmatter.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))

