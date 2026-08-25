"""Load, merge, validate, and prepare user/project MCP server configuration.

Configuration is data only: command spawning happens later in ``McpServerManager`` so
validation and secret interpolation can be audited before a server starts.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import dotenv_values


_VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_CHROME_DEVTOOLS_SERVER = {
    "command": "npx",
    "args": ["-y", "chrome-devtools-mcp@latest", "--isolated=true"],
}


class McpConfigError(ValueError):
    """Raised when an MCP configuration file or server entry is invalid."""


@dataclass
class McpServerConfig:
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    disabled: bool = False
    source: str = ""

    @property
    def is_stdio(self) -> bool:
        return bool(self.command.strip())

    @property
    def is_http(self) -> bool:
        return bool(self.url.strip())

    @property
    def transport_name(self) -> str:
        return "http" if self.is_http else "stdio"


class McpConfigLoader:
    def __init__(
        self,
        project_dir: str | Path,
        user_config: str | Path | None = None,
        project_config: str | Path | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.user_config = Path(user_config or Path.home() / ".stellarcode" / "mcp.json")
        self.project_config = Path(
            project_config or self.project_dir / ".stellarcode" / "mcp.json"
        )

    def bootstrap_chrome_devtools(self) -> str:
        """Create the user config once, without changing an existing file."""
        if not self.user_config.exists():
            self.user_config.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "mcpServers": {
                    "chrome-devtools": DEFAULT_CHROME_DEVTOOLS_SERVER,
                }
            }
            try:
                with self.user_config.open("x", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
            except FileExistsError:
                pass
            else:
                return (
                    f"Created default MCP config: {self.user_config} "
                    "(chrome-devtools, isolated mode)"
                )

        try:
            raw = json.loads(self.user_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        servers = raw.get("mcpServers") if isinstance(raw, dict) else None
        if isinstance(servers, dict) and "chrome-devtools" not in servers:
            return (
                f"MCP hint: {self.user_config} does not configure chrome-devtools; "
                "see README.md for the isolated browser setup."
            )
        return ""

    def load(self) -> dict[str, McpServerConfig]:
        merged: dict[str, McpServerConfig] = {}
        for path, source in (
            (self.user_config, "user"),
            (self.project_config, "project"),
        ):
            if path.is_file():
                merged.update(self._read(path, source))
        return merged

    def install_project_server(
        self,
        name: str,
        config: McpServerConfig,
        *,
        overwrite: bool = False,
    ) -> McpServerConfig:
        normalized_name = _server_name(name)
        config = replace(config, source="project")
        self.prepare(config)
        document = self._project_document()
        servers = document.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise McpConfigError(
                f"MCP config {self.project_config} must contain an mcpServers object."
            )
        if normalized_name in servers and not overwrite:
            raise McpConfigError(
                f"Project MCP server already exists: {normalized_name}"
            )
        servers[normalized_name] = _serialize_config(config)
        self._write_project_document(document)
        return config

    def remove_project_server(self, name: str) -> None:
        normalized_name = _server_name(name)
        document = self._project_document()
        servers = document.get("mcpServers")
        if not isinstance(servers, dict) or normalized_name not in servers:
            raise McpConfigError(
                f"Project MCP server not found: {normalized_name}"
            )
        del servers[normalized_name]
        self._write_project_document(document)

    def prepare(self, config: McpServerConfig) -> McpServerConfig:
        prepared = replace(
            config,
            command=self._expand(config.command),
            args=[self._expand(value) for value in config.args],
            env={key: self._expand(value) for key, value in config.env.items()},
            url=self._expand(config.url),
            headers={key: self._expand(value) for key, value in config.headers.items()},
        )
        if prepared.is_stdio == prepared.is_http:
            raise McpConfigError(
                "An MCP server must configure exactly one of 'command' or 'url'."
            )
        if prepared.is_http and not prepared.url.lower().startswith(("http://", "https://")):
            raise McpConfigError("MCP server URL must use http:// or https://.")
        return prepared

    def _read(self, path: Path, source: str = "") -> dict[str, McpServerConfig]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise McpConfigError(f"Could not read MCP config {path}: {exc}") from exc
        servers = raw.get("mcpServers") if isinstance(raw, dict) else None
        if not isinstance(servers, dict):
            raise McpConfigError(f"MCP config {path} must contain an mcpServers object.")

        parsed: dict[str, McpServerConfig] = {}
        for name, value in servers.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
                raise McpConfigError(f"Invalid MCP server entry in {path}: {name!r}")
            parsed[name.strip()] = McpServerConfig(
                command=_string(value.get("command")),
                args=_string_list(value.get("args")),
                env=_string_map(value.get("env")),
                url=_string(value.get("url")),
                headers=_string_map(value.get("headers")),
                disabled=bool(value.get("disabled", False)),
                source=source,
            )
        return parsed

    def _project_document(self) -> dict[str, object]:
        if not self.project_config.exists():
            return {"mcpServers": {}}
        try:
            document = json.loads(self.project_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise McpConfigError(
                f"Could not read MCP config {self.project_config}: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise McpConfigError(f"MCP config {self.project_config} must be an object.")
        return document

    def _write_project_document(self, document: dict[str, object]) -> None:
        self.project_config.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.project_config.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.project_config)
        except OSError as exc:
            raise McpConfigError(
                f"Could not write MCP config {self.project_config}: {exc}"
            ) from exc

    def _expand(self, raw: str) -> str:
        if not raw:
            return raw

        def replace_variable(match: re.Match[str]) -> str:
            name = match.group(1)
            value = self._configured_value(name)
            if value is None or not value.strip():
                raise McpConfigError(
                    f"MCP configuration references an unset variable: {name}"
                )
            return value

        return _VARIABLE_PATTERN.sub(replace_variable, raw)

    def _configured_value(self, name: str) -> str | None:
        if name == "PROJECT_DIR":
            return str(self.project_dir)
        if name == "HOME":
            return str(Path.home())
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
        for path in (self.project_dir / ".env", Path.home() / ".env"):
            if not path.is_file():
                continue
            value = dotenv_values(path).get(name)
            if value and value.strip():
                return value.strip()
        return None


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise McpConfigError("MCP server 'args' must be an array of strings.")
    return list(value)


def _string_map(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise McpConfigError("MCP server env/headers must map strings to strings.")
    return dict(value)


def _server_name(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        raise McpConfigError("MCP server name must contain 1-64 characters.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", normalized):
        raise McpConfigError(
            "MCP server name may contain only letters, numbers, '_' and '-'."
        )
    return normalized


def _serialize_config(config: McpServerConfig) -> dict[str, object]:
    payload: dict[str, object] = {"disabled": config.disabled}
    if config.is_stdio:
        payload["command"] = config.command
        if config.args:
            payload["args"] = list(config.args)
        if config.env:
            payload["env"] = dict(config.env)
    else:
        payload["url"] = config.url
        if config.headers:
            payload["headers"] = dict(config.headers)
    return payload
