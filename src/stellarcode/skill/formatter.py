from __future__ import annotations

from stellarcode.skill.model import Skill


MAX_DESCRIPTION_CHARACTERS = 500
MAX_ENABLED_SKILLS = 20
MAX_INDEX_BYTES = 4096


def format_skill_index(enabled: list[Skill] | None) -> str:
    if not enabled:
        return ""

    selected = sorted(enabled, key=lambda skill: skill.name)[:MAX_ENABLED_SKILLS]
    lines = ["## 可用 Skills（按需调用 load_skill 加载完整指引）", ""]
    for skill in selected:
        description = skill.description.strip()
        if len(description) > MAX_DESCRIPTION_CHARACTERS:
            description = f"{description[:MAX_DESCRIPTION_CHARACTERS]}..."
        lines.append(f"- **{skill.name}**：{description}")
    lines.extend(
        [
            "",
            (
                "判断准则：当任务描述匹配某个 skill 的触发场景时，调用 "
                "load_skill(name) 加载完整指引；已加载的 skill 会在下一条用户消息中以 "
                '"## 已加载 Skill" 段落出现。不要重复加载同一 skill。'
            ),
        ]
    )
    content = "\n".join(lines) + "\n"
    return _truncate_utf8(content, MAX_INDEX_BYTES)


def _truncate_utf8(content: str, limit: int) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) <= limit:
        return content
    suffix = "\n...(skill 索引段被截断)\n"
    budget = max(0, limit - len(suffix.encode("utf-8")))
    truncated = encoded[:budget]
    while truncated:
        try:
            return truncated.decode("utf-8") + suffix
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return suffix

