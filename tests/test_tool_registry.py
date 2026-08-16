from __future__ import annotations

import os
import sys

import pytest

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
    schemas = {
        tool["function"]["name"]: tool["function"]
        for tool in registry.schemas()
    }

    assert "web_search" in schemas
    assert schemas["web_search"]["parameters"]["required"] == ["query"]
    assert schemas["web_fetch"]["parameters"]["properties"]["max_chars"]["default"] == 8000
