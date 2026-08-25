"""CLI-friendly skill management commands backed by the shared registry."""

from __future__ import annotations

from stellarcode.skill.model import Skill
from stellarcode.skill.registry import SkillRegistry, bootstrap_bundled_skills
from stellarcode.skill.state import SkillStateStore


def startup_summary(registry: SkillRegistry) -> str:
    all_skills = registry.all_skills()
    if not all_skills:
        return "Skills: none discovered"
    return f"Skills: {len(registry.enabled_skills())}/{len(all_skills)} enabled"


def format_skill_warnings(
    registry: SkillRegistry,
    state_store: SkillStateStore,
) -> str:
    warnings = list(dict.fromkeys((*registry.warnings(), *state_store.warnings())))
    if not warnings:
        return ""
    return "Skill warnings:\n" + "\n".join(f"  - {warning}" for warning in warnings)


def handle_skill_command(
    command: str,
    registry: SkillRegistry,
    state_store: SkillStateStore,
) -> str:
    parts = command.split(maxsplit=2)
    if parts in (["/skill"], ["/skill", "list"]):
        return _with_warnings(_list_skills(registry), registry, state_store)
    if parts == ["/skill", "reload"]:
        bootstrap_warnings = (
            bootstrap_bundled_skills(
                registry.user_dir,
                state_store=state_store,
            )
            if registry.user_dir is not None
            else ()
        )
        registry.reload()
        message = f"Skills reloaded. {startup_summary(registry)}; changes apply next turn."
        if bootstrap_warnings:
            message = f"{message}\n" + "\n".join(f"  - {warning}" for warning in bootstrap_warnings)
        return _with_warnings(message, registry, state_store)
    if len(parts) != 3:
        return _usage()

    action, name = parts[1], parts[2].strip()
    if action == "show":
        return _show_skill(registry, name)
    if action == "on":
        return _set_enabled(registry, state_store, name, enabled=True)
    if action == "off":
        return _set_enabled(registry, state_store, name, enabled=False)
    return _usage()


def _list_skills(registry: SkillRegistry) -> str:
    all_skills = registry.all_skills()
    if not all_skills:
        return "Skills: none discovered\nRun /skill reload to scan again."
    enabled = {skill.name for skill in registry.enabled_skills()}
    lines = [f"Skills ({len(all_skills)}):"]
    for skill in all_skills:
        marker = "on " if skill.name in enabled else "off"
        version = f" v{skill.version}" if skill.version else ""
        description = _abbreviate(skill.description, 80)
        lines.append(
            f"  [{marker}] {skill.name:<16} {skill.source.value:<7}{version:<10} {description}"
        )
    lines.extend(
        [
            "",
            "Use /skill show <name>, /skill on|off <name>, or /skill reload.",
        ]
    )
    return "\n".join(lines)


def _show_skill(registry: SkillRegistry, name: str) -> str:
    skill = registry.find_any_skill(name)
    if skill is None:
        return f"Skill not found: {name}. Use /skill list to inspect skills."
    references = f"\nreferences: {skill.references_dir}" if skill.references_dir else ""
    return (
        f"Skill: {skill.name} ({skill.source.value})\n"
        f"path: {skill.skill_md_path}{references}\n\n"
        f"{_frontmatter(skill)}\n\n{skill.body}"
    )


def _set_enabled(
    registry: SkillRegistry,
    state_store: SkillStateStore,
    name: str,
    *,
    enabled: bool,
) -> str:
    if registry.find_any_skill(name) is None:
        return f"Skill not found: {name}. Use /skill list to inspect skills."
    if enabled:
        if not state_store.enable(name):
            return f"Could not enable skill: {name}. {_latest_state_warning(state_store)}"
        return f"Enabled skill: {name}. The change applies on the next LLM turn."
    if not state_store.disable(name):
        return f"Could not disable skill: {name}. {_latest_state_warning(state_store)}"
    return f"Disabled skill: {name}. State saved to {state_store.file}."


def _frontmatter(skill: Skill) -> str:
    lines = ["---", f"name: {skill.name}", f"description: {skill.description}"]
    if skill.version:
        lines.append(f'version: "{skill.version}"')
    if skill.author:
        lines.append(f"author: {skill.author}")
    if skill.tags:
        lines.append(f"tags: [{', '.join(skill.tags)}]")
    lines.append("---")
    return "\n".join(lines)


def _abbreviate(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."


def _usage() -> str:
    return (
        "usage: /skill | /skill list | /skill show <name> | "
        "/skill on <name> | /skill off <name> | /skill reload"
    )


def _with_warnings(
    message: str,
    registry: SkillRegistry,
    state_store: SkillStateStore,
) -> str:
    warnings = format_skill_warnings(registry, state_store)
    return f"{message}\n\n{warnings}" if warnings else message


def _latest_state_warning(state_store: SkillStateStore) -> str:
    warnings = state_store.warnings()
    return warnings[-1] if warnings else f"Could not update {state_store.file}."
