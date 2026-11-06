"""Consolidated MCP-host registration for every ctools agent.

This is the single place that knows *how to point an agent's own MCP client at
the ctools server*. Each agent in ``ctools.agents.REGISTRY`` declares an
:func:`McpHost` (or ``None`` if it has no MCP client); the engine here reads,
writes, removes, and strips ctools entries across the config formats the agents
actually use (JSON, JSONC, YAML, TOML).

The point of this module: adding a new agent gets MCP install/check/uninstall
for free. There is no per-agent list to chase -- ``agent_mcp_hosts()`` derives
every host straight from the registry.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is an optional dependency
    yaml = None  # type: ignore[assignment]


class McpError(Exception):
    """Raised when a ctools MCP host cannot be read or written."""


#: Server names the ctools MCP server advertises; strip_ctools removes any of
#: these a user may have registered under different names.
ALL_TOOL_NAMES = (
    "ctools", "ctools-mcp",
    "list_agents", "list_sessions", "search_sessions", "export_session",
    "summarize_session", "extract_concepts", "get_session_concepts",
    "copy_concepts",
)


# ---------------------------------------------------------------------------
# McpHost: how an agent registers an MCP server
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class McpHost:
    """Where and how an agent's MCP client learns about a server.

    Attributes
    ----------
    path:
        The agent's MCP config file (already expanded to ``$HOME``).
    fmt:
        Config language: ``json`` | ``jsonc`` | ``yaml`` | ``toml``.
    container:
        Keys from the config root to the dict that directly maps
        *server name* -> *entry*.  e.g. ``("mcpServers",)`` for Claude/Pi,
        ``("mcp", "servers")`` for OpenClaw, ``("mcp_servers",)`` for Codex.
    entry:
        Shape of a single server entry, one of:
          * ``command_args``  -> ``{"command": cmd, "args": [...]}``
          * ``type_command``  -> ``{"type": "local", "command": [cmd, ...args]}``
          * ``cmd_args``      -> ``{"cmd": cmd, "args": [...], "type": "stdio"}``
    """
    path: Path
    fmt: str
    container: Tuple[str, ...]
    entry: str

    # -- identity for CLI / help ------------------------------------------
    def server_key(self, name: str) -> str:
        """Dotted key of a server entry in this host's config, e.g.
        ``mcpServers.ctools`` or ``mcp.servers.ctools``."""
        return ".".join((*self.container, name))

    def supports_format(self) -> bool:
        """True if this Python has what the engine needs to touch this file."""
        if self.fmt in ("json", "jsonc"):
            return True
        if self.fmt == "toml":
            return True
        if self.fmt == "yaml":
            return yaml is not None
        return False

    def entry_payload(self, command: str, args: Tuple[str, ...]) -> dict:
        """Build the per-server entry for this host."""
        if self.entry == "command_args":
            return {"command": command, "args": list(args)}
        if self.entry == "type_command":
            return {"type": "local", "command": [command, *args]}
        if self.entry == "cmd_args":
            return {"cmd": command, "args": list(args), "type": "stdio"}
        raise McpError(f"unknown entry shape {self.entry!r}")

    def __str__(self) -> str:  # compact, e.g. "json  ~/.pi/agent/mcp.json"
        return f"{self.fmt:6}  {self.path}"


# ---------------------------------------------------------------------------
# Loaders / dumper (json, jsonc, yaml)
# ---------------------------------------------------------------------------

def _strip_jsonc(text: str) -> str:
    """Strip // and /* */ comments and trailing commas from JSONC.

    Conservative: string literals are preserved verbatim (comment markers and
    commas inside quotes are left alone).
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    escape = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    body = "".join(out)
    # drop trailing commas before } or ]
    return re.sub(r",(\s*[}\]])", r"\1", body)


def load_config(host: McpHost) -> dict:
    """Load the host's config as a dict (empty dict if the file is absent)."""
    p = host.path
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    if host.fmt == "json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise McpError(f"{p}: invalid JSON: {e}") from e
    if host.fmt == "jsonc":
        try:
            return json.loads(_strip_jsonc(text))
        except json.JSONDecodeError as e:
            raise McpError(f"{p}: invalid JSONC: {e}") from e
    if host.fmt == "yaml":
        if yaml is None:
            raise McpError(f"PyYAML is required to edit {p} (install ctools[mcp])")
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    if host.fmt == "toml":
        try:
            data = tomllib.loads(text)
            return data if isinstance(data, dict) else {}
        except tomllib.TOMLDecodeError as e:
            raise McpError(f"{p}: invalid TOML: {e}") from e
    raise McpError(f"unsupported format {host.fmt!r}")


def dump_config(host: McpHost, root: dict) -> str:
    """Serialize ``root`` back to the host's config language."""
    if host.fmt == "json":
        return json.dumps(root, indent=2, ensure_ascii=False) + "\n"
    if host.fmt == "jsonc":
        return json.dumps(root, indent=2, ensure_ascii=False) + "\n"
    if host.fmt == "yaml":
        if yaml is None:
            raise McpError("PyYAML is required to edit YAML configs")
        return yaml.safe_dump(root, sort_keys=False, default_flow_style=False)
    if host.fmt == "toml":
        return toml_dump(root)
    raise McpError(f"unsupported format {host.fmt!r}")


def _set_path(root: dict, container: Tuple[str, ...], name: str, value) -> None:
    node = root
    for key in container:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[name] = value


def _del_path(root: dict, container: Tuple[str, ...], name: str) -> bool:
    node = root
    for key in container:
        node = node.get(key)
        if not isinstance(node, dict):
            return False
    return node.pop(name, None) is not None


def _get_path(root: dict, container: Tuple[str, ...], name: str):
    node = root
    for key in container:
        node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return None
    return node.get(name)


# ---------------------------------------------------------------------------
# TOML writer (minimal, no external dependency)
# ---------------------------------------------------------------------------

def _toml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # JSON string escaping is valid TOML for our uses
    raise McpError(f"cannot serialize {type(v).__name__} as a TOML scalar")


def _toml_value(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        inner = ", ".join(f"{_toml_key(k)} = {_toml_value(val)}" for k, val in v.items())
        return "{" + inner + "}"
    return _toml_scalar(v)


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def toml_dump(root: dict) -> str:
    """Serialize a dict to TOML. Scalars/arrays at the top level; every nested
    dict becomes a ``[section]`` table (dotted for nesting), in insertion order.
    A table's own key=value lines always precede its sub-tables (a TOML rule).
    """
    lines: list[str] = []
    for k, v in root.items():
        if not isinstance(v, dict):
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
    if lines:
        lines.append("")

    def emit(prefix, table):
        for k, v in table.items():
            if not isinstance(v, dict):
                lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
        for k, v in table.items():
            if isinstance(v, dict):
                if lines and lines[-1] != "":
                    lines.append("")
                name = f"{prefix}.{_toml_key(k)}" if prefix else _toml_key(k)
                lines.append(f"[{name}]")
                emit(name, v)

    emit("", {k: v for k, v in root.items() if isinstance(v, dict)})
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# High-level operations (used by install / check / uninstall / print-config)
# ---------------------------------------------------------------------------

def install(host: McpHost, name: str, command: str, args: Tuple[str, ...]) -> str:
    """Register ``name`` in the host's config; return the dotted server key."""
    if not host.supports_format():
        raise McpError(f"this build cannot edit {host.fmt} configs")
    root = load_config(host)
    _set_path(root, host.container, name, host.entry_payload(command, args))
    host.path.parent.mkdir(parents=True, exist_ok=True)
    host.path.write_text(dump_config(host, root), encoding="utf-8")
    return host.server_key(name)


def remove(host: McpHost, name: str) -> bool:
    """Remove ``name`` from the host's config; True if it was present."""
    root = load_config(host)
    if not _del_path(root, host.container, name):
        return False
    host.path.write_text(dump_config(host, root), encoding="utf-8")
    return True


def get_entry(host: McpHost, name: str) -> Optional[dict]:
    """Return the entry for ``name`` or None (without requiring a write)."""
    return _get_path(load_config(host), host.container, name)


def strip_ctools(host: McpHost) -> bool:
    """Remove every ctools entry the server advertises; True if any removed."""
    root = load_config(host)
    node = root
    for key in host.container:
        node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return False
    removed = False
    for n in ALL_TOOL_NAMES:
        if n in node:
            del node[n]
            removed = True
    if removed:
        host.path.write_text(dump_config(host, root), encoding="utf-8")
    return removed
