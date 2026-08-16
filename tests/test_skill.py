from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from stellarcode.agent import Agent
from stellarcode.multi_agent import AgentOrchestrator
from stellarcode.skill import (
    SkillContextBuffer,
    SkillRegistry,
    SkillSource,
    SkillStateStore,
    activate_skill_context,
    bootstrap_bundled_skills,
    bundled_skills_dir,
    explicit_skill_context,
    format_skill_index,
    handle_skill_command,
    parse_frontmatter,
    register_skill_tools,
)
from stellarcode.tools import ToolDefinition, ToolInvocation, ToolRegistry


def _write_skill(
    root: Path,
    directory_name: str,
    *,
    name: str | None = None,
    description: str = "test skill",
    body: str = "Follow this guidance.",
) -> Path:
    skill_dir = root / directory_name
    skill_dir.mkdir(parents=True)
    name_line = f"name: {name}\n" if name else ""
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\n{name_line}description: {description}\n"
        'version: "1.2.3"\nauthor: Test\ntags: [one, two]\n'
        f"---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_frontmatter_parser_supports_multiline_and_inline_list():
    result = parse_frontmatter(
        "---\n"
        "name: web-access\n"
        "description: |\n"
        "  first line\n"
        "  second line\n"
        "tags: [web, 'browser']\n"
        "---\n\n"
        "# Body\n"
    )

    assert result.frontmatter["name"] == "web-access"
    assert result.frontmatter["description"] == "first line second line"
    assert result.frontmatter["tags"] == ["web", "browser"]
    assert result.body == "\n# Body\n"
    assert result.warnings == ()


def test_frontmatter_parser_keeps_body_and_warns_without_markers():
    result = parse_frontmatter("# Plain body")

    assert result.frontmatter == {}
    assert result.body == "# Plain body"
    assert result.warnings


def test_registry_uses_user_project_override_order(tmp_path):
    user = tmp_path / "user"
    project = tmp_path / "project"
    _write_skill(user, "shared", body="user body")
    project_path = _write_skill(project, "different-directory", name="shared", body="project")
    state = SkillStateStore(tmp_path / "skills.json")
    registry = SkillRegistry(user, project, state)

    registry.reload()

    skill = registry.find_skill("shared")
    assert skill is not None
    assert skill.source == SkillSource.PROJECT
    assert skill.body.strip() == "project"
    assert skill.skill_md_path == project_path


def test_state_store_persists_only_disabled_names(tmp_path):
    store = SkillStateStore(tmp_path / ".stellarcode" / "skills.json")

    store.disable("web-access")
    store.disable("code-review")
    store.enable("web-access")

    assert store.disabled() == frozenset({"code-review"})
    assert json.loads(store.file.read_text(encoding="utf-8")) == {
        "disabled": ["code-review"]
    }


def test_registry_filters_disabled_skills_and_commands_toggle_state(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "web-access")
    store = SkillStateStore(tmp_path / "state.json")
    registry = SkillRegistry(root, None, store)
    registry.reload()

    disabled = handle_skill_command("/skill off web-access", registry, store)
    assert "Disabled" in disabled
    assert registry.find_skill("web-access") is None
    assert registry.find_any_skill("web-access") is not None

    enabled = handle_skill_command("/skill on web-access", registry, store)
    assert "Enabled" in enabled
    assert registry.find_skill("web-access") is not None
    assert "web-access" in handle_skill_command("/skill", registry, store)
    assert "Follow this guidance" in handle_skill_command(
        "/skill show web-access", registry, store
    )


def test_skill_commands_display_registry_warnings(tmp_path):
    root = tmp_path / "skills"
    skill_path = _write_skill(root, "warning-skill")
    skill_path.write_text(
        "---\nname: warning-skill\nthis line is invalid\n---\nBody\n",
        encoding="utf-8",
    )
    store = SkillStateStore(tmp_path / "state.json")
    registry = SkillRegistry(root, None, store)
    registry.reload()

    listed = handle_skill_command("/skill", registry, store)
    reloaded = handle_skill_command("/skill reload", registry, store)

    assert "Skill warnings:" in listed
    assert "cannot parse frontmatter line" in listed
    assert "Skill warnings:" in reloaded


def test_state_write_failure_is_reported_without_leaving_temp_file(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "skills"
    _write_skill(root, "web-access")
    store = SkillStateStore(tmp_path / ".stellarcode" / "skills.json")
    registry = SkillRegistry(root, None, store)
    registry.reload()

    def fail_replace(_source, _target):
        raise PermissionError("state file is locked")

    monkeypatch.setattr("stellarcode.skill.state.os.replace", fail_replace)

    result = handle_skill_command("/skill off web-access", registry, store)

    assert "Could not disable skill" in result
    assert "state file is locked" in result
    assert not store.file.with_suffix(".json.tmp").exists()
    assert store.warnings()


def test_context_buffer_is_one_shot_deduplicated_and_bounded():
    buffer = SkillContextBuffer(max_skills=3)
    buffer.push("one", "old")
    buffer.push("two", "second")
    buffer.push("one", "new")
    buffer.push("three", "third")
    buffer.push("four", "fourth")

    loaded = buffer.drain()

    assert "Skill：two" not in loaded
    assert "Skill：one" in loaded and "new" in loaded
    assert "Skill：three" in loaded
    assert "Skill：four" in loaded
    assert buffer.drain() == ""


def test_skill_index_respects_count_and_utf8_budget(tmp_path):
    root = tmp_path / "skills"
    for index in range(25):
        _write_skill(
            root,
            f"skill-{index:02}",
            description="中文描述" * 200,
        )
    registry = SkillRegistry(root, None)
    registry.reload()

    index = format_skill_index(registry.enabled_skills())

    assert "skill-00" in index
    assert "skill-20" not in index
    assert index.count("- **skill-") <= 20
    assert len(index.encode("utf-8")) <= 4096


def test_load_skill_tool_queues_body_for_next_agent_user_message(tmp_path):
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root,
        "web-access",
        description="Use for web research.",
        body="Always inspect the page evidence.",
    )
    registry = SkillRegistry(skills_root, None)
    registry.reload()
    buffer = SkillContextBuffer()
    tools = ToolRegistry()
    register_skill_tools(tools, registry, buffer)

    class SkillClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            temperature: float = 0.2,
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                assert "web-access" in messages[0]["content"]
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "load_web",
                            "type": "function",
                            "function": {
                                "name": "load_skill",
                                "arguments": '{"name":"web-access"}',
                            },
                        }
                    ],
                }
            if self.calls == 2:
                assert messages[-1]["role"] == "tool"
                return {"role": "assistant", "content": "Skill queued."}
            assert messages[-1]["role"] == "user"
            assert "## 已加载 Skill：web-access" in messages[-1]["content"]
            assert "Always inspect the page evidence." in messages[-1]["content"]
            assert "继续读取网页" in messages[-1]["content"]
            return {"role": "assistant", "content": "Used the loaded guidance."}

    client = SkillClient()
    agent = Agent(
        client,
        tools,
        skill_registry=registry,
        skill_context_buffer=buffer,
    )

    assert agent.run("读取网页") == "Skill queued."
    assert agent.run("继续读取网页") == "Used the loaded guidance."
    assert buffer.is_empty()


def test_explicit_skill_reference_loads_enabled_guidance_for_current_task(tmp_path):
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root,
        "web-access",
        body="Inspect primary evidence before answering.",
    )
    state = SkillStateStore(tmp_path / "state.json")
    registry = SkillRegistry(skills_root, None, state)
    registry.reload()

    rendered = explicit_skill_context(
        "@skill:web-access @skill:web-access investigate this page",
        registry,
    )

    assert rendered.count("Explicitly referenced Skill: web-access") == 1
    assert "Inspect primary evidence before answering." in rendered

    state.disable("web-access")
    assert explicit_skill_context("@skill:web-access investigate", registry) == ""


def test_parallel_tool_threads_route_skill_loads_to_the_calling_context(tmp_path):
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "alpha", body="Alpha guidance.")
    _write_skill(skills_root, "beta", body="Beta guidance.")
    registry = SkillRegistry(skills_root, None)
    registry.reload()
    tools = ToolRegistry(max_parallel_tools=2)
    register_skill_tools(tools, registry)
    tools.register(
        ToolDefinition(
            name="noop",
            description="Test helper.",
            parameters={"type": "object"},
            handler=lambda: "ok",
        )
    )
    alpha_buffer = SkillContextBuffer()
    beta_buffer = SkillContextBuffer()

    def load(name: str, buffer: SkillContextBuffer):
        with activate_skill_context(buffer):
            return tools.execute_tools(
                [
                    ToolInvocation(
                        id=f"load-{name}",
                        name="load_skill",
                        arguments={"name": name},
                    ),
                    ToolInvocation(id=f"noop-{name}", name="noop", arguments={}),
                ]
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        alpha_future = executor.submit(load, "alpha", alpha_buffer)
        beta_future = executor.submit(load, "beta", beta_buffer)
        assert all(result.success for result in alpha_future.result())
        assert all(result.success for result in beta_future.result())

    alpha_loaded = alpha_buffer.drain()
    beta_loaded = beta_buffer.drain()
    assert "Alpha guidance" in alpha_loaded
    assert "Beta guidance" not in alpha_loaded
    assert "Beta guidance" in beta_loaded
    assert "Alpha guidance" not in beta_loaded


def test_multi_agent_assigns_an_independent_skill_buffer_to_every_role(tmp_path):
    class UnusedClient:
        def chat(self, messages, tools=None, temperature=0.2):
            raise AssertionError("No LLM call expected in this construction test")

    orchestrator = AgentOrchestrator(
        llm_client=UnusedClient(),
        tool_registry=ToolRegistry(),
        worker_count=3,
    )

    agents = [orchestrator.planner, *orchestrator.workers, orchestrator.reviewer]
    buffers = [agent.skill_context_buffer for agent in agents]
    assert all(buffer is not None for buffer in buffers)
    assert len({id(buffer) for buffer in buffers}) == len(buffers)


def test_bundled_web_access_is_installed_into_user_skills(tmp_path):
    user_skills = tmp_path / "user-skills"
    assert bootstrap_bundled_skills(user_skills) == ()
    registry = SkillRegistry(user_skills, None)
    registry.reload()

    skill = registry.find_skill("web-access")

    assert skill is not None
    assert skill.source == SkillSource.USER
    assert skill.skill_md_path == user_skills / "web-access" / "SKILL.md"
    assert skill.references_dir is not None
    assert (skill.references_dir / "cdp-cheatsheet.md").is_file()
    assert (skill.references_dir / "site-patterns" / "github.com.md").is_file()
    assert bundled_skills_dir().is_dir()


def test_bundled_skill_bootstrap_never_overwrites_user_copy(tmp_path):
    user_skills = tmp_path / "user-skills"
    custom = _write_skill(user_skills, "web-access", body="custom user guidance")

    assert bootstrap_bundled_skills(user_skills) == ()
    assert "custom user guidance" in custom.read_text(encoding="utf-8")
