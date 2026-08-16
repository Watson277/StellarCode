from __future__ import annotations

import json
import os
import sys
import threading
from io import BytesIO
from pathlib import Path

import pytest

from stellarcode.diagnostics.service import (
    CommandResult,
    DiagnosticsService,
    SafeCommandRunner,
    _diagnostic_environment,
    _parse_cargo_diagnostics,
    _parse_ruff_diagnostics,
    _parse_tsc_diagnostics,
)
from stellarcode.diagnostics.lsp import (
    JsonRpcFramer,
    LspConfig,
    LspProtocolError,
    PythonLspClient,
)


def test_python_syntax_diagnostics_are_persisted_and_restored(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (workspace / "valid.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    storage = tmp_path / "state"

    service = DiagnosticsService(workspace, storage, sys.executable)
    result = service.run("syntax")

    assert result["status"] == "completed"
    assert result["counts"] == {"error": 1, "warning": 0, "info": 0, "total": 1}
    assert result["diagnostics"][0]["relative_path"] == "broken.py"
    assert result["diagnostics"][0]["code"] == "PYTHON_SYNTAX"
    assert result["files_scanned"] == 2
    assert result["detected_projects"][0]["markers"] == ["pyproject.toml"]

    on_disk = json.loads((storage / "snapshot.json").read_text(encoding="utf-8"))
    assert on_disk == result
    restored = DiagnosticsService(workspace, storage, sys.executable).snapshot()
    assert restored["run_id"] == result["run_id"]
    assert restored["diagnostics"] == result["diagnostics"]
    assert restored["stale"] is True


def test_diagnostic_run_uses_caller_supplied_id_for_snapshot_and_problems(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    result = DiagnosticsService(workspace, tmp_path / "state").run(
        "syntax", run_id="diagnostics-desktop-123"
    )

    assert result["run_id"] == "diagnostics-desktop-123"
    assert result["diagnostics"][0]["run_id"] == "diagnostics-desktop-123"


def test_diagnostic_environment_allowlists_build_inputs_without_credentials(
    monkeypatch,
):
    monkeypatch.setenv("PATH", r"C:\tools")
    monkeypatch.setenv("CARGO_HOME", r"E:\cargo")
    monkeypatch.setenv("RUSTUP_HOME", r"E:\rustup")
    monkeypatch.setenv("GLM_API_KEY", "glm-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("CARGO_REGISTRIES_PRIVATE_TOKEN", "cargo-secret")
    monkeypatch.setenv("MY_SERVICE_PASSWORD", "password")
    monkeypatch.setenv("PYTHONPATH", r"E:\runtime")

    environment = _diagnostic_environment()

    assert environment["PATH"] == r"C:\tools"
    assert environment["CARGO_HOME"] == r"E:\cargo"
    assert environment["RUSTUP_HOME"] == r"E:\rustup"
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert "GLM_API_KEY" not in environment
    assert "DEEPSEEK_API_KEY" not in environment
    assert "CARGO_REGISTRIES_PRIVATE_TOKEN" not in environment
    assert "MY_SERVICE_PASSWORD" not in environment
    assert "PYTHONPATH" not in environment


def test_syntax_scan_honors_pep263_and_ignores_generated_directories(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    latin = b"# -*- coding: latin-1 -*-\nname = 'caf\xe9'\n"
    (workspace / "latin.py").write_bytes(latin)
    generated = workspace / ".venv" / "Lib"
    generated.mkdir(parents=True)
    (generated / "broken.py").write_text("if:\n", encoding="utf-8")

    result = DiagnosticsService(workspace, tmp_path / "state").run("syntax")

    assert result["status"] == "completed"
    assert result["files_scanned"] == 1
    assert result["diagnostics"] == []


def test_syntax_scan_rejects_context_sensitive_top_level_return(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "broken.py").write_text("return\n", encoding="utf-8")

    result = DiagnosticsService(workspace, tmp_path / "state").run("syntax")

    assert result["counts"]["error"] == 1
    assert "outside function" in result["diagnostics"][0]["message"]


def test_source_discovery_does_not_follow_directory_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "broken.py").write_text("def outside(:\n", encoding="utf-8")
    try:
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available")

    result = DiagnosticsService(workspace, tmp_path / "state").run("syntax")

    assert result["files_scanned"] == 0
    assert result["diagnostics"] == []


def test_cancelled_run_persists_a_terminal_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "demo.py").write_text("value = 1\n", encoding="utf-8")
    cancelled = threading.Event()
    cancelled.set()

    result = DiagnosticsService(workspace, tmp_path / "state").run(
        "syntax", cancel_event=cancelled
    )

    assert result["status"] == "cancelled"
    assert result["error"] == "Diagnostic run cancelled."
    assert json.loads((tmp_path / "state" / "snapshot.json").read_text(encoding="utf-8"))[
        "status"
    ] == "cancelled"


def test_provider_snapshot_always_reports_syntax_and_explicit_ruff_state(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    snapshot = DiagnosticsService(
        workspace,
        tmp_path / "state",
        python_executable=workspace / "missing-python",
    ).snapshot()

    providers = {provider["id"]: provider for provider in snapshot["providers"]}
    assert providers["python-syntax"]["available"] is True
    assert providers["ruff"]["available"] is False
    assert providers["ruff"]["reason"]


def test_lsp_framer_uses_utf8_byte_length_and_rejects_oversized_payload():
    stream = BytesIO()
    JsonRpcFramer.write(
        stream,
        {"jsonrpc": "2.0", "method": "window/logMessage", "params": {"message": "中文"}},
    )
    stream.seek(0)

    assert JsonRpcFramer.read(stream)["params"]["message"] == "中文"

    oversized = BytesIO(b"Content-Length: 999999999\r\n\r\n")
    with pytest.raises(LspProtocolError, match="size limit"):
        JsonRpcFramer.read(oversized)


def test_configured_lsp_provider_is_real_and_invalid_config_does_not_spawn(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    valid = DiagnosticsService(
        workspace,
        tmp_path / "valid-state",
        lsp_config=LspConfig(True, sys.executable, ("-I",), 5),
    ).snapshot()

    providers = {provider["id"]: provider for provider in valid["providers"]}
    assert providers["python-lsp"]["available"] is True
    assert providers["python-lsp"]["executable"] == str(Path(sys.executable).resolve())

    invalid_service = DiagnosticsService(
        workspace,
        tmp_path / "invalid-state",
        lsp_config=LspConfig(True, str(workspace / "missing.exe"), (), 5),
    )
    invalid = {provider["id"]: provider for provider in invalid_service.snapshot()["providers"]}
    assert invalid["python-lsp"]["available"] is False
    assert "absolute local file" in invalid["python-lsp"]["reason"]


def test_real_python_lsp_lifecycle_rejects_server_requests_and_filters_uris(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "demo.py"
    target.write_text("value = 1\n", encoding="utf-8")
    unopened = workspace / "not-opened.txt"
    unopened.write_text("value = 2\n", encoding="utf-8")
    log_path = tmp_path / "fake-lsp-log.json"
    fake_server = Path(__file__).parent / "fixtures" / "fake_python_lsp.py"
    service = DiagnosticsService(
        workspace,
        tmp_path / "state",
        lsp_config=LspConfig(
            enabled=True,
            command=sys.executable,
            args=("-I", str(fake_server.resolve()), str(log_path), unopened.as_uri()),
            timeout_seconds=5,
        ),
    )

    result = service.run("auto")

    lsp_problems = [item for item in result["diagnostics"] if item["source"] == "lsp:fake"]
    assert len(lsp_problems) == 1
    assert lsp_problems[0]["relative_path"] == "demo.py"
    assert lsp_problems[0]["severity"] == "error"
    assert (lsp_problems[0]["line"], lsp_problems[0]["column"]) == (3, 5)
    assert all(item["message"] != "must be ignored" for item in result["diagnostics"])

    lifecycle = json.loads(log_path.read_text(encoding="utf-8"))
    methods = [message.get("method") for message in lifecycle]
    assert methods[:3] == ["initialize", "initialized", "textDocument/didOpen"]
    assert "textDocument/didClose" in methods
    assert methods[-2:] == ["shutdown", "exit"]
    refusal = next(message for message in lifecycle if message.get("id") == 700)
    assert refusal["error"]["code"] == -32601


def test_python_lsp_cancellation_stops_a_server_during_initialize(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "demo.py"
    target.write_text("value = 1\n", encoding="utf-8")
    fake_server = Path(__file__).parent / "fixtures" / "fake_python_lsp.py"
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    started = __import__("time").monotonic()
    try:
        result = PythonLspClient(
            workspace,
            LspConfig(
                True,
                sys.executable,
                (
                    "-I",
                    str(fake_server.resolve()),
                    str(tmp_path / "unused-log.json"),
                    target.as_uri(),
                    "hang-initialize",
                ),
                5,
            ),
        ).run([target], cancel_event=cancel)
    finally:
        timer.cancel()

    assert result.cancelled is True
    assert __import__("time").monotonic() - started < 4


def test_ruff_parser_handles_windows_paths_and_rejects_outside_results(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "demo.py"
    target.write_text("import os\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    payload = [
        {
            "code": "F401",
            "filename": str(target),
            "location": {"row": 1, "column": 8},
            "end_location": {"row": 1, "column": 10},
            "message": "`os` imported but unused",
        },
        {
            "code": "F401",
            "filename": str(outside),
            "location": {"row": 1, "column": 1},
            "message": "outside",
        },
    ]

    diagnostics, message = _parse_ruff_diagnostics(
        json.dumps(payload), workspace=workspace, max_diagnostics=10
    )

    assert len(diagnostics) == 1
    assert diagnostics[0]["relative_path"] == "demo.py"
    assert diagnostics[0]["severity"] == "warning"
    assert "outside" in message


def test_auto_profile_runs_ruff_with_fixed_argv_and_no_cache(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "demo.py"
    target.write_text("import os\n", encoding="utf-8")
    service = DiagnosticsService(workspace, tmp_path / "state")
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        if argv[-1] == "--version":
            return CommandResult(tuple(argv), 0, "ruff 1.0.0\n", "", 1)
        payload = [
            {
                "code": "F401",
                "filename": str(target),
                "location": {"row": 1, "column": 8},
                "end_location": {"row": 1, "column": 10},
                "message": "unused import",
            }
        ]
        return CommandResult(tuple(argv), 1, json.dumps(payload), "", 1)

    monkeypatch.setattr(service._runner, "run", fake_run)

    result = service.run("auto")

    ruff_call = next(call for call in calls if "check" in call)
    assert ruff_call[:4] == (sys.executable, "-I", "-m", "ruff")
    assert "--no-cache" in ruff_call
    assert "--force-exclude" in ruff_call
    assert str(target) in ruff_call
    assert result["counts"]["warning"] == 1


def test_provider_snapshot_reports_lsp_tsc_and_locked_cargo_state(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    typescript = workspace / "web"
    (typescript / "node_modules" / "typescript" / "bin").mkdir(parents=True)
    (typescript / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (typescript / "node_modules" / "typescript" / "bin" / "tsc").write_text(
        "", encoding="utf-8"
    )
    rust = workspace / "native"
    rust.mkdir()
    (rust / "Cargo.toml").write_text("[package]\nname='demo'\n", encoding="utf-8")
    (rust / "Cargo.lock").write_text("version = 3\n", encoding="utf-8")
    monkeypatch.setattr(
        "stellarcode.diagnostics.service.shutil.which",
        lambda executable: f"C:/tools/{executable}.exe",
    )

    providers = {
        provider["id"]: provider
        for provider in DiagnosticsService(workspace, tmp_path / "state").snapshot()[
            "providers"
        ]
    }

    assert providers["python-lsp"]["available"] is False
    assert "not simulated LSP" in providers["python-lsp"]["reason"]
    assert providers["tsc"]["available"] is True
    assert providers["cargo"]["available"] is True


def test_build_profile_uses_only_local_tsc_and_locked_offline_cargo(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    typescript = workspace / "web"
    compiler = typescript / "node_modules" / "typescript" / "bin" / "tsc"
    compiler.parent.mkdir(parents=True)
    compiler.write_text("", encoding="utf-8")
    (typescript / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (typescript / "demo.ts").write_text("const value: number = 'x';\n", encoding="utf-8")
    rust = workspace / "native"
    (rust / "src").mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[package]\nname='demo'\n", encoding="utf-8")
    (rust / "Cargo.lock").write_text("version = 3\n", encoding="utf-8")
    (rust / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "stellarcode.diagnostics.service.shutil.which",
        lambda executable: f"C:/tools/{executable}.exe",
    )
    service = DiagnosticsService(workspace, tmp_path / "state")
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        if argv[-1] == "--version":
            return CommandResult(tuple(argv), 1, "", "missing ruff", 1)
        if "--message-format=json" in argv:
            message = {
                "reason": "compiler-message",
                "message": {
                    "level": "error",
                    "code": {"code": "E0308"},
                    "message": "mismatched types",
                    "spans": [
                        {
                            "file_name": "src/main.rs",
                            "is_primary": True,
                            "line_start": 1,
                            "column_start": 1,
                            "line_end": 1,
                            "column_end": 3,
                        }
                    ],
                },
            }
            return CommandResult(tuple(argv), 101, json.dumps(message), "", 1)
        return CommandResult(
            tuple(argv),
            2,
            "demo.ts(1,7): error TS2322: Type 'string' is not assignable to type 'number'.\n",
            "",
            1,
        )

    monkeypatch.setattr(service._runner, "run", fake_run)

    result = service.run("build")

    build_calls = [call for call in calls if call[-1] != "--version"]
    tsc_call = next(call for call in build_calls if "--noEmit" in call)
    cargo_call = next(call for call in build_calls if "check" in call)
    assert tsc_call[0] == "C:/tools/node.exe"
    assert tsc_call[1] == str(compiler.resolve())
    assert "--noEmit" in tsc_call
    assert tsc_call[tsc_call.index("--pretty") + 1] == "false"
    assert tsc_call[tsc_call.index("--incremental") + 1] == "false"
    assert cargo_call[:5] == (
        "C:/tools/cargo.exe",
        "check",
        "--locked",
        "--offline",
        "--message-format=json",
    )
    assert not any(part in {"npm", "npx", "mvn", "gradle"} for call in calls for part in call)
    assert result["counts"]["error"] == 2
    assert {item["source"] for item in result["diagnostics"]} == {"tsc", "cargo"}


def test_auto_profile_never_runs_project_build_commands(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (workspace / "Cargo.toml").write_text("[package]\nname='demo'\n", encoding="utf-8")
    (workspace / "Cargo.lock").write_text("version = 3\n", encoding="utf-8")
    service = DiagnosticsService(workspace, tmp_path / "state")
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        return CommandResult(tuple(argv), 1, "", "ruff unavailable", 1)

    monkeypatch.setattr(service._runner, "run", fake_run)

    service.run("auto")

    assert all("check" not in call for call in calls)
    assert all("--noEmit" not in call for call in calls)


def test_tsc_parser_handles_a_windows_style_path(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "src" / "demo.ts"
    target.parent.mkdir()
    target.write_text("", encoding="utf-8")
    raw = f"{target}(12,9): error TS2322: mismatched type"

    diagnostics, message = _parse_tsc_diagnostics(
        raw, workspace=workspace, project_root=workspace, max_diagnostics=10
    )

    assert message == ""
    assert diagnostics[0]["code"] == "TS2322"
    assert diagnostics[0]["line"] == 12
    assert diagnostics[0]["relative_path"] == "src/demo.ts"


def test_cargo_parser_uses_only_primary_spans_inside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    source = workspace / "src" / "lib.rs"
    source.parent.mkdir(parents=True)
    source.write_text("", encoding="utf-8")
    payload = {
        "reason": "compiler-message",
        "message": {
            "level": "warning",
            "code": {"code": "unused_variables"},
            "message": "unused variable",
            "spans": [
                {
                    "file_name": "src/lib.rs",
                    "is_primary": True,
                    "line_start": 3,
                    "column_start": 5,
                    "line_end": 3,
                    "column_end": 10,
                }
            ],
        },
    }

    diagnostics, message = _parse_cargo_diagnostics(
        "not-json\n" + json.dumps(payload),
        workspace=workspace,
        project_root=workspace,
        max_diagnostics=10,
    )

    assert "malformed" in message
    assert diagnostics[0]["severity"] == "warning"
    assert diagnostics[0]["relative_path"] == "src/lib.rs"


def test_safe_runner_can_cancel_a_child_process(tmp_path):
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        result = SafeCommandRunner().run(
            [sys.executable, "-I", "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            timeout_seconds=5,
            max_output_bytes=4096,
            cancel_event=cancel,
        )
    finally:
        timer.cancel()

    assert result.cancelled is True
    assert result.error == "diagnostic command cancelled"


def test_safe_runner_stops_a_child_that_exceeds_the_output_budget(tmp_path):
    result = SafeCommandRunner().run(
        [sys.executable, "-I", "-c", "import sys; sys.stdout.write('x' * 10000000)"],
        cwd=tmp_path,
        timeout_seconds=5,
        max_output_bytes=4096,
    )

    assert result.output_limited is True
    assert len(result.stdout.encode("utf-8")) <= 4096
    assert "output limit" in result.error


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only executable test")
def test_invalid_profile_does_not_start_a_diagnostic_run(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = DiagnosticsService(workspace, tmp_path / "state", "/bin/false")

    with pytest.raises(ValueError, match="unsupported diagnostics profile"):
        service.run("build")

    assert service.snapshot()["status"] == "idle"
