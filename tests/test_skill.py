from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from stellarcode.agent import Agent
from stellarcode.multi_agent import AgentMessage, AgentOrchestrator, AgentRole, SubAgent
from stellarcode.skill import (
    BundledSkillManager,
    SkillContextBuffer,
    SkillRegistry,
    SkillSource,
    SkillStateStore,
    SkillUpgradeError,
    activate_skill_context,
    bootstrap_bundled_skills,
    bundled_skills_dir,
    explicit_skill_context,
    format_skill_index,
    handle_skill_command,
    parse_frontmatter,
    register_skill_tools,
    skill_tree_hash,
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


def _write_bundled_skill(
    root: Path,
    name: str,
    version: str,
    body: str,
    *,
    reference: str = "",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Bundled test skill\n"
        f'version: "{version}"\n'
        "author: StellarCode\n"
        "tags: [test]\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    if reference:
        references = skill_dir / "references"
        references.mkdir()
        (references / "guide.md").write_text(reference, encoding="utf-8")
    return skill_dir


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
    assert json.loads(store.file.read_text(encoding="utf-8")) == {"disabled": ["code-review"]}


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
    assert "Follow this guidance" in handle_skill_command("/skill show web-access", registry, store)


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


def test_load_skill_tool_injects_body_into_next_model_round_of_same_task(tmp_path):
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
            assert self.calls == 2
            assert messages[-2]["role"] == "tool"
            assert messages[-1]["role"] == "user"
            assert "## 已加载 Skill：web-access" in messages[-1]["content"]
            assert "Always inspect the page evidence." in messages[-1]["content"]
            assert "current task" in messages[-1]["content"]
            return {"role": "assistant", "content": "Used the loaded guidance."}

    client = SkillClient()
    agent = Agent(
        client,
        tools,
        skill_registry=registry,
        skill_context_buffer=buffer,
    )

    assert agent.run("读取网页") == "Used the loaded guidance."
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


def test_team_worker_receives_loaded_skill_in_same_assigned_step(tmp_path):
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "code-exploration", body="Inspect exact source before editing.")
    registry = SkillRegistry(skills_root, None)
    registry.reload()
    tools = ToolRegistry()
    register_skill_tools(tools, registry)

    class WorkerClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, tools=None, temperature=0.2, on_delta=None):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "load-code",
                            "type": "function",
                            "function": {
                                "name": "load_skill",
                                "arguments": '{"name":"code-exploration"}',
                            },
                        }
                    ],
                }
            assert "## 已加载 Skill：code-exploration" in messages[-1]["content"]
            assert "Inspect exact source before editing." in messages[-1]["content"]
            return {"role": "assistant", "content": "worker used skill"}

    worker = SubAgent(
        "worker",
        AgentRole.WORKER,
        WorkerClient(),
        tools,
        skill_registry=registry,
        skill_context_buffer=SkillContextBuffer(),
    )

    result = worker.execute(AgentMessage.task("lead", "inspect the code"))

    assert result.content == "worker used skill"


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


def test_bundled_workflow_skills_are_installed_into_user_skills(tmp_path):
    user_skills = tmp_path / "user-skills"
    assert bootstrap_bundled_skills(user_skills) == ()
    registry = SkillRegistry(user_skills, None)
    registry.reload()

    skill = registry.find_skill("web-access")
    code_skill = registry.find_skill("code-exploration")

    assert skill is not None
    assert skill.source == SkillSource.USER
    assert skill.skill_md_path == user_skills / "web-access" / "SKILL.md"
    assert skill.references_dir is not None
    assert (skill.references_dir / "cdp-cheatsheet.md").is_file()
    assert (skill.references_dir / "site-patterns" / "github.com.md").is_file()
    assert code_skill is not None
    assert code_skill.source == SkillSource.USER
    assert code_skill.skill_md_path == user_skills / "code-exploration" / "SKILL.md"
    assert "glob_files" in code_skill.body
    assert "grep_code" in code_skill.body
    assert "search_code" in code_skill.body
    assert "Runtime capability state" in code_skill.body
    assert bundled_skills_dir().is_dir()


def test_bundled_skill_bootstrap_never_overwrites_user_copy(tmp_path):
    user_skills = tmp_path / "user-skills"
    custom = _write_skill(user_skills, "web-access", body="custom user guidance")

    assert bootstrap_bundled_skills(user_skills) == ()
    assert "custom user guidance" in custom.read_text(encoding="utf-8")


def test_bundled_skill_records_hash_and_auto_upgrades_only_clean_copy(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    source = _write_bundled_skill(
        bundled,
        "review",
        "1.0.0",
        "Review version one.",
        reference="reference one",
    )
    store = SkillStateStore(tmp_path / "skills.json")
    manager = BundledSkillManager(user, store, bundled)

    assert manager.bootstrap() == ()
    first_hash = skill_tree_hash(source)
    assert skill_tree_hash(user / "review") == first_hash
    assert store.bundled_records()["review"]["installed_hash"] == first_hash

    (source / "SKILL.md").write_text(
        (source / "SKILL.md")
        .read_text(encoding="utf-8")
        .replace('version: "1.0.0"', 'version: "2.0.0"')
        .replace("Review version one.", "Review version two."),
        encoding="utf-8",
    )
    (source / "references" / "guide.md").write_text(
        "reference two",
        encoding="utf-8",
    )

    assert manager.bootstrap() == ()
    status = manager.status("review")
    assert status["upgrade_state"] == "current"
    assert status["builtin_version"] == "2.0.0"
    assert status["current_hash"] == skill_tree_hash(source)
    assert "Review version two." in (user / "review" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_custom_bundled_skill_is_preserved_and_can_acknowledge_update(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    source = _write_bundled_skill(bundled, "review", "1.0.0", "Version one.")
    store = SkillStateStore(tmp_path / "skills.json")
    manager = BundledSkillManager(user, store, bundled)
    manager.bootstrap()
    installed = user / "review" / "SKILL.md"
    installed.write_text(
        installed.read_text(encoding="utf-8") + "\nUser customization.\n",
        encoding="utf-8",
    )
    (source / "SKILL.md").write_text(
        (source / "SKILL.md")
        .read_text(encoding="utf-8")
        .replace('version: "1.0.0"', 'version: "2.0.0"')
        .replace("Version one.", "Version two."),
        encoding="utf-8",
    )

    manager.bootstrap()
    status = manager.status("review")
    assert status["upgrade_state"] == "update_available"
    assert status["customized"] is True
    assert "User customization." in installed.read_text(encoding="utf-8")

    diff = manager.diff("review")
    kept = manager.keep_custom(
        "review",
        expected_current_hash=diff["current_hash"],
        expected_builtin_hash=diff["builtin_hash"],
    )
    assert kept["upgrade_state"] == "custom_kept"
    assert "User customization." in installed.read_text(encoding="utf-8")

    (source / "SKILL.md").write_text(
        (source / "SKILL.md")
        .read_text(encoding="utf-8")
        .replace('version: "2.0.0"', 'version: "3.0.0"'),
        encoding="utf-8",
    )
    assert manager.status("review")["upgrade_state"] == "update_available"


def test_bundled_skill_diff_update_restore_and_stale_hash_protection(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    source = _write_bundled_skill(bundled, "review", "1.0.0", "Default guidance.")
    store = SkillStateStore(tmp_path / "skills.json")
    manager = BundledSkillManager(user, store, bundled)
    manager.bootstrap()
    installed = user / "review" / "SKILL.md"
    installed.write_text(
        installed.read_text(encoding="utf-8") + "\nCustom guidance.\n",
        encoding="utf-8",
    )
    (source / "SKILL.md").write_text(
        (source / "SKILL.md")
        .read_text(encoding="utf-8")
        .replace('version: "1.0.0"', 'version: "2.0.0"'),
        encoding="utf-8",
    )

    preview = manager.diff("review")
    assert "current/SKILL.md" in preview["diff"]
    installed.write_text(
        installed.read_text(encoding="utf-8") + "Changed after preview.\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillUpgradeError, match="changed after the preview"):
        manager.update(
            "review",
            expected_current_hash=preview["current_hash"],
            expected_builtin_hash=preview["builtin_hash"],
        )

    preview = manager.diff("review")
    updated = manager.update(
        "review",
        expected_current_hash=preview["current_hash"],
        expected_builtin_hash=preview["builtin_hash"],
    )
    assert updated["upgrade_state"] == "current"
    assert skill_tree_hash(user / "review") == skill_tree_hash(source)

    installed.write_text(
        installed.read_text(encoding="utf-8") + "\nAnother customization.\n",
        encoding="utf-8",
    )
    preview = manager.diff("review")
    restored = manager.restore_default(
        "review",
        expected_current_hash=preview["current_hash"],
        expected_builtin_hash=preview["builtin_hash"],
    )
    assert restored["upgrade_state"] == "current"
    assert "Another customization." not in installed.read_text(encoding="utf-8")


def test_bundled_skill_update_rolls_back_when_baseline_write_fails(
    tmp_path,
    monkeypatch,
):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    source = _write_bundled_skill(bundled, "review", "1.0.0", "Default guidance.")
    store = SkillStateStore(tmp_path / "skills.json")
    manager = BundledSkillManager(user, store, bundled)
    manager.bootstrap()
    installed = user / "review" / "SKILL.md"
    installed.write_text(
        installed.read_text(encoding="utf-8") + "\nImportant customization.\n",
        encoding="utf-8",
    )
    (source / "SKILL.md").write_text(
        (source / "SKILL.md")
        .read_text(encoding="utf-8")
        .replace('version: "1.0.0"', 'version: "2.0.0"'),
        encoding="utf-8",
    )
    preview = manager.diff("review")
    previous_hash = skill_tree_hash(user / "review")
    monkeypatch.setattr(store, "set_bundled_record", lambda _name, _record: False)

    with pytest.raises(SkillUpgradeError, match="persist bundled Skill state"):
        manager.update(
            "review",
            expected_current_hash=preview["current_hash"],
            expected_builtin_hash=preview["builtin_hash"],
        )

    assert skill_tree_hash(user / "review") == previous_hash
    assert "Important customization." in installed.read_text(encoding="utf-8")


def test_large_same_size_skill_files_produce_hash_diff(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    source = _write_bundled_skill(bundled, "review", "1.0.0", "Default.")
    payload_size = 2 * 1024 * 1024 + 1
    (source / "large.txt").write_bytes(b"a" * payload_size)
    store = SkillStateStore(tmp_path / "skills.json")
    manager = BundledSkillManager(user, store, bundled)
    manager.bootstrap()
    (user / "review" / "large.txt").write_bytes(b"b" * payload_size)

    preview = manager.diff("review")

    assert "No differences." not in preview["diff"]
    assert "sha256=" in preview["diff"]


def test_bundled_skill_rejects_frontmatter_name_mismatch(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    source = _write_bundled_skill(bundled, "review", "1.0.0", "Default.")
    skill_md = source / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8").replace("name: review", "name: other"),
        encoding="utf-8",
    )
    manager = BundledSkillManager(user, SkillStateStore(tmp_path / "skills.json"), bundled)

    warnings = manager.bootstrap()

    assert warnings
    assert "must match its directory" in warnings[0]
    assert not (user / "review").exists()


def test_skill_reload_installs_new_bundled_workflow_skills(tmp_path):
    user_skills = tmp_path / "user-skills"
    store = SkillStateStore(tmp_path / "skills.json")
    registry = SkillRegistry(user_skills, None, store)
    registry.reload()

    result = handle_skill_command("/skill reload", registry, store)

    assert "Skills reloaded" in result
    assert registry.find_skill("web-access") is not None
    assert registry.find_skill("code-exploration") is not None
