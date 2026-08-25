"""Safe workspace diagnostics orchestrator.

Fast syntax checks are non-executing. Build and LSP providers are explicit local process
launches with constrained environment, output, time, and cancellation budgets.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stellarcode.diagnostics.lsp import LspConfig, PythonLspClient
from stellarcode.path_utils import subprocess_safe_path


SCHEMA_VERSION = 1
SUPPORTED_PROFILES = frozenset({"auto", "syntax", "ruff", "build"})
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".stellarcode",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        "target",
        "dist",
        "build",
    }
)
PYTHON_PROJECT_MARKERS = frozenset(
    {
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "requirements.txt",
        "pipfile",
        "poetry.lock",
        "uv.lock",
        "environment.yml",
        "environment.yaml",
        "pytest.ini",
        "tox.ini",
        ".python-version",
    }
)
ERROR_RUFF_PREFIXES = ("E9", "F63", "F7", "F82")
TSC_DIAGNOSTIC_PATTERN = re.compile(
    r"^(.*)\((\d+),(\d+)\):\s+(error|warning|info)\s+TS(\d+):\s+(.*)$"
)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


ProgressCallback = Callable[[str], None]


# Diagnostic subprocesses may execute project-owned build scripts.  Keep their
# environment deliberately small so Runtime credentials are never inherited.
# These names are limited to process startup, locale, temporary directories,
# and toolchain discovery/configuration required by the supported checks.
DIAGNOSTIC_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "AR",
        "CARGO_BUILD_JOBS",
        "CARGO_HOME",
        "CARGO_TARGET_DIR",
        "CC",
        "CFLAGS",
        "COMSPEC",
        "CXX",
        "CXXFLAGS",
        "HOMEDRIVE",
        "HOMEPATH",
        "HOME",
        "INCLUDE",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LDFLAGS",
        "LIB",
        "LIBPATH",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "PATH",
        "PATHEXT",
        "PKG_CONFIG_LIBDIR",
        "PKG_CONFIG_PATH",
        "PKG_CONFIG_SYSROOT_DIR",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "RUST_BACKTRACE",
        "RUSTC",
        "RUSTDOC",
        "RUSTFLAGS",
        "RUSTUP_HOME",
        "SDKROOT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "UNIVERSALCRTSDKDIR",
        "UCRTVERSION",
        "USERPROFILE",
        "VCINSTALLDIR",
        "VCTOOLSINSTALLDIR",
        "VSINSTALLDIR",
        "WINDIR",
        "WINDOWSSDKDIR",
        "WINDOWSSDKVERSION",
    }
)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    return_code: int | None
    stdout: str
    stderr: str
    elapsed_ms: int
    error: str = ""
    cancelled: bool = False
    timed_out: bool = False
    output_limited: bool = False


class SafeCommandRunner:
    """Run a fixed argv without a shell and bound its process tree and output."""

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise ValueError("diagnostic command argv must contain non-empty strings")
        started = time.monotonic()
        creation_flags = 0
        popen_options: dict[str, Any] = {}
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            popen_options["start_new_session"] = True
        environment = _diagnostic_environment()
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr_file:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=subprocess_safe_path(cwd),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    creationflags=creation_flags,
                    **popen_options,
                )
            except (FileNotFoundError, OSError) as exc:
                return CommandResult(
                    tuple(argv),
                    None,
                    "",
                    "",
                    int((time.monotonic() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )

            deadline = started + max(0.1, timeout_seconds)
            cancelled = False
            timed_out = False
            output_limited = False
            while process.poll() is None:
                cancelled = bool(cancel_event and cancel_event.is_set())
                timed_out = time.monotonic() >= deadline
                output_limited = (
                    _stream_size(stdout_file) + _stream_size(stderr_file) > max_output_bytes
                )
                if cancelled or timed_out or output_limited:
                    _terminate_process_tree(process)
                    break
                time.sleep(0.05)
            output_limited = output_limited or (
                _stream_size(stdout_file) + _stream_size(stderr_file) > max_output_bytes
            )
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass

            stdout = _read_bounded(stdout_file, max_output_bytes)
            remaining = max(0, max_output_bytes - len(stdout.encode("utf-8")))
            stderr = _read_bounded(stderr_file, remaining)
            error = ""
            if cancelled:
                error = "diagnostic command cancelled"
            elif timed_out:
                error = f"diagnostic command timed out after {timeout_seconds:g}s"
            elif output_limited:
                error = f"diagnostic command exceeded the {max_output_bytes}-byte output limit"
            return CommandResult(
                tuple(argv),
                process.returncode,
                stdout,
                stderr,
                int((time.monotonic() - started) * 1000),
                error=error,
                cancelled=cancelled,
                timed_out=timed_out,
                output_limited=output_limited,
            )


class DiagnosticsService:
    """Collect read-only diagnostics for one workspace and persist the latest run.

    Syntax diagnostics use the Runtime interpreter's parser and never import or execute
    workspace code. Ruff is optional and is invoked in Python isolated mode with an explicit
    file list, no cache, no fixes, no shell, and bounded time/output.
    """

    def __init__(
        self,
        workspace: str | Path,
        storage_dir: str | Path,
        python_executable: str | Path = sys.executable,
        *,
        max_files: int = 5_000,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_diagnostics: int = 5_000,
        command_timeout_seconds: float = 90.0,
        max_command_output_bytes: int = 2 * 1024 * 1024,
        lsp_config: LspConfig | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"diagnostics workspace is not a directory: {self.workspace}")
        self.storage_dir = Path(storage_dir).resolve()
        self.snapshot_file = self.storage_dir / "snapshot.json"
        self.python_executable = str(python_executable)
        self.max_files = max(1, int(max_files))
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_diagnostics = max(1, int(max_diagnostics))
        self.command_timeout_seconds = max(0.1, float(command_timeout_seconds))
        self.max_command_output_bytes = max(4_096, int(max_command_output_bytes))
        self.lsp_config = lsp_config or LspConfig()
        self._validated_lsp_config: LspConfig | None = None
        self._lsp_configuration_error = ""
        if self.lsp_config.enabled:
            try:
                self._validated_lsp_config = self.lsp_config.validated()
            except (OSError, TypeError, ValueError) as exc:
                self._lsp_configuration_error = str(exc)
        self._state_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._runner = SafeCommandRunner()
        self._projects = _detect_projects(self.workspace, self.max_files)
        self._providers = self._detect_providers()
        loaded_snapshot = self._load_snapshot()
        self._state = loaded_snapshot or self._empty_snapshot()
        self._state.update(
            {
                "workspace": str(self.workspace),
                "storage_path": str(self.snapshot_file),
                "detected_projects": copy.deepcopy(self._projects),
                "providers": copy.deepcopy(self._providers),
            }
        )
        if self._state.get("status") == "running":
            self._state["status"] = "cancelled"
            self._state["error"] = "The previous diagnostic run was interrupted."
            self._state["finished_at"] = _utc_now()
            self._state["stale"] = True
        elif loaded_snapshot is not None:
            # Results remain useful after a restart, but no file watcher proves that the
            # workspace is unchanged while the Runtime was offline.
            self._state["stale"] = True
        self._persist_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return copy.deepcopy(self._state)

    def run(
        self,
        profile: str = "auto",
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_profile = str(profile).strip().lower()
        if normalized_profile not in SUPPORTED_PROFILES:
            choices = ", ".join(sorted(SUPPORTED_PROFILES))
            raise ValueError(f"unsupported diagnostics profile {profile!r}; choose {choices}")
        effective_run_id = _validated_run_id(run_id)
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("another diagnostics run is already active")

        started = time.monotonic()
        started_at = _utc_now()
        diagnostics: list[dict[str, Any]] = []
        provider_messages: list[dict[str, str]] = []
        files: list[Path] = []
        try:
            self._projects = _detect_projects(self.workspace, self.max_files)
            self._providers = self._detect_providers()
            self._replace_state(
                {
                    **self._empty_snapshot(),
                    "run_id": effective_run_id,
                    "profile": normalized_profile,
                    "status": "running",
                    "started_at": started_at,
                    "stale": False,
                    "detected_projects": copy.deepcopy(self._projects),
                    "providers": copy.deepcopy(self._providers),
                }
            )
            if normalized_profile != "build":
                _report(progress, "Discovering Python source files...")
                files, discovery = _discover_python_files(
                    self.workspace,
                    max_files=self.max_files,
                    max_file_bytes=self.max_file_bytes,
                    cancel_event=cancel_event,
                )
                provider_messages.extend(discovery["messages"])
                if _cancelled(cancel_event):
                    return self._finish_cancelled(
                        effective_run_id,
                        normalized_profile,
                        started,
                        diagnostics,
                        files,
                        provider_messages,
                    )

                _report(progress, f"Checking Python syntax in {len(files)} file(s)...")
                diagnostics.extend(
                    self._syntax_diagnostics(files, progress=progress, cancel_event=cancel_event)
                )
                if _cancelled(cancel_event):
                    return self._finish_cancelled(
                        effective_run_id,
                        normalized_profile,
                        started,
                        diagnostics,
                        files,
                        provider_messages,
                    )

            ruff = next(provider for provider in self._providers if provider["id"] == "ruff")
            if normalized_profile in {"auto", "ruff"}:
                if ruff["available"]:
                    _report(progress, "Running Ruff diagnostics...")
                    ruff_diagnostics, ruff_message, ruff_cancelled = self._ruff_diagnostics(
                        files,
                        progress=progress,
                        cancel_event=cancel_event,
                    )
                    diagnostics.extend(ruff_diagnostics)
                    if ruff_message:
                        provider_messages.append({"provider": "ruff", "message": ruff_message})
                    if ruff_cancelled:
                        return self._finish_cancelled(
                            effective_run_id,
                            normalized_profile,
                            started,
                            diagnostics,
                            files,
                            provider_messages,
                        )
                else:
                    provider_messages.append(
                        {"provider": "ruff", "message": str(ruff.get("reason") or "unavailable")}
                    )

            if normalized_profile == "auto" and self._validated_lsp_config is not None:
                _report(progress, "Running configured Python language server...")
                lsp_result = PythonLspClient(
                    self.workspace,
                    self._validated_lsp_config,
                    max_files=self.max_files,
                    max_file_bytes=self.max_file_bytes,
                    max_diagnostics=max(1, self.max_diagnostics - len(diagnostics)),
                ).run(files, progress=progress, cancel_event=cancel_event)
                diagnostics.extend(lsp_result.diagnostics)
                if lsp_result.message:
                    provider_messages.append(
                        {"provider": "python-lsp", "message": lsp_result.message}
                    )
                if lsp_result.cancelled or _cancelled(cancel_event):
                    return self._finish_cancelled(
                        effective_run_id,
                        normalized_profile,
                        started,
                        diagnostics,
                        files,
                        provider_messages,
                    )

            if normalized_profile == "build":
                build_diagnostics, build_files, build_messages, build_cancelled = (
                    self._build_diagnostics(progress=progress, cancel_event=cancel_event)
                )
                diagnostics.extend(build_diagnostics)
                files.extend(build_files)
                provider_messages.extend(build_messages)
                if build_cancelled:
                    return self._finish_cancelled(
                        effective_run_id,
                        normalized_profile,
                        started,
                        diagnostics,
                        files,
                        provider_messages,
                    )

            diagnostics = _deduplicate_diagnostics(diagnostics)[: self.max_diagnostics]
            for diagnostic in diagnostics:
                diagnostic["run_id"] = effective_run_id
            result = self._result_snapshot(
                run_id=effective_run_id,
                profile=normalized_profile,
                status="completed",
                started_at=started_at,
                started=started,
                diagnostics=diagnostics,
                files=files,
                provider_messages=provider_messages,
            )
            self._replace_state(result)
            _report(progress, f"Diagnostics completed with {len(diagnostics)} problem(s).")
            return self.snapshot()
        except Exception as exc:
            result = self._result_snapshot(
                run_id=effective_run_id,
                profile=normalized_profile,
                status="failed",
                started_at=started_at,
                started=started,
                diagnostics=diagnostics,
                files=files,
                provider_messages=provider_messages,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._replace_state(result)
            return self.snapshot()
        finally:
            self._run_lock.release()

    def _syntax_diagnostics(
        self,
        files: list[Path],
        *,
        progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
    ) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        for index, path in enumerate(files, start=1):
            if _cancelled(cancel_event) or len(diagnostics) >= self.max_diagnostics:
                break
            if index == 1 or index % 50 == 0 or index == len(files):
                _report(progress, f"Python syntax: {index}/{len(files)}")
            try:
                with tokenize.open(path) as stream:
                    source = stream.read()
                # Compiling produces a code object but does not execute or import the file.
                # Unlike ast.parse alone, it also rejects context-sensitive syntax such as
                # top-level return/yield/await statements.
                compile(source, str(path), "exec", dont_inherit=True)
            except (SyntaxError, UnicodeDecodeError) as exc:
                diagnostics.append(_syntax_error_diagnostic(self.workspace, path, exc))
            except OSError as exc:
                diagnostics.append(
                    _diagnostic(
                        self.workspace,
                        path,
                        severity="warning",
                        source="python",
                        code="PYTHON_IO",
                        message=f"Could not read Python source: {exc}",
                        line=1,
                        column=1,
                    )
                )
        return diagnostics

    def _ruff_diagnostics(
        self,
        files: list[Path],
        *,
        progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
    ) -> tuple[list[dict[str, Any]], str, bool]:
        if not files:
            return [], "", False
        diagnostics: list[dict[str, Any]] = []
        batches = list(_argument_batches(files))
        output_budget = self.max_command_output_bytes
        for index, batch in enumerate(batches, start=1):
            if _cancelled(cancel_event):
                return diagnostics, "Ruff was cancelled.", True
            _report(progress, f"Ruff: batch {index}/{len(batches)}")
            argv = [
                self.python_executable,
                "-I",
                "-m",
                "ruff",
                "check",
                "--output-format=json",
                "--no-cache",
                "--force-exclude",
                *[str(path) for path in batch],
            ]
            result = self._runner.run(
                argv,
                cwd=self.workspace,
                timeout_seconds=self.command_timeout_seconds,
                max_output_bytes=max(4_096, output_budget),
                cancel_event=cancel_event,
            )
            output_budget -= len(result.stdout.encode("utf-8")) + len(
                result.stderr.encode("utf-8")
            )
            if result.cancelled:
                return diagnostics, result.error, True
            if result.error:
                return diagnostics, result.error, False
            if result.return_code not in {0, 1}:
                message = result.stderr.strip() or result.stdout.strip()
                return diagnostics, f"Ruff failed with exit code {result.return_code}: {message}", False
            parsed, error = _parse_ruff_diagnostics(
                result.stdout,
                workspace=self.workspace,
                max_diagnostics=max(0, self.max_diagnostics - len(diagnostics)),
            )
            diagnostics.extend(parsed)
            if error:
                return diagnostics, error, False
            if len(diagnostics) >= self.max_diagnostics:
                return diagnostics, "Ruff diagnostics were truncated by the result limit.", False
            if output_budget <= 0:
                return diagnostics, "Ruff output was truncated by the aggregate output limit.", False
        return diagnostics, "", False

    def _build_diagnostics(
        self,
        *,
        progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
    ) -> tuple[list[dict[str, Any]], list[Path], list[dict[str, str]], bool]:
        diagnostics: list[dict[str, Any]] = []
        roots: list[Path] = []
        messages: list[dict[str, str]] = []
        provider_by_id = {provider["id"]: provider for provider in self._providers}

        typescript_projects = _projects_of_kind(self._projects, "typescript")
        tsc_provider = provider_by_id["tsc"]
        if typescript_projects and not tsc_provider["available"]:
            messages.append({"provider": "tsc", "message": str(tsc_provider["reason"])})
        elif tsc_provider["available"]:
            node = str(tsc_provider["executable"])
            for project in typescript_projects:
                if _cancelled(cancel_event):
                    return diagnostics, roots, messages, True
                root = Path(project["root"])
                compiler = _local_tsc_path(root)
                if compiler is None:
                    messages.append(
                        {
                            "provider": "tsc",
                            "message": f"Skipped {root}: project-local TypeScript is missing.",
                        }
                    )
                    continue
                roots.append(root)
                _report(progress, f"TypeScript: {project['relative_root']}")
                argv = [
                    node,
                    str(compiler),
                    "--noEmit",
                    "--pretty",
                    "false",
                    "--incremental",
                    "false",
                    "--project",
                    str(root / "tsconfig.json"),
                ]
                result = self._runner.run(
                    argv,
                    cwd=root,
                    timeout_seconds=self.command_timeout_seconds,
                    max_output_bytes=self.max_command_output_bytes,
                    cancel_event=cancel_event,
                )
                if result.cancelled:
                    messages.append({"provider": "tsc", "message": result.error})
                    return diagnostics, roots, messages, True
                parsed, parse_message = _parse_tsc_diagnostics(
                    "\n".join(part for part in (result.stdout, result.stderr) if part),
                    workspace=self.workspace,
                    project_root=root,
                    max_diagnostics=max(0, self.max_diagnostics - len(diagnostics)),
                )
                diagnostics.extend(parsed)
                if parse_message:
                    messages.append({"provider": "tsc", "message": parse_message})
                if result.error:
                    messages.append({"provider": "tsc", "message": result.error})
                elif result.return_code not in {0, 1, 2}:
                    messages.append(
                        {
                            "provider": "tsc",
                            "message": f"TypeScript exited with code {result.return_code}.",
                        }
                    )

        cargo_projects = _projects_of_kind(self._projects, "rust")
        cargo_provider = provider_by_id["cargo"]
        if cargo_projects and not cargo_provider["available"]:
            messages.append({"provider": "cargo", "message": str(cargo_provider["reason"])})
        elif cargo_provider["available"]:
            cargo = str(cargo_provider["executable"])
            for project in cargo_projects:
                if _cancelled(cancel_event):
                    return diagnostics, roots, messages, True
                root = Path(project["root"])
                if not (root / "Cargo.lock").is_file():
                    messages.append(
                        {
                            "provider": "cargo",
                            "message": f"Skipped {root}: Cargo.lock is required.",
                        }
                    )
                    continue
                roots.append(root)
                _report(progress, f"Cargo: {project['relative_root']}")
                argv = [
                    cargo,
                    "check",
                    "--locked",
                    "--offline",
                    "--message-format=json",
                    "--manifest-path",
                    str(root / "Cargo.toml"),
                ]
                result = self._runner.run(
                    argv,
                    cwd=root,
                    timeout_seconds=self.command_timeout_seconds,
                    max_output_bytes=self.max_command_output_bytes,
                    cancel_event=cancel_event,
                )
                if result.cancelled:
                    messages.append({"provider": "cargo", "message": result.error})
                    return diagnostics, roots, messages, True
                parsed, parse_message = _parse_cargo_diagnostics(
                    result.stdout,
                    workspace=self.workspace,
                    project_root=root,
                    max_diagnostics=max(0, self.max_diagnostics - len(diagnostics)),
                )
                diagnostics.extend(parsed)
                if parse_message:
                    messages.append({"provider": "cargo", "message": parse_message})
                if result.error:
                    messages.append({"provider": "cargo", "message": result.error})
                elif result.return_code not in {0, 101}:
                    messages.append(
                        {
                            "provider": "cargo",
                            "message": f"Cargo exited with code {result.return_code}.",
                        }
                    )
        if not typescript_projects and not cargo_projects:
            messages.append(
                {
                    "provider": "build",
                    "message": "No supported local TypeScript or locked Cargo project was detected.",
                }
            )
        return diagnostics, roots, messages, False

    def _detect_providers(self) -> list[dict[str, Any]]:
        syntax = {
            "id": "python-syntax",
            "name": "Python syntax",
            "kind": "syntax",
            "available": True,
            "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "executable": sys.executable,
            "reason": "Compiles source without importing or executing workspace code.",
        }
        probe = self._runner.run(
            [self.python_executable, "-I", "-m", "ruff", "--version"],
            cwd=self.workspace,
            timeout_seconds=min(5.0, self.command_timeout_seconds),
            max_output_bytes=65_536,
        )
        available = probe.return_code == 0 and not probe.error
        version = probe.stdout.strip().removeprefix("ruff ") if available else ""
        reason = ""
        if not available:
            detail = probe.error or probe.stderr.strip() or probe.stdout.strip()
            reason = detail or "Ruff is not installed in the selected Python environment."
        ruff = {
            "id": "ruff",
            "name": "Ruff",
            "kind": "linter",
            "available": available,
            "version": version,
            "executable": self.python_executable,
            "reason": reason,
        }
        typescript_projects = _projects_of_kind(self._projects, "typescript")
        local_tsc = [
            _local_tsc_path(Path(project["root"])) for project in typescript_projects
        ]
        local_tsc = [path for path in local_tsc if path is not None]
        node = shutil.which("node")
        tsc_available = bool(node and local_tsc)
        if not typescript_projects:
            tsc_reason = "No tsconfig.json project was detected."
        elif not node:
            tsc_reason = "Node.js was not found on PATH."
        elif not local_tsc:
            tsc_reason = "No project-local node_modules/typescript/bin/tsc was found."
        else:
            tsc_reason = "Uses the project-local TypeScript compiler; never npm or npx."
        tsc = {
            "id": "tsc",
            "name": "TypeScript compiler",
            "kind": "compiler",
            "available": tsc_available,
            "version": "",
            "executable": str(node or ""),
            "reason": tsc_reason,
        }

        cargo_projects = _projects_of_kind(self._projects, "rust")
        locked_cargo_projects = [
            project for project in cargo_projects if (Path(project["root"]) / "Cargo.lock").is_file()
        ]
        cargo_executable = shutil.which("cargo")
        cargo_available = bool(cargo_executable and locked_cargo_projects)
        if not cargo_projects:
            cargo_reason = "No Cargo.toml project was detected."
        elif not cargo_executable:
            cargo_reason = "Cargo was not found on PATH."
        elif not locked_cargo_projects:
            cargo_reason = "Cargo.lock is required for locked offline diagnostics."
        else:
            cargo_reason = "Runs cargo check with --locked --offline."
        cargo = {
            "id": "cargo",
            "name": "Cargo check",
            "kind": "compiler",
            "available": cargo_available,
            "version": "",
            "executable": str(cargo_executable or ""),
            "reason": cargo_reason,
        }
        lsp_available = self._validated_lsp_config is not None
        if lsp_available:
            lsp_reason = (
                "Runs the explicitly configured Python language server over read-only "
                "stdio JSON-RPC with bounded files, output, time, and process lifetime."
            )
        elif self.lsp_config.enabled:
            lsp_reason = self._lsp_configuration_error or "Invalid Python LSP configuration."
        else:
            lsp_reason = (
                "No managed LSP provider is configured. Syntax, Ruff, and explicit build "
                "diagnostics remain real compiler/linter results, not simulated LSP output."
            )
        lsp = {
            "id": "python-lsp",
            "name": "Python language server",
            "kind": "language-server",
            "available": lsp_available,
            "version": "",
            "executable": (
                self._validated_lsp_config.command if self._validated_lsp_config else ""
            ),
            "reason": lsp_reason,
        }
        return [syntax, ruff, tsc, cargo, lsp]

    def _finish_cancelled(
        self,
        run_id: str,
        profile: str,
        started: float,
        diagnostics: list[dict[str, Any]],
        files: list[Path],
        provider_messages: list[dict[str, str]],
    ) -> dict[str, Any]:
        result = self._result_snapshot(
            run_id=run_id,
            profile=profile,
            status="cancelled",
            started_at=str(self._state.get("started_at") or _utc_now()),
            started=started,
            diagnostics=_deduplicate_diagnostics(diagnostics)[: self.max_diagnostics],
            files=files,
            provider_messages=provider_messages,
            error="Diagnostic run cancelled.",
        )
        self._replace_state(result)
        return self.snapshot()

    def _result_snapshot(
        self,
        *,
        run_id: str,
        profile: str,
        status: str,
        started_at: str,
        started: float,
        diagnostics: list[dict[str, Any]],
        files: list[Path],
        provider_messages: list[dict[str, str]],
        error: str = "",
    ) -> dict[str, Any]:
        counts = _diagnostic_counts(diagnostics)
        for diagnostic in diagnostics:
            diagnostic["run_id"] = run_id
        return {
            "schema_version": SCHEMA_VERSION,
            "workspace": str(self.workspace),
            "storage_path": str(self.snapshot_file),
            "run_id": run_id,
            "profile": profile,
            "status": status,
            "detected_projects": copy.deepcopy(self._projects),
            "providers": copy.deepcopy(self._providers),
            "diagnostics": diagnostics,
            "diagnostics_truncated": len(diagnostics) >= self.max_diagnostics,
            "counts": counts,
            "files_scanned": len(files),
            "provider_messages": provider_messages,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "stale": False,
            "error": error,
        }

    def _empty_snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "workspace": str(self.workspace),
            "storage_path": str(self.snapshot_file),
            "run_id": "",
            "profile": "auto",
            "status": "idle",
            "detected_projects": copy.deepcopy(self._projects),
            "providers": copy.deepcopy(self._providers),
            "diagnostics": [],
            "diagnostics_truncated": False,
            "counts": {"error": 0, "warning": 0, "info": 0, "total": 0},
            "files_scanned": 0,
            "provider_messages": [],
            "started_at": None,
            "finished_at": None,
            "elapsed_ms": 0,
            "stale": True,
            "error": "",
        }

    def _load_snapshot(self) -> dict[str, Any] | None:
        if not self.snapshot_file.is_file():
            return None
        try:
            if self.snapshot_file.stat().st_size > 10 * 1024 * 1024:
                return None
            payload = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != SCHEMA_VERSION:
            return None
        if Path(str(payload.get("workspace") or "")).resolve() != self.workspace:
            return None
        return payload

    def _replace_state(self, state: dict[str, Any]) -> None:
        with self._state_lock:
            self._state = state
            self._persist_locked()

    def _persist_locked(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_file.with_name(
            f".{self.snapshot_file.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(self._state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.snapshot_file)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _detect_projects(workspace: Path, max_directories: int) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    python_seen = False
    visited = 0
    stack = [workspace]
    while stack and visited < max_directories:
        directory = stack.pop()
        visited += 1
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        names = {entry.name.casefold() for entry in entries}
        markers = sorted(
            entry.name
            for entry in entries
            if entry.is_file(follow_symlinks=False) and _is_python_marker(entry.name)
        )
        has_python = any(
            entry.is_file(follow_symlinks=False)
            and Path(entry.name).suffix.casefold() in PYTHON_SUFFIXES
            for entry in entries
        )
        python_seen = python_seen or has_python
        if markers:
            projects.append(
                {
                    "kind": "python",
                    "root": str(directory),
                    "relative_root": _relative_display(workspace, directory),
                    "markers": markers,
                }
            )
        if "tsconfig.json" in names:
            projects.append(
                {
                    "kind": "typescript",
                    "root": str(directory),
                    "relative_root": _relative_display(workspace, directory),
                    "markers": ["tsconfig.json"],
                }
            )
        if "cargo.toml" in names:
            rust_markers = ["Cargo.toml"]
            if "cargo.lock" in names:
                rust_markers.append("Cargo.lock")
            projects.append(
                {
                    "kind": "rust",
                    "root": str(directory),
                    "relative_root": _relative_display(workspace, directory),
                    "markers": rust_markers,
                }
            )
        for entry in reversed(sorted(entries, key=lambda item: item.name.casefold())):
            if entry.name.casefold() in IGNORED_DIRECTORIES:
                continue
            if _safe_directory_entry(entry):
                stack.append(Path(entry.path))
    if not projects and python_seen:
        projects.append(
            {
                "kind": "python",
                "root": str(workspace),
                "relative_root": ".",
                "markers": ["python-files"],
            }
        )
    return projects


def _projects_of_kind(projects: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [project for project in projects if project.get("kind") == kind]


def _local_tsc_path(project_root: Path) -> Path | None:
    compiler = project_root / "node_modules" / "typescript" / "bin" / "tsc"
    if compiler.is_file():
        return compiler.resolve()
    compiler_js = compiler.with_suffix(".js")
    return compiler_js.resolve() if compiler_js.is_file() else None


def _discover_python_files(
    workspace: Path,
    *,
    max_files: int,
    max_file_bytes: int,
    cancel_event: threading.Event | None,
) -> tuple[list[Path], dict[str, Any]]:
    files: list[Path] = []
    messages: list[dict[str, str]] = []
    oversized = 0
    unreadable = 0
    truncated = False
    stack = [workspace]
    while stack and not _cancelled(cancel_event):
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            unreadable += 1
            if unreadable <= 10:
                messages.append(
                    {"provider": "python-syntax", "message": f"Could not scan {directory}: {exc}"}
                )
            continue
        for entry in entries:
            if _cancelled(cancel_event):
                break
            if entry.name.casefold() in IGNORED_DIRECTORIES:
                continue
            if _safe_directory_entry(entry):
                stack.append(Path(entry.path))
                continue
            try:
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                is_file = False
            if not is_file or Path(entry.name).suffix.casefold() not in PYTHON_SUFFIXES:
                continue
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                unreadable += 1
                continue
            if size > max_file_bytes:
                oversized += 1
                continue
            files.append(Path(entry.path))
            if len(files) >= max_files:
                truncated = True
                stack.clear()
                break
    files.sort(key=lambda path: str(path).casefold())
    if oversized:
        messages.append(
            {
                "provider": "python-syntax",
                "message": f"Skipped {oversized} Python file(s) larger than {max_file_bytes} bytes.",
            }
        )
    if unreadable > 10:
        messages.append(
            {
                "provider": "python-syntax",
                "message": f"Could not inspect {unreadable} workspace path(s).",
            }
        )
    if truncated:
        messages.append(
            {
                "provider": "python-syntax",
                "message": f"Source discovery stopped at the {max_files}-file limit.",
            }
        )
    return files, {"messages": messages, "truncated": truncated}


def _parse_ruff_diagnostics(
    raw: str,
    *,
    workspace: Path,
    max_diagnostics: int,
) -> tuple[list[dict[str, Any]], str]:
    if not raw.strip():
        return [], ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], f"Ruff returned invalid JSON: {exc}"
    if not isinstance(payload, list):
        return [], "Ruff returned a JSON value that was not a diagnostic list."
    diagnostics: list[dict[str, Any]] = []
    outside = 0
    for item in payload:
        if len(diagnostics) >= max_diagnostics:
            break
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            continue
        path = Path(filename)
        candidate = (workspace / path).resolve() if not path.is_absolute() else path.resolve()
        if not _is_within(workspace, candidate):
            outside += 1
            continue
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        end = item.get("end_location") if isinstance(item.get("end_location"), dict) else {}
        code = str(item.get("code") or "RUFF")
        diagnostics.append(
            _diagnostic(
                workspace,
                candidate,
                severity=("error" if code.startswith(ERROR_RUFF_PREFIXES) else "warning"),
                source="ruff",
                code=code,
                message=str(item.get("message") or "Ruff diagnostic"),
                line=_positive_int(location.get("row")),
                column=_positive_int(location.get("column")),
                end_line=_positive_int(end.get("row")),
                end_column=_positive_int(end.get("column")),
            )
        )
    message = f"Ignored {outside} Ruff diagnostic(s) outside the workspace." if outside else ""
    return diagnostics, message


def _parse_tsc_diagnostics(
    raw: str,
    *,
    workspace: Path,
    project_root: Path,
    max_diagnostics: int,
) -> tuple[list[dict[str, Any]], str]:
    diagnostics: list[dict[str, Any]] = []
    ignored = 0
    unparsed = 0
    for raw_line in raw.splitlines():
        line = ANSI_ESCAPE_PATTERN.sub("", raw_line).strip()
        if not line:
            continue
        match = TSC_DIAGNOSTIC_PATTERN.match(line)
        if not match:
            unparsed += 1
            continue
        path_text, row, column, severity, code, message = match.groups()
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        candidate = candidate.resolve()
        if not _is_within(workspace, candidate):
            ignored += 1
            continue
        if len(diagnostics) >= max_diagnostics:
            break
        diagnostics.append(
            _diagnostic(
                workspace,
                candidate,
                severity=severity,
                source="tsc",
                code=f"TS{code}",
                message=message,
                line=_positive_int(row),
                column=_positive_int(column),
            )
        )
    notes = []
    if ignored:
        notes.append(f"Ignored {ignored} TypeScript diagnostic(s) outside the workspace.")
    if unparsed and not diagnostics:
        notes.append(f"TypeScript returned {unparsed} non-diagnostic output line(s).")
    return diagnostics, " ".join(notes)


def _parse_cargo_diagnostics(
    raw: str,
    *,
    workspace: Path,
    project_root: Path,
    max_diagnostics: int,
) -> tuple[list[dict[str, Any]], str]:
    diagnostics: list[dict[str, Any]] = []
    malformed = 0
    ignored = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(item, dict) or item.get("reason") != "compiler-message":
            continue
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        level = str(message.get("level") or "info")
        if level not in {"error", "warning", "note", "help", "info"}:
            level = "info"
        severity = "info" if level in {"note", "help"} else level
        code_data = message.get("code")
        code = (
            str(code_data.get("code") or "CARGO")
            if isinstance(code_data, dict)
            else "CARGO"
        )
        spans = message.get("spans")
        if not isinstance(spans, list):
            continue
        primary = next(
            (span for span in spans if isinstance(span, dict) and span.get("is_primary") is True),
            None,
        )
        if primary is None:
            continue
        filename = primary.get("file_name")
        if not isinstance(filename, str) or not filename:
            continue
        candidate = Path(filename)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        candidate = candidate.resolve()
        if not _is_within(workspace, candidate):
            ignored += 1
            continue
        if len(diagnostics) >= max_diagnostics:
            break
        diagnostics.append(
            _diagnostic(
                workspace,
                candidate,
                severity=severity,
                source="cargo",
                code=code,
                message=str(message.get("message") or "Cargo compiler diagnostic"),
                line=_positive_int(primary.get("line_start")),
                column=_positive_int(primary.get("column_start")),
                end_line=_positive_int(primary.get("line_end")),
                end_column=_positive_int(primary.get("column_end")),
            )
        )
    notes = []
    if malformed:
        notes.append(f"Ignored {malformed} malformed Cargo JSON line(s).")
    if ignored:
        notes.append(f"Ignored {ignored} Cargo diagnostic(s) outside the workspace.")
    return diagnostics, " ".join(notes)


def _syntax_error_diagnostic(
    workspace: Path,
    path: Path,
    error: SyntaxError | UnicodeDecodeError,
) -> dict[str, Any]:
    if isinstance(error, SyntaxError):
        return _diagnostic(
            workspace,
            path,
            severity="error",
            source="python",
            code="PYTHON_SYNTAX",
            message=error.msg,
            line=_positive_int(error.lineno),
            column=_positive_int(error.offset),
            end_line=_positive_int(error.end_lineno),
            end_column=_positive_int(error.end_offset),
        )
    return _diagnostic(
        workspace,
        path,
        severity="error",
        source="python",
        code="PYTHON_DECODE",
        message=f"Could not decode Python source: {error}",
        line=1,
        column=1,
    )


def _diagnostic(
    workspace: Path,
    path: Path,
    *,
    severity: str,
    source: str,
    code: str,
    message: str,
    line: int,
    column: int,
    end_line: int | None = None,
    end_column: int | None = None,
) -> dict[str, Any]:
    relative = _relative_display(workspace, path)
    identity = "\0".join(
        (source, code, relative, str(line), str(column), message)
    ).encode("utf-8", errors="replace")
    return {
        "id": f"diagnostic-{hashlib.sha256(identity).hexdigest()[:20]}",
        "severity": severity,
        "source": source,
        "code": code,
        "message": message,
        "path": str(path),
        "relative_path": relative,
        "line": max(1, line),
        "column": max(1, column),
        "end_line": max(1, end_line or line),
        "end_column": max(1, end_column or column),
    }


def _argument_batches(paths: list[Path], max_items: int = 100, max_chars: int = 20_000):
    batch: list[Path] = []
    characters = 0
    for path in paths:
        length = len(str(path)) + 3
        if batch and (len(batch) >= max_items or characters + length > max_chars):
            yield batch
            batch = []
            characters = 0
        batch.append(path)
        characters += length
    if batch:
        yield batch


def _detect_marker_name(name: str) -> str:
    lowered = name.casefold()
    if lowered.startswith("requirements") and lowered.endswith(".txt"):
        return "requirements*.txt"
    return lowered


def _is_python_marker(name: str) -> bool:
    marker = _detect_marker_name(name)
    return marker in PYTHON_PROJECT_MARKERS or marker == "requirements*.txt"


def _safe_directory_entry(entry: os.DirEntry[str]) -> bool:
    try:
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            return False
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(metadata, "st_file_attributes", 0)
        if reparse_flag and attributes & reparse_flag:
            return False
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _diagnostic_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in DIAGNOSTIC_ENVIRONMENT_ALLOWLIST
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    environment["NO_COLOR"] = "1"
    return environment


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        taskkill = str(Path(system_root) / "System32" / "taskkill.exe")
        try:
            result = subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _stream_size(stream: Any) -> int:
    try:
        return int(os.fstat(stream.fileno()).st_size)
    except OSError:
        return 0


def _read_bounded(stream: Any, limit: int) -> str:
    stream.seek(0)
    return stream.read(max(0, limit)).decode("utf-8", errors="replace")


def _deduplicate_diagnostics(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for value in values:
        identifier = str(value.get("id") or "")
        if identifier and identifier not in unique:
            unique[identifier] = value
    return sorted(
        unique.values(),
        key=lambda item: (
            str(item.get("relative_path") or "").casefold(),
            int(item.get("line") or 1),
            int(item.get("column") or 1),
            str(item.get("source") or ""),
            str(item.get("code") or ""),
        ),
    )


def _diagnostic_counts(diagnostics: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for diagnostic in diagnostics:
        severity = str(diagnostic.get("severity") or "info")
        counts[severity if severity in counts else "info"] += 1
    return {**counts, "total": len(diagnostics)}


def _relative_display(workspace: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace).as_posix() or "."
    except ValueError:
        return str(path)


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _positive_int(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _cancelled(cancel_event: threading.Event | None) -> bool:
    return bool(cancel_event and cancel_event.is_set())


def _report(progress: ProgressCallback | None, message: str) -> None:
    if progress is None:
        return
    try:
        progress(message[:500])
    except Exception:
        pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validated_run_id(value: str | None) -> str:
    if value is None:
        return f"diagnostics-{uuid.uuid4().hex}"
    normalized = str(value).strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("diagnostics run_id must contain 1-128 characters")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", normalized):
        raise ValueError("diagnostics run_id contains unsupported characters")
    return normalized
