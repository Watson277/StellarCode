"""Skill discovery, enablement, prompt indexing, and deferred body injection."""

from stellarcode.skill.commands import (
    format_skill_warnings,
    handle_skill_command,
    startup_summary,
)
from stellarcode.skill.context import SkillContextBuffer
from stellarcode.skill.formatter import format_skill_index
from stellarcode.skill.model import Skill, SkillSource
from stellarcode.skill.parser import FrontmatterResult, parse_frontmatter
from stellarcode.skill.registry import (
    SkillRegistry,
    bootstrap_bundled_skills,
)
from stellarcode.skill.state import SkillStateStore
from stellarcode.skill.upgrade import (
    BundledSkillManager,
    SkillUpgradeError,
    bundled_skills_dir,
    skill_tree_hash,
)
from stellarcode.skill.tools import (
    activate_skill_context,
    explicit_skill_context,
    register_skill_tools,
    render_skill_guidance,
)

__all__ = [
    "FrontmatterResult",
    "BundledSkillManager",
    "Skill",
    "SkillContextBuffer",
    "SkillRegistry",
    "SkillSource",
    "SkillStateStore",
    "SkillUpgradeError",
    "activate_skill_context",
    "bootstrap_bundled_skills",
    "bundled_skills_dir",
    "explicit_skill_context",
    "format_skill_index",
    "format_skill_warnings",
    "handle_skill_command",
    "parse_frontmatter",
    "register_skill_tools",
    "render_skill_guidance",
    "startup_summary",
    "skill_tree_hash",
]
