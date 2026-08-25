from __future__ import annotations

import json

import pytest

from stellarcode.benchmark import (
    BenchmarkOptions,
    build_benchmark_registry,
    run_benchmark_agent,
)
from stellarcode.benchmark_runner import (
    RunnerOptions,
    _is_test_infrastructure_error,
    _set_cpp_exercise_name,
    _test_commands,
    run_benchmark,
)
from stellarcode.tools.registry import ToolExecutionError


def test_benchmark_options_require_a_clean_workspace_contract(tmp_path):
    source = tmp_path / "exercise.py"
    source.write_text("def solve():\n    pass\n", encoding="utf-8")
    prompt = tmp_path / "PROMPT.md"
    prompt.write_text("Implement solve.", encoding="utf-8")

    options = BenchmarkOptions.parse(
        [
            "--workspace",
            str(tmp_path),
            "--prompt-file",
            str(prompt),
            "--editable-file",
            "exercise.py",
            "--result-file",
            str(tmp_path.parent / "result.json"),
        ]
    )

    assert options.workspace == tmp_path.resolve()
    assert options.editable_files == ("exercise.py",)


def test_benchmark_registry_limits_tools_paths_and_writes(tmp_path):
    source = tmp_path / "exercise.py"
    source.write_text("value = 1\n", encoding="utf-8")
    protected = tmp_path / "PROMPT.md"
    protected.write_text("task", encoding="utf-8")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("secret", encoding="utf-8")
    registry = build_benchmark_registry(tmp_path, ("exercise.py",))

    assert {item["function"]["name"] for item in registry.schemas()} == {
        "read_file",
        "write_file",
        "apply_patch",
        "list_dir",
        "glob_files",
        "grep_code",
    }
    assert registry.execute("read_file", {"path": "exercise.py"}) == "value = 1\n"

    with pytest.raises(ToolExecutionError, match="outside the workspace"):
        registry.execute("read_file", {"path": str(outside)})
    with pytest.raises(ToolExecutionError, match="declared editable files"):
        registry.execute("write_file", {"path": "PROMPT.md", "content": "changed"})

    registry.execute("apply_patch", {"path": "exercise.py", "edits": [{"old_text": "1", "new_text": "2"}]})
    assert source.read_text(encoding="utf-8") == "value = 2\n"


def test_benchmark_agent_writes_machine_readable_result(tmp_path):
    source = tmp_path / "exercise.py"
    source.write_text("value = 1\n", encoding="utf-8")
    prompt = tmp_path / "PROMPT.md"
    prompt.write_text("Return a short summary.", encoding="utf-8")
    result_path = tmp_path.parent / "result.json"
    options = BenchmarkOptions.parse(
        [
            "--workspace",
            str(tmp_path),
            "--prompt-file",
            str(prompt),
            "--editable-file",
            "exercise.py",
            "--result-file",
            str(result_path),
        ]
    )

    class FakeClient:
        provider_name = "fake"
        model = "test"

        def chat(self, messages, *, tools, temperature):
            return {"content": "done"}

    result = run_benchmark_agent(options, client_factory=FakeClient)
    saved = json.loads(result_path.read_text(encoding="utf-8"))

    assert result["status"] == "completed"
    assert saved["termination"] == "agent_finished"
    assert saved["changed_files"] == []
    assert saved["allowed_tools"] == sorted(saved["allowed_tools"])


def test_one_click_runner_copies_agent_edits_to_private_tests_only(tmp_path):
    clean_root = tmp_path / "clean"
    tests_root = tmp_path / "tests"
    clean_case = clean_root / "example"
    test_case = tests_root / "example"
    clean_case.mkdir(parents=True)
    test_case.mkdir(parents=True)
    (clean_case / "PROMPT.md").write_text("Implement.", encoding="utf-8")
    (clean_case / "exercise.py").write_text("value = 1\n", encoding="utf-8")
    (test_case / "exercise.py").write_text("value = 1\n", encoding="utf-8")
    (test_case / "exercise_test.py").write_text(
        "from exercise import value\n\ndef test_value():\n    assert value == 2\n",
        encoding="utf-8",
    )
    options = RunnerOptions.parse(
        [
            "--clean-root", str(clean_root),
            "--tests-root", str(tests_root),
            "--results-dir", str(tmp_path / "results"),
        ]
    )

    def fake_agent(agent_options):
        (agent_options.workspace / "exercise.py").write_text("value = 2\n", encoding="utf-8")
        return {"status": "completed"}

    summary = run_benchmark(options, agent_runner=fake_agent)

    assert summary["pass_rate_1"] == 100.0
    assert (test_case / "exercise.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (tmp_path / "results" / "summary.json").is_file()


def test_one_click_runner_marks_agent_startup_errors_as_invalid(tmp_path):
    clean_root = tmp_path / "clean"
    tests_root = tmp_path / "tests"
    clean_case = clean_root / "example"
    test_case = tests_root / "example"
    clean_case.mkdir(parents=True)
    test_case.mkdir(parents=True)
    (clean_case / "PROMPT.md").write_text("Implement.", encoding="utf-8")
    (clean_case / "exercise.py").write_text("value = 1\n", encoding="utf-8")
    (test_case / "exercise.py").write_text("value = 1\n", encoding="utf-8")
    options = RunnerOptions.parse(
        [
            "--clean-root", str(clean_root),
            "--tests-root", str(tests_root),
            "--results-dir", str(tmp_path / "results"),
        ]
    )

    summary = run_benchmark(options, agent_runner=lambda _options: {"status": "error"})

    assert not summary["valid"]
    assert summary["agent_error_cases"] == 1
    assert not summary["cases"][0]["passed"]
    assert summary["cases"][0]["attempts"][0]["pytest"]["skipped"]


def test_test_infrastructure_error_is_distinguished_from_a_failed_solution():
    assert _is_test_infrastructure_error({"infrastructure_error": True})
    assert not _is_test_infrastructure_error({"return_code": 1, "timed_out": False})


def test_cpp_runner_preserves_the_exercise_name_in_a_private_test_copy(tmp_path):
    cmake_lists = tmp_path / "CMakeLists.txt"
    cmake_lists.write_text(
        "get_filename_component(exercise ${CMAKE_CURRENT_SOURCE_DIR} NAME)\n",
        encoding="utf-8",
    )

    _set_cpp_exercise_name(tmp_path, "all-your-base")

    assert cmake_lists.read_text(encoding="utf-8") == 'set(exercise "all-your-base")\n'


def test_java_test_command_uses_cmd_to_run_the_gradle_wrapper_on_windows(monkeypatch):
    monkeypatch.setattr("stellarcode.benchmark_runner.sys.platform", "win32")

    assert _test_commands("java") == [["cmd.exe", "/d", "/c", "gradlew.bat", "test", "--no-daemon"]]


def test_runner_discards_java_build_caches_before_private_test(tmp_path):
    clean_root = tmp_path / "clean"
    tests_root = tmp_path / "tests"
    clean_case = clean_root / "example"
    test_case = tests_root / "example"
    clean_case.mkdir(parents=True)
    (clean_case / "PROMPT.md").write_text("Implement.", encoding="utf-8")
    (clean_case / "src" / "main" / "java").mkdir(parents=True)
    (clean_case / "src" / "main" / "java" / "Example.java").write_text("class Example {}", encoding="utf-8")
    test_case.mkdir(parents=True)
    (test_case / "src" / "main" / "java").mkdir(parents=True)
    (test_case / "src" / "main" / "java" / "Example.java").write_text("class Example {}", encoding="utf-8")
    (test_case / "build").mkdir()
    (test_case / "bin").mkdir()
    (test_case / ".gradle").mkdir()
    manifest = {"language": "java", "cases": [{"id": "example", "editable_files": ["src/main/java/Example.java"]}]}
    (clean_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    options = RunnerOptions.parse(["--clean-root", str(clean_root), "--tests-root", str(tests_root), "--results-dir", str(tmp_path / "results")])

    def fake_agent(agent_options):
        private_test = agent_options.workspace.parent / "private-tests"
        return {"status": "error", "private_test_exists": private_test.exists()}

    summary = run_benchmark(options, agent_runner=fake_agent)
    private_test_dir = tmp_path / "results" / "cases" / "example" / "attempt-1" / "private-tests"

    assert not (private_test_dir / "build").exists()
    assert not (private_test_dir / "bin").exists()
    assert not (private_test_dir / ".gradle").exists()
    assert not summary["valid"]
