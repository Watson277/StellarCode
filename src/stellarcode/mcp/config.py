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
        for path in (self.user_config, self.project_config):
            if path.is_file():
                merged.update(self._read(path))
        return merged

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
        return prepared

    def _read(self, path: Path) -> dict[str, McpServerConfig]:
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
            )
        return parsed

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
