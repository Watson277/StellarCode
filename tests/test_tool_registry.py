from __future__ import annotations

import os
import sys

import pytest

from stellarcode.tools import code_search
from stellarcode.task_workspace import task_workspace_scope
from stellarcode.tools import ToolExecutionError, build_default_registry


def test_read_and_write_file(tmp_path):
    registry = build_default_registry(tmp_path)

    write_result = registry.execute(
        "write_file",
        {"path": "notes/hello.txt", "content": "hello from pai"},
    )
    read_result = registry.execute("read_file", {"path": "notes/hello.txt"})

    assert "Wrote 14 characters" in write_result
    assert read_result == "hello from pai"


def test_file_tools_allow_paths_outside_working_directory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    registry = build_default_registry(workspace)

    write_result = registry.execute(
        "write_file",
        {"path": "../outside/secret.txt", "content": "available"},
    )
    read_result = registry.execute(
        "read_file",
        {"path": str(outside / "secret.txt")},
    )
    listing = registry.execute("list_dir", {"path": str(outside)})
    delete_result = registry.execute(
        "delete_file",
        {"path": "../outside/secret.txt"},
    )

    assert str(outside / "secret.txt") in write_result
    assert read_result == "available"
    assert str(outside / "secret.txt") in listing
    assert str(outside / "secret.txt") in delete_result
    assert not (outside / "secret.txt").exists()


def test_task_workspace_scope_routes_relative_and_project_absolute_paths(tmp_path):
    project = tmp_path / "project"
    isolated = tmp_path / "task-worktree"
    project.mkdir()
    isolated.mkdir()
    registry = build_default_registry(project)

    with task_workspace_scope(project, isolated):
        registry.execute("write_file", {"path": "relative.txt", "content": "isolated"})
        registry.execute(
            "write_file",
            {"path": str(project / "absolute.txt"), "content": "remapped"},
        )
        glob_result = registry.execute("glob_files", {"pattern": "*.txt"})
        grep_result = registry.execute(
            "grep_code",
            {"pattern": "remapped", "glob": "*.txt"},
        )
        command = registry.execute(
            "execute_command",
            {
                "command": [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import sys; "
                        "Path('command.txt').write_text('task'); "
                        "Path(sys.argv[1]).write_text('command remapped')"
                    ),
                    str(project / "command-absolute.txt"),
                ]
            },
        )

    assert "exit_code: 0" in command
    assert "relative.txt" in glob_result
    assert "absolute.txt:1" in grep_result
    assert not (project / "relative.txt").exists()
    assert not (project / "absolute.txt").exists()
    assert not (project / "command.txt").exists()
    assert not (project / "command-absolute.txt").exists()
    assert (isolated / "relative.txt").read_text(encoding="utf-8") == "isolated"
    assert (isolated / "absolute.txt").read_text(encoding="utf-8") == "remapped"
    assert (isolated / "command.txt").read_text(encoding="utf-8") == "task"
    assert (isolated / "command-absolute.txt").read_text(encoding="utf-8") == ("command remapped")


@pytest.mark.parametrize(
    "command",
    [
        'Start-Process -FilePath "npm" -ArgumentList "run", "dev"',
        "Start-Job { npm run dev }",
        "nohup npm run dev &",
        'start "" /b npm run dev',
    ],
)
def test_isolated_execute_command_rejects_detached_processes(tmp_path, command):
    project = tmp_path / "project"
    isolated = tmp_path / "task-worktree"
    project.mkdir()
    isolated.mkdir()
    registry = build_default_registry(project)

    with task_workspace_scope(project, isolated):
        with pytest.raises(ToolExecutionError, match="Detached/background commands"):
            registry.execute("execute_command", {"command": command})


def test_nonisolated_execute_command_keeps_background_syntax_compatibility(
    monkeypatch,
    tmp_path,
):
    registry = build_default_registry(tmp_path)
    captured: dict[str, object] = {}

    class CompletedProcess:
        returncode = 0

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            return "ok", ""

        def poll(self):
            return self.returncode

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return CompletedProcess()

    monkeypatch.setattr("stellarcode.tools.builtin.subprocess.Popen", fake_popen)

    result = registry.execute(
        "execute_command",
        {"command": 'Start-Process -FilePath "demo.exe"'},
    )

    assert "exit_code: 0" in result
    assert captured["argv"]


def test_isolated_builtin_write_rejects_paths_excluded_from_terminal_merge(tmp_path):
    project = tmp_path / "project"
    isolated = tmp_path / "task-worktree"
    project.mkdir()
    isolated.mkdir()
    registry = build_default_registry(project)

    with task_workspace_scope(project, isolated):
        with pytest.raises(ToolExecutionError, match="excluded from task Git worktrees"):
            registry.execute("write_file", {"path": ".env", "content": "TOKEN=secret"})
        with pytest.raises(ToolExecutionError, match="excluded from task Git worktrees"):
            registry.execute(
                "write_file",
                {"path": "node_modules/generated.js", "content": "generated"},
            )

    assert not (isolated / ".env").exists()
    assert not (isolated / "node_modules").exists()


def test_list_dir_and_delete_file(tmp_path):
    registry = build_default_registry(tmp_path)
    folder = tmp_path / "notes"
    folder.mkdir()
    target = folder / "obsolete.txt"
    target.write_text("remove me", encoding="utf-8")

    listing = registry.execute("list_dir", {"path": "notes"})
    delete_result = registry.execute("delete_file", {"path": "notes/obsolete.txt"})

    assert "[file] notes\\obsolete.txt" in listing or "[file] notes/obsolete.txt" in listing
    assert "Deleted file" in delete_result
    assert not target.exists()


def test_glob_files_finds_candidates_and_skips_generated_directories(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(code_search.shutil, "which", lambda _name: None)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (tmp_path / "src" / "app.ts").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "hidden.py").write_text("hidden\n", encoding="utf-8")
    registry = build_default_registry(tmp_path)

    result = registry.execute("glob_files", {"pattern": "**/*.py"})

    assert "src/app.py" in result
    assert "hidden.py" not in result
    assert "engine=python" in result


def test_grep_code_supports_literal_filters_context_and_regex(monkeypatch, tmp_path):
    monkeypatch.setattr(code_search.shutil, "which", lambda _name: None)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "agent.py").write_text(
        "before\nclass StellarAgent:\n    pass\nafter\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "agent.md").write_text(
        "class StellarAgent:\n",
        encoding="utf-8",
    )
    registry = build_default_registry(tmp_path)

    literal = registry.execute(
        "grep_code",
        {
            "pattern": "stellaragent",
            "glob": "**/*.py",
            "case_sensitive": False,
            "context_lines": 1,
        },
    )
    regex = registry.execute(
        "grep_code",
        {"pattern": r"class\s+StellarAgent", "regex": True},
    )

    assert "src/agent.py:2" in literal
    assert "agent.md" not in literal
    assert "1 | before" in literal
    assert "suggested_reads:" in literal
    assert "src/agent.py:2" in regex


def test_grep_code_rejects_invalid_regex(monkeypatch, tmp_path):
    monkeypatch.setattr(code_search.shutil, "which", lambda _name: None)
    registry = build_default_registry(tmp_path)

    with pytest.raises(ToolExecutionError, match="invalid regular expression"):
        registry.execute("grep_code", {"pattern": "[", "regex": True})


def test_apply_patch_applies_ordered_exact_edits_and_preserves_crlf(tmp_path):
    target = tmp_path / "module.py"
    target.write_bytes(b"def first():\r\n    return 1\r\n\r\ndef second():\r\n    return 2\r\n")
    registry = build_default_registry(tmp_path)

    result = registry.execute(
        "apply_patch",
        {
            "path": "module.py",
            "edits": [
                {"old_text": "return 1", "new_text": "return 10"},
                {"old_text": "def second():", "new_text": "def renamed():"},
            ],
        },
    )

    content = target.read_bytes()
    assert "Patched module.py with 2 edit(s)" in result
    assert b"return 10" in content
    assert b"def renamed():" in content
    assert b"\r\n" in content
    assert b"\n" not in content.replace(b"\r\n", b"")


def test_apply_patch_requires_unique_match_unless_replace_all(tmp_path):
    target = tmp_path / "values.txt"
    target.write_text("old\nold\n", encoding="utf-8")
    registry = build_default_registry(tmp_path)

    with pytest.raises(ToolExecutionError, match="matched 2 locations"):
        registry.execute(
            "apply_patch",
            {"path": "values.txt", "edits": [{"old_text": "old", "new_text": "new"}]},
        )

    registry.execute(
        "apply_patch",
        {
            "path": "values.txt",
            "edits": [{"old_text": "old", "new_text": "new", "replace_all": True}],
        },
    )
    assert target.read_text(encoding="utf-8") == "new\nnew\n"


def test_apply_patch_refuses_missing_and_binary_files(tmp_path):
    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"before\x00after")
    registry = build_default_registry(tmp_path)

    with pytest.raises(ToolExecutionError, match="only edits existing files"):
        registry.execute(
            "apply_patch",
            {"path": "missing.txt", "edits": [{"old_text": "a", "new_text": "b"}]},
        )
    with pytest.raises(ToolExecutionError, match="does not support binary"):
        registry.execute(
            "apply_patch",
            {"path": "binary.dat", "edits": [{"old_text": "a", "new_text": "b"}]},
        )


def test_delete_file_rejects_directories_and_missing_files(tmp_path):
    registry = build_default_registry(tmp_path)
    (tmp_path / "folder").mkdir()

    with pytest.raises(ToolExecutionError, match="not a file"):
        registry.execute("delete_file", {"path": "folder"})
    with pytest.raises(ToolExecutionError, match="File not found"):
        registry.execute("delete_file", {"path": "missing.txt"})


def test_delete_file_accepts_an_absolute_path_inside_workspace(tmp_path):
    registry = build_default_registry(tmp_path)
    target = tmp_path / "absolute.txt"
    target.write_text("remove me", encoding="utf-8")

    result = registry.execute("delete_file", {"path": str(target)})

    assert result == "Deleted file: absolute.txt"
    assert not target.exists()


def test_execute_command_uses_workspace(tmp_path):
    registry = build_default_registry(tmp_path)

    result = registry.execute("execute_command", {"command": ["python", "--version"]})

    assert "exit_code: 0" in result
    assert "Python" in result


@pytest.mark.skipif(os.name != "nt", reason="Windows verbatim paths only")
def test_execute_command_removes_windows_verbatim_workspace_prefix(tmp_path):
    verbatim_workspace = rf"\\?\{tmp_path}"
    registry = build_default_registry(verbatim_workspace)

    result = registry.execute(
        "execute_command",
        {"command": "Write-Output (Get-Location).Path"},
    )

    assert "exit_code: 0" in result
    assert "\\\\?\\" not in result
    assert str(tmp_path) in result


def test_execute_command_child_stdin_is_closed(tmp_path):
    registry = build_default_registry(tmp_path)

    result = registry.execute(
        "execute_command",
        {
            "command": [
                sys.executable,
                "-c",
                "import sys; print('stdin-bytes=' + str(len(sys.stdin.buffer.read())))",
            ],
            "timeout_seconds": 5,
        },
    )

    assert "exit_code: 0" in result
    assert "stdin-bytes=0" in result


def test_execute_command_restores_user_pythonpath(monkeypatch, tmp_path):
    runtime_path = str(tmp_path / "runtime-src")
    user_path = str(tmp_path / "user-src")
    monkeypatch.setenv("PYTHONPATH", runtime_path)
    monkeypatch.setenv("STELLARCODE_RUNTIME_PYTHONPATH", runtime_path)
    monkeypatch.setenv("STELLARCODE_TOOL_PYTHONPATH", user_path)
    registry = build_default_registry(tmp_path)

    result = registry.execute(
        "execute_command",
        {
            "command": [
                sys.executable,
                "-c",
                (
                    "import os; "
                    "print('pythonpath=' + str(os.getenv('PYTHONPATH'))); "
                    "print('runtime-marker=' + str(os.getenv('STELLARCODE_RUNTIME_PYTHONPATH'))); "
                    "print('tool-marker=' + str(os.getenv('STELLARCODE_TOOL_PYTHONPATH')))"
                ),
            ]
        },
    )

    assert f"pythonpath={user_path}" in result
    assert "runtime-marker=None" in result
    assert "tool-marker=None" in result


def test_execute_command_string_uses_platform_shell(tmp_path):
    registry = build_default_registry(tmp_path)
    target = tmp_path / "shell-delete.txt"
    target.write_text("remove me", encoding="utf-8")
    command = "Remove-Item shell-delete.txt" if os.name == "nt" else "rm shell-delete.txt"

    result = registry.execute("execute_command", {"command": command})

    assert "exit_code: 0" in result
    assert not target.exists()


def test_execute_command_truncates_very_large_output(tmp_path):
    registry = build_default_registry(tmp_path)
    command = ["python", "-c", "print('x' * 12000)"]

    result = registry.execute("execute_command", {"command": command})

    assert len(result) == 8000
    assert "command output truncated" in result
    assert "original length" in result


def test_registry_exposes_web_search_and_web_fetch_schemas(tmp_path):
    registry = build_default_registry(tmp_path)
    schemas = {tool["function"]["name"]: tool["function"] for tool in registry.schemas()}

    assert "web_search" in schemas
    assert schemas["web_search"]["parameters"]["required"] == ["query"]
    assert schemas["web_fetch"]["parameters"]["properties"]["max_chars"]["default"] == 8000


def test_registry_exposes_exact_code_tools_and_structured_patch_schema(tmp_path):
    registry = build_default_registry(tmp_path)
    schemas = {tool["function"]["name"]: tool["function"] for tool in registry.schemas()}

    assert schemas["glob_files"]["parameters"]["required"] == ["pattern"]
    assert schemas["grep_code"]["parameters"]["properties"]["regex"]["default"] is False
    patch_parameters = schemas["apply_patch"]["parameters"]
    assert patch_parameters["required"] == ["path", "edits"]
    assert patch_parameters["properties"]["edits"]["items"]["required"] == [
        "old_text",
        "new_text",
    ]


def test_tool_descriptions_do_not_embed_cross_tool_workflows(tmp_path):
    schemas = {
        tool["function"]["name"]: tool["function"]
        for tool in build_default_registry(tmp_path).schemas()
    }

    assert "grep_code" not in schemas["glob_files"]["description"]
    assert "read_file" not in schemas["glob_files"]["description"]
    assert "search_code" not in schemas["grep_code"]["description"]
    assert "read_file" not in schemas["apply_patch"]["description"]
    assert "grep_code" not in schemas["apply_patch"]["description"]
    assert "web_fetch" not in schemas["web_search"]["description"]
    assert "glob_files" not in schemas["search_code"]["description"]
    assert "grep_code" not in schemas["search_code"]["description"]
