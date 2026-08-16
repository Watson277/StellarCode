from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class SkillSource(str, Enum):
    USER = "user"
    PROJECT = "project"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str | None
    author: str | None
    tags: tuple[str, ...]
    source: SkillSource
    body: str
    skill_md_path: Path
    references_dir: Path | None = None

