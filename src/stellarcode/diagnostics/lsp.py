from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable
from urllib.parse import unquote, urlparse

from stellarcode.path_utils import subprocess_safe_path


MAX_HEADER_BYTES = 8 * 1024
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_FILES = 2_000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 32 * 1024 * 1024
MAX_DIAGNOSTICS = 5_000
MAX_PENDING_MESSAGES = 512
_CONTENT_LENGTH = re.compile(rb"^[0-9]+$")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UNSAFE_WINDOWS_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd", ".ps1"})


class LspProtocolError(RuntimeError):
    """The language server sent an invalid or unsafe JSON-RPC frame."""


@dataclass(frozen=True)
class LspConfig:
    enabled: bool = False
    command: str = ""
    args: tuple[str, ...] = ()
    timeout_seconds: float = 20.0

    def validated(self) -> LspConfig:
        if not self.enabled:
            return LspConfig()
        command = Path(self.command).expanduser()
        if not command.is_absolute():
            raise ValueError("LSP command must be an existing absolute local file")
        command = command.resolve()
        if not command.is_file():
            raise ValueError("LSP command must be an existing absolute local file")
        if os.name != "nt" and not os.access(command, os.X_OK):
            raise ValueError("LSP command must be executable")
        if command.suffix.casefold() in _UNSAFE_WINDOWS_SCRIPT_SUFFIXES:
            raise ValueError("LSP command must be a native executable, not a shell script")
        arguments = tuple(str(value) for value in self.args)
        if len(arguments) > 32 or any("\0" in value or len(value) > 2_048 for value in arguments):
            raise ValueError("LSP arguments exceed the safe argv budget")
        timeout = float(self.timeout_seconds)
        if not 2.0 <= timeout <= 60.0:
            raise ValueError("LSP timeout must be between 2 and 60 seconds")
        return LspConfig(True, str(command), arguments, timeout)


@dataclass(frozen=True)
class LspRunResult:
    diagnostics: tuple[dict[str, Any], ...]
    server_name: str = ""
    server_version: str = ""
    message: str = ""
    cancelled: bool = False
    timed_out: bool = False


class JsonRpcFramer:
    """Strict LSP Content-Length framing over a binary stream."""

    @staticmethod
    def read(stream: BinaryIO) -> dict[str, Any] | None:
        headers: dict[bytes, bytes] = {}
        consumed = 0
        while True:
            line = stream.readline(MAX_HEADER_BYTES + 1)
            if line == b"":
                if not headers:
                    return None
                raise LspProtocolError("unexpected EOF in LSP headers")
            consumed += len(line)
            if consumed > MAX_HEADER_BYTES or not line.endswith(b"\n"):
                raise LspProtocolError("LSP header exceeds the size limit")
            line = line.rstrip(b"\r\n")
            if not line:
                break
            if b":" not in line:
                raise LspProtocolError("malformed LSP header")
            name, value = line.split(b":", 1)
            normalized = name.strip().lower()
            if normalized in headers:
                raise LspProtocolError("duplicate LSP header")
            headers[normalized] = value.strip()
        length_value = headers.get(b"content-length")
        if length_value is None or not _CONTENT_LENGTH.fullmatch(length_value):
            raise LspProtocolError("missing or invalid LSP Content-Length")
        length = int(length_value)
        if length <= 0 or length > MAX_MESSAGE_BYTES:
            raise LspProtocolError("LSP message exceeds the size limit")
        body = _read_exact(stream, length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LspProtocolError(f"invalid LSP JSON payload: {exc}") from exc
        if not isinstance(payload, dict):
            raise LspProtocolError("LSP JSON-RPC payload must be an object")
        if payload.get("jsonrpc") != "2.0":
            raise LspProtocolError("LSP payload must declare jsonrpc 2.0")
        return payload

    @staticmethod
    def write(stream: BinaryIO, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_MESSAGE_BYTES:
            raise LspProtocolError("outgoing LSP message exceeds the size limit")
        stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
        stream.write(body)
        stream.flush()


class PythonLspClient:
    """One-shot, read-only Python LSP diagnostics client.

    It opens a bounded set of workspace Python files, observes publishDiagnostics,
    rejects every server request, then performs the LSP shutdown lifecycle.
    """

    def __init__(
        self,
        workspace: str | Path,
        config: LspConfig,
        *,
        max_files: int = MAX_FILES,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_total_source_bytes: int = MAX_TOTAL_SOURCE_BYTES,
        max_diagnostics: int = MAX_DIAGNOSTICS,
        max_stderr_bytes: int = MAX_STDERR_BYTES,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("LSP workspace must be a directory")
        self.config = config.validated()
        self.max_files = max(1, min(int(max_files), MAX_FILES))
        self.max_file_bytes = max(1, min(int(max_file_bytes), MAX_FILE_BYTES))
        self.max_total_source_bytes = max(
            1, min(int(max_total_source_bytes), MAX_TOTAL_SOURCE_BYTES)
        )
        self.max_diagnostics = max(1, min(int(max_diagnostics), MAX_DIAGNOSTICS))
        self.max_stderr_bytes = max(1, min(int(max_stderr_bytes), MAX_STDERR_BYTES))

    def run(
        self,
        files: Iterable[str | Path],
        *,
        progress: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> LspRunResult:
        if not self.config.enabled:
            return LspRunResult((), message="Python LSP is disabled.")
        selected, source_budget = self._select_files(files)
        if not selected:
            return LspRunResult((), message="No eligible Python files were found for LSP.")
        argv = [self.config.command, *self.config.args]
        try:
            process = self._spawn(argv)
        except OSError as exc:
            return LspRunResult((), message=f"Could not start Python LSP: {exc}")
        messages: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue(
            maxsize=MAX_PENDING_MESSAGES
        )
        stderr_buffer = bytearray()
        reader = threading.Thread(
            target=_reader_loop, args=(process.stdout, messages), daemon=True, name="lsp-reader"
        )
        stderr_reader = threading.Thread(
            target=_stderr_loop,
            args=(process.stderr, stderr_buffer, self.max_stderr_bytes),
            daemon=True,
            name="lsp-stderr",
        )
        reader.start()
        stderr_reader.start()
        diagnostics: dict[str, list[dict[str, Any]]] = {}
        server_name = ""
        server_version = ""
        next_id = 1
        deadline = time.monotonic() + self.config.timeout_seconds
        quiet_deadline: float | None = None
        opened = 0
        opened_paths: list[Path] = []
        allowed_paths: dict[str, Path] = {}
        try:
            _emit(progress, "Starting configured Python language server...")
            JsonRpcFramer.write(
                _stdin(process),
                {
                    "jsonrpc": "2.0",
                    "id": next_id,
                    "method": "initialize",
                    "params": {
                        "processId": os.getpid(),
                        "rootUri": self.workspace.as_uri(),
                        "workspaceFolders": [
                            {"uri": self.workspace.as_uri(), "name": self.workspace.name}
                        ],
                        "capabilities": {
                            "textDocument": {
                                "publishDiagnostics": {
                                    "relatedInformation": False,
                                    "versionSupport": True,
                                }
                            },
                            "workspace": {"configuration": False},
                        },
                        "general": {"positionEncodings": ["utf-16"]},
                        "clientInfo": {"name": "StellarCode", "version": "1"},
                    },
                },
            )
            initialize = self._wait_for_response(
                process, messages, next_id, deadline, cancel_event, diagnostics
            )
            if "error" in initialize:
                raise LspProtocolError(f"LSP initialize failed: {initialize['error']!r}")
            result = initialize.get("result")
            if isinstance(result, dict):
                server_info = result.get("serverInfo")
                if isinstance(server_info, dict):
                    server_name = _bounded_text(server_info.get("name"), 200)
                    server_version = _bounded_text(server_info.get("version"), 200)
            JsonRpcFramer.write(
                _stdin(process), {"jsonrpc": "2.0", "method": "initialized", "params": {}}
            )

            for index, path in enumerate(selected, start=1):
                if _cancelled(cancel_event):
                    return LspRunResult(
                        tuple(_flatten(diagnostics, self.max_diagnostics)),
                        server_name,
                        server_version,
                        "Python LSP was cancelled.",
                        cancelled=True,
                    )
                source_budget -= path.stat().st_size
                if source_budget < 0:
                    break
                try:
                    with tokenize.open(path) as stream:
                        text = stream.read()
                except (OSError, SyntaxError, UnicodeDecodeError):
                    continue
                JsonRpcFramer.write(
                    _stdin(process),
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/didOpen",
                        "params": {
                            "textDocument": {
                                "uri": path.as_uri(),
                                "languageId": "python",
                                "version": 1,
                                "text": text,
                            }
                        },
                    },
                )
                opened += 1
                opened_paths.append(path)
                allowed_paths[str(path)] = path
                if index == 1 or index % 50 == 0 or index == len(selected):
                    _emit(progress, f"Python LSP: opened {index}/{len(selected)} file(s)...")

            # A one-shot language server has no portable "analysis complete" signal.
            # Wait until output has been quiet for a bounded grace period, within the
            # global run deadline. Servers that publish an empty list are accounted for.
            quiet_deadline = time.monotonic() + min(1.0, self.config.timeout_seconds / 4)
            published: set[str] = set()
            while time.monotonic() < deadline:
                if _cancelled(cancel_event):
                    return LspRunResult(
                        tuple(_flatten(diagnostics, self.max_diagnostics)),
                        server_name,
                        server_version,
                        "Python LSP was cancelled.",
                        cancelled=True,
                    )
                if quiet_deadline is not None and time.monotonic() >= quiet_deadline:
                    break
                timeout = min(0.1, max(0.0, deadline - time.monotonic()))
                try:
                    message = messages.get(timeout=timeout)
                except queue.Empty:
                    continue
                if isinstance(message, BaseException):
                    raise message
                if message is None:
                    raise LspProtocolError("language server exited before diagnostics completed")
                if _handle_server_message(
                    message,
                    process,
                    self.workspace,
                    diagnostics,
                    self.max_diagnostics,
                    published,
                    allowed_paths,
                ):
                    quiet_deadline = time.monotonic() + 0.5
                if opened and len(published) >= opened:
                    quiet_deadline = min(quiet_deadline or deadline, time.monotonic() + 0.1)

            message = f"Python LSP analyzed {opened} file(s)."
            if not published:
                message = (
                    f"Python LSP opened {opened} file(s), but the server did not publish "
                    "diagnostics before the bounded wait ended."
                )
            return LspRunResult(
                tuple(_flatten(diagnostics, self.max_diagnostics)),
                server_name,
                server_version,
                message,
                timed_out=time.monotonic() >= deadline,
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            detail = _bounded_text(exc, 500)
            stderr = bytes(stderr_buffer).decode("utf-8", errors="replace").strip()
            if stderr:
                detail = f"{detail}; stderr: {_bounded_text(stderr, 500)}"
            return LspRunResult(
                tuple(_flatten(diagnostics, self.max_diagnostics)),
                server_name,
                server_version,
                detail,
                cancelled=_cancelled(cancel_event),
                timed_out=isinstance(exc, TimeoutError),
            )
        finally:
            self._shutdown(
                process,
                messages,
                min(deadline, time.monotonic() + 2.0),
                next_id + 1,
                opened_paths,
                diagnostics,
                allowed_paths,
            )

    def _select_files(self, files: Iterable[str | Path]) -> tuple[list[Path], int]:
        selected: list[Path] = []
        for value in files:
            if len(selected) >= self.max_files:
                break
            path = Path(value).resolve()
            if path.suffix.casefold() not in {".py", ".pyi"}:
                continue
            if not _within(self.workspace, path) or not path.is_file() or _is_reparse_point(path):
                continue
            try:
                if path.stat().st_size > self.max_file_bytes:
                    continue
            except OSError:
                continue
            selected.append(path)
        return selected, self.max_total_source_bytes

    def _spawn(self, argv: list[str]) -> subprocess.Popen[bytes]:
        options: dict[str, Any] = {}
        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            options["start_new_session"] = True
        environment = _minimal_environment()
        return subprocess.Popen(
            argv,
            cwd=subprocess_safe_path(self.workspace),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
            creationflags=creation_flags,
            **options,
        )

    def _wait_for_response(
        self,
        process: subprocess.Popen[bytes],
        messages: queue.Queue[dict[str, Any] | BaseException | None],
        request_id: int,
        deadline: float,
        cancel_event: threading.Event | None,
        diagnostics: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        while time.monotonic() < deadline:
            if _cancelled(cancel_event):
                raise RuntimeError("Python LSP was cancelled")
            try:
                message = messages.get(timeout=min(0.1, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if isinstance(message, BaseException):
                raise message
            if message is None:
                raise LspProtocolError("language server exited before initialize completed")
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
            _handle_server_message(
                message,
                process,
                self.workspace,
                diagnostics,
                self.max_diagnostics,
                set(),
                {},
            )
        raise TimeoutError("Python LSP initialize timed out")

    def _shutdown(
        self,
        process: subprocess.Popen[bytes],
        messages: queue.Queue[dict[str, Any] | BaseException | None],
        deadline: float,
        request_id: int,
        opened_paths: list[Path],
        diagnostics: dict[str, list[dict[str, Any]]],
        allowed_paths: dict[str, Path],
    ) -> None:
        if process.poll() is not None:
            return
        try:
            for path in opened_paths:
                JsonRpcFramer.write(
                    _stdin(process),
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/didClose",
                        "params": {"textDocument": {"uri": path.as_uri()}},
                    },
                )
            JsonRpcFramer.write(
                _stdin(process),
                {"jsonrpc": "2.0", "id": request_id, "method": "shutdown", "params": None},
            )
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    message = messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    break
                if isinstance(message, dict):
                    _handle_server_message(
                        message,
                        process,
                        self.workspace,
                        diagnostics,
                        self.max_diagnostics,
                        set(),
                        allowed_paths,
                    )
            JsonRpcFramer.write(
                _stdin(process), {"jsonrpc": "2.0", "method": "exit", "params": None}
            )
            process.wait(timeout=max(0.05, deadline - time.monotonic()))
        except (BrokenPipeError, OSError, subprocess.SubprocessError, LspProtocolError):
            pass
        finally:
            _terminate_process_tree(process)


def _handle_server_message(
    message: dict[str, Any],
    process: subprocess.Popen[bytes],
    workspace: Path,
    diagnostics: dict[str, list[dict[str, Any]]],
    max_diagnostics: int,
    published: set[str],
    allowed_paths: dict[str, Path],
) -> bool:
    method = message.get("method")
    if isinstance(method, str) and "id" in message:
        # This client is intentionally read-only and non-extensible. It refuses
        # workspace edits, dynamic registration, configuration, commands and all
        # other server-to-client requests with MethodNotFound.
        JsonRpcFramer.write(
            _stdin(process),
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32601, "message": "Server requests are disabled"},
            },
        )
        return False
    if method != "textDocument/publishDiagnostics":
        return False
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    uri = params.get("uri")
    path = _safe_file_uri(workspace, uri)
    if path is None or str(path) not in allowed_paths:
        return False
    items = params.get("diagnostics")
    if not isinstance(items, list):
        return False
    version = params.get("version")
    if version is not None and version != 1:
        return False
    relative = path.relative_to(workspace).as_posix()
    parsed: list[dict[str, Any]] = []
    for item in items[:max_diagnostics]:
        diagnostic = _parse_diagnostic(workspace, path, relative, item)
        if diagnostic is not None:
            parsed.append(diagnostic)
    diagnostics[str(path)] = parsed
    published.add(str(path))
    return True


def _parse_diagnostic(
    workspace: Path, path: Path, relative: str, item: Any
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    location = item.get("range")
    if not isinstance(location, dict):
        return None
    start = location.get("start")
    end = location.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    try:
        line = max(0, int(start.get("line", 0))) + 1
        column = max(0, int(start.get("character", 0))) + 1
        end_line = max(0, int(end.get("line", line - 1))) + 1
        end_column = max(0, int(end.get("character", column - 1))) + 1
    except (TypeError, ValueError):
        return None
    severity = {1: "error", 2: "warning", 3: "info", 4: "info"}.get(item.get("severity"), "info")
    message = _bounded_text(item.get("message"), 4_000)
    if not message:
        return None
    code_value = item.get("code")
    if isinstance(code_value, dict):
        code_value = code_value.get("value")
    code = _bounded_text(code_value, 200) or "LSP"
    source_name = _bounded_text(item.get("source"), 100)
    source = f"lsp:{source_name}" if source_name else "lsp"
    identity = "\0".join((source, code, relative, str(line), str(column), message))
    import hashlib

    return {
        "id": f"diagnostic-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}",
        "severity": severity,
        "source": source,
        "code": code,
        "message": message,
        "path": str(path),
        "relative_path": relative,
        "line": line,
        "column": column,
        "end_line": end_line,
        "end_column": end_column,
    }


def _safe_file_uri(workspace: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or len(value) > 16_384:
        return None
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "file" or parsed.query or parsed.fragment:
        return None
    if parsed.netloc not in {"", "localhost"}:
        return None
    decoded = unquote(parsed.path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", decoded):
        decoded = decoded[1:]
    path = Path(decoded).resolve()
    if not _within(workspace, path) or not path.is_file() or _is_reparse_point(path):
        return None
    return path


def _reader_loop(
    stream: BinaryIO | None, target: queue.Queue[dict[str, Any] | BaseException | None]
) -> None:
    if stream is None:
        target.put(LspProtocolError("language server stdout is unavailable"))
        return
    try:
        while True:
            message = JsonRpcFramer.read(stream)
            try:
                target.put(message, timeout=0.5)
            except queue.Full:
                # Do not block forever if a hostile/noisy server outruns the
                # consumer. Make room for the terminal protocol error.
                try:
                    target.get_nowait()
                except queue.Empty:
                    pass
                try:
                    target.put_nowait(
                        LspProtocolError("language server message queue exceeded the limit")
                    )
                except queue.Full:
                    pass
                return
            if message is None:
                return
    except BaseException as exc:
        try:
            target.put(exc, timeout=0.5)
        except queue.Full:
            pass


def _stderr_loop(stream: BinaryIO | None, target: bytearray, limit: int) -> None:
    if stream is None:
        return
    while len(target) < limit:
        chunk = stream.read(min(4_096, limit - len(target)))
        if not chunk:
            return
        target.extend(chunk)


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    parts = bytearray()
    while len(parts) < length:
        chunk = stream.read(length - len(parts))
        if not chunk:
            raise LspProtocolError("unexpected EOF in LSP message body")
        parts.extend(chunk)
    return bytes(parts)


def _stdin(process: subprocess.Popen[bytes]) -> BinaryIO:
    if process.stdin is None:
        raise LspProtocolError("language server stdin is unavailable")
    return process.stdin


def _flatten(values: dict[str, list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    flattened = [item for path in sorted(values, key=str.casefold) for item in values[path]]
    return flattened[:limit]


def _within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    if path.is_symlink():
        return True
    flag = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and getattr(metadata, "st_file_attributes", 0) & flag)


def _minimal_environment() -> dict[str, str]:
    allowed = {
        "APPDATA",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update({"NO_COLOR": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    return environment


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        try:
            subprocess.run(
                [str(Path(root) / "System32" / "taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
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


def _bounded_text(value: Any, limit: int) -> str:
    value_text = _ANSI_ESCAPE.sub("", str(value or "").replace("\0", ""))
    return "".join(
        character
        for character in value_text
        if character in {"\n", "\t"} or ord(character) >= 32
    )[:limit]


def _cancelled(event: threading.Event | None) -> bool:
    return bool(event and event.is_set())


def _emit(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is None:
        return
    try:
        callback(message[:500])
    except Exception:
        pass
