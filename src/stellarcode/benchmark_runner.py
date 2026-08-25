"""One-command runner for cleaned Aider Polyglot datasets."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from stellarcode.benchmark import BenchmarkOptions, run_benchmark_agent


@dataclass(frozen=True)
class RunnerOptions:
    clean_root: Path
    tests_root: Path
    results_dir: Path
    cases: tuple[str, ...]
    editable_files_by_case: dict[str, tuple[str, ...]]
    language: str
    tries: int
    max_iterations: int
    test_timeout_seconds: int

    @classmethod
    def parse(cls, argv: list[str]) -> "RunnerOptions":
        parser = argparse.ArgumentParser(
            prog="stellarcode benchmark-run",
            description="Run StellarCode and the language test suite for cleaned Polyglot cases.",
        )
        parser.add_argument("--clean-root", required=True, type=Path)
        parser.add_argument("--tests-root", required=True, type=Path)
        parser.add_argument("--results-dir", required=True, type=Path)
        parser.add_argument("--case", action="append", default=[])
        parser.add_argument("--language", choices=("python", "java", "cpp"), default=None)
        parser.add_argument("--tries", type=_positive_int, default=1)
        parser.add_argument("--max-iterations", type=_positive_int, default=30)
        parser.add_argument("--test-timeout-seconds", type=_positive_int, default=60)
        args = parser.parse_args(argv)

        clean_root = args.clean_root.resolve()
        tests_root = args.tests_root.resolve()
        if not clean_root.is_dir() or not tests_root.is_dir():
            parser.error("--clean-root and --tests-root must be existing directories")
        manifest = _load_manifest(clean_root)
        manifest_language = str(manifest.get("language") or "python").lower()
        language = args.language or manifest_language
        if language != manifest_language and manifest.get("language"):
            parser.error("--language does not match the clean dataset manifest")
        editable_files_by_case = {
            str(item["id"]): tuple(str(path) for path in item.get("editable_files", []))
            for item in manifest.get("cases", [])
            if isinstance(item, dict) and item.get("id")
        }
        available = sorted(editable_files_by_case) or sorted(
            path.name for path in clean_root.iterdir() if path.is_dir()
        )
        selected = tuple(args.case or available)
        missing = [case for case in selected if case not in available]
        if missing:
            parser.error("unknown cleaned case(s): " + ", ".join(missing))
        missing_tests = [case for case in selected if not (tests_root / case).is_dir()]
        if missing_tests:
            parser.error("missing private test case(s): " + ", ".join(missing_tests))
        return cls(
            clean_root=clean_root,
            tests_root=tests_root,
            results_dir=args.results_dir.resolve(),
            cases=selected,
            editable_files_by_case=editable_files_by_case,
            language=language,
            tries=args.tries,
            max_iterations=args.max_iterations,
            test_timeout_seconds=args.test_timeout_seconds,
        )


def run_benchmark(
    options: RunnerOptions,
    *,
    agent_runner: Callable[[BenchmarkOptions], dict[str, Any]] = run_benchmark_agent,
) -> dict[str, Any]:
    """Run all selected cases without mutating either source dataset."""

    options.results_dir.mkdir(parents=True, exist_ok=True)
    case_results = [
        _run_case(options, case_name, agent_runner)
        for case_name in options.cases
    ]
    passed_first_try = sum(item["attempts"][0]["passed"] for item in case_results)
    passed_any_try = sum(item["passed"] for item in case_results)
    agent_errors = sum(
        1
        for item in case_results
        if any(attempt["agent"].get("status") != "completed" for attempt in item["attempts"])
    )
    test_infrastructure_errors = sum(
        1
        for item in case_results
        if any(_is_test_infrastructure_error(attempt["pytest"]) for attempt in item["attempts"])
    )
    summary = {
        "benchmark": f"Aider Polyglot {_language_label(options.language)} / StellarCode Agent",
        "language": options.language,
        "case_count": len(case_results),
        "tries": options.tries,
        "valid": agent_errors == 0 and test_infrastructure_errors == 0,
        "agent_error_cases": agent_errors,
        "test_infrastructure_error_cases": test_infrastructure_errors,
        "pass_rate_1": _rate(passed_first_try, len(case_results)),
        "pass_rate_any_try": _rate(passed_any_try, len(case_results)),
        "passed_first_try": passed_first_try,
        "passed_any_try": passed_any_try,
        "cases": case_results,
    }
    _write_json(options.results_dir / "summary.json", summary)
    return summary


def _run_case(
    options: RunnerOptions,
    case_name: str,
    agent_runner: Callable[[BenchmarkOptions], dict[str, Any]],
) -> dict[str, Any]:
    clean_case = options.clean_root / case_name
    test_case = options.tests_root / case_name
    prompt_name = "PROMPT.md"
    prompt_file = clean_case / prompt_name
    if not prompt_file.is_file():
        raise ValueError(f"clean case has no {prompt_name}: {clean_case}")
    editable_files = options.editable_files_by_case.get(case_name) or _editable_files(
        clean_case,
        options.language,
    )
    case_results_dir = options.results_dir / "cases" / case_name
    attempts: list[dict[str, Any]] = []

    for attempt_number in range(1, options.tries + 1):
        attempt_dir = case_results_dir / f"attempt-{attempt_number}"
        work_dir = attempt_dir / "work"
        private_test_dir = attempt_dir / "private-tests"
        shutil.copytree(clean_case, work_dir)
        shutil.copytree(test_case, private_test_dir)
        _remove_build_artifacts(private_test_dir, options.language)

        agent_result_path = attempt_dir / "agent-result.json"
        agent_options = BenchmarkOptions(
            workspace=work_dir,
            prompt_file=work_dir / prompt_name,
            editable_files=editable_files,
            result_file=agent_result_path,
            max_iterations=options.max_iterations,
        )
        agent_result = agent_runner(agent_options)
        agent_completed = agent_result.get("status") == "completed"
        if agent_completed:
            _copy_agent_edits(work_dir, private_test_dir, editable_files)
            test_result = _run_tests(
                private_test_dir,
                options.language,
                options.test_timeout_seconds,
                case_name=case_name,
            )
        else:
            test_result = {
                "command": [sys.executable, "-m", "pytest", "-q"],
                "return_code": None,
                "timed_out": False,
                "duration_ms": 0,
                "output": "Skipped because the Agent did not start successfully.",
                "skipped": True,
            }
        attempt = {
            "attempt": attempt_number,
            "agent": agent_result,
            "pytest": test_result,
            "passed": agent_completed and test_result["return_code"] == 0 and not test_result["timed_out"],
        }
        attempts.append(attempt)
        _write_json(attempt_dir / "result.json", attempt)
        if attempt["passed"]:
            break

    return {"case": case_name, "passed": any(item["passed"] for item in attempts), "attempts": attempts}


def _editable_files(clean_case: Path, language: str) -> tuple[str, ...]:
    suffix = {"python": ".py", "java": ".java", "cpp": ".cpp"}[language]
    return tuple(
        path.relative_to(clean_case).as_posix()
        for path in clean_case.rglob("*")
        if path.suffix == suffix
        if path.is_file()
    )


def _copy_agent_edits(work_dir: Path, private_test_dir: Path, editable_files: tuple[str, ...]) -> None:
    for relative_path in editable_files:
        source = work_dir / relative_path
        destination = private_test_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _remove_build_artifacts(test_dir: Path, language: str) -> None:
    """Force tests to compile the Agent's source rather than reuse source-tree caches."""

    names = {
        "python": ("__pycache__", ".pytest_cache"),
        "java": ("build", "bin", ".gradle"),
        "cpp": ("build",),
    }[language]
    for name in names:
        target = test_dir / name
        if target.is_dir():
            shutil.rmtree(target)


def _run_tests(
    test_dir: Path,
    language: str,
    timeout_seconds: int,
    *,
    case_name: str | None = None,
) -> dict[str, Any]:
    if language == "cpp" and case_name:
        _set_cpp_exercise_name(test_dir, case_name)
    commands = _test_commands(language)
    started = time.monotonic()
    outputs: list[str] = []
    command_results: list[dict[str, Any]] = []
    try:
        for command in commands:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            completed = subprocess.run(
                command,
                cwd=test_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=remaining,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            command_results.append({"command": command, "return_code": completed.returncode})
            outputs.append("$ " + " ".join(command) + "\n" + output)
            if completed.returncode != 0:
                break
        return {
            "commands": command_results,
            "return_code": completed.returncode,
            "timed_out": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output": "\n".join(outputs)[-20_000:],
        }
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return {
            "commands": command_results,
            "return_code": None,
            "timed_out": True,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output": output[-20_000:],
        }
    except FileNotFoundError as exc:
        return {
            "commands": command_results,
            "return_code": None,
            "timed_out": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output": f"Required test tool is unavailable: {exc}",
            "infrastructure_error": True,
        }


def _test_commands(language: str) -> list[list[str]]:
    if language == "python":
        return [[sys.executable, "-m", "pytest", "-q"]]
    if language == "java":
        if sys.platform.startswith("win"):
            return [["cmd.exe", "/d", "/c", "gradlew.bat", "test", "--no-daemon"]]
        return [["./gradlew", "test", "--no-daemon"]]
    if language == "cpp":
        return [
            ["cmake", "-S", ".", "-B", "build"],
            ["cmake", "--build", "build"],
            ["ctest", "--test-dir", "build", "--output-on-failure"],
        ]
    raise ValueError(f"Unsupported benchmark language: {language}")


def _set_cpp_exercise_name(test_dir: Path, case_name: str) -> None:
    """Keep the upstream CMake project name when testing a renamed private copy."""

    cmake_lists = test_dir / "CMakeLists.txt"
    if not cmake_lists.is_file():
        return
    derived_name = "get_filename_component(exercise ${CMAKE_CURRENT_SOURCE_DIR} NAME)"
    contents = cmake_lists.read_text(encoding="utf-8")
    if derived_name in contents:
        cmake_lists.write_text(
            contents.replace(derived_name, f'set(exercise "{case_name}")'),
            encoding="utf-8",
        )


def _load_manifest(clean_root: Path) -> dict[str, Any]:
    manifest_path = clean_root / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid clean dataset manifest: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Clean dataset manifest must be a JSON object: {manifest_path}")
    return payload


def _rate(passed: int, total: int) -> float:
    return round((passed / total * 100) if total else 0.0, 2)


def _is_test_infrastructure_error(test_result: dict[str, Any]) -> bool:
    return bool(test_result.get("infrastructure_error"))


def _language_label(language: str) -> str:
    return {"cpp": "C++"}.get(language, language.capitalize())


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    try:
        options = RunnerOptions.parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return int(exc.code)
    summary = run_benchmark(options)
    print(json.dumps({key: value for key, value in summary.items() if key != "cases"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
