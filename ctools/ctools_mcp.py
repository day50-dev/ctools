#!/usr/bin/env python3
"""
ctools MCP server - search and manage LLM conversations from any MCP host.

Provides tools for listing agents, searching sessions, and copying concepts
between conversations. Runs over stdio for integration with Claude, opencode,
and other MCP-compatible clients.
"""

import json
import os
import re
import sys
from typing import List, Optional, Tuple

from mcp.server.fastmcp import FastMCP

from ctools.agents import Agent, AgentError, REGISTRY as AGENTS, get_agent
from ctools.cextract import concepts_to_text, extract_concepts_from_messages

mcp = FastMCP("ctools")


def _resolve(agent_name: str) -> Tuple[Optional[Agent], Optional[str]]:
    """Look up an installed agent. Returns (agent, error_message)."""
    agent = get_agent(agent_name)
    if agent is None:
        return None, f"Unknown agent: {agent_name}. Available: {', '.join(AGENTS)}"
    if not agent.exists():
        return None, f"Agent {agent_name} not found at {agent.base_path}"
    return agent, None


def _session_ref(ref: str) -> Tuple[Optional[Agent], Optional[str], Optional[str]]:
    """Resolve an '@agent/session_id' reference. Returns (agent, id, error)."""
    return _resolve_session(ref)


def _resolve_session(ref: str, agent_hint: Optional[str] = None
                    ) -> Tuple[Optional[Agent], Optional[str], Optional[str]]:
    """Resolve a session reference. Returns (agent, session_id, error).

    Accepts 'agent/session_id', '@agent/session_id', or a bare session id
    (which requires the agent hint). A bad reference always comes back as a
    self-explaining error string, never an exception.
    """
    ref = ref.lstrip("@").strip()
    name, _, session_id = ref.partition("/")
    if not session_id:
        name, session_id = "", ref
    if not name:
        name = (agent_hint or "").strip()
    if not name:
        return None, None, (
            f"Cannot determine the agent for '{ref}'. "
            f"Use 'agent/session_id' (available: {', '.join(AGENTS)})"
        )
    agent, error = _resolve(name)
    if error:
        return None, None, error
    return agent, session_id, None


def _is_session_ref(s: str) -> bool:
    """True if s looks like a session reference ('agent/id' or '@agent/id')."""
    s = s.lstrip("@")
    name, _, session_id = s.partition("/")
    return bool(session_id) and name in AGENTS


def _read_concepts(agent: Agent, session_id: str) -> Tuple[Optional[list], Optional[str]]:
    """Extract concepts from a session. Returns (concepts, error_message)."""
    try:
        messages = agent.raw_messages(session_id)
    except AgentError as exc:
        return None, str(exc)
    if not messages:
        return None, f"Session not found: {agent.name}/{session_id}"
    return extract_concepts_from_messages(messages), None


# --- Tools ---

@mcp.tool()
def list_agents() -> str:
    """List all supported LLM agents and whether they are installed."""
    return "\n".join(
        f"{agent.name}: {agent.description} "
        f"[{'installed' if agent.exists() else 'not found'}] ({agent.storage_format})"
        for agent in AGENTS.values()
    )


@mcp.tool()
def list_sessions(agent: str, sort: str = "time") -> str:
    """List conversation sessions for an agent.

    Subagent sessions are listed nested under their parent, not as
    top-level entries.

    Args:
        agent: Agent name (claude, claude-code, opencode, codex, pi, goose)
        sort: Sort by 'time' or 'size'
    """
    resolved, error = _resolve(agent)
    if error:
        return error

    try:
        sessions = resolved.sessions()
    except AgentError as exc:
        return str(exc)
    if not sessions:
        return f"No sessions found for {agent}"

    if sort == "size":
        sessions.sort(key=lambda s: s.size, reverse=True)
    else:
        sessions.sort(key=lambda s: s.mtime or s.ctime, reverse=True)

    # Subagent sessions (parent_id set) are listed nested under their
    # parent, not as top-level entries.
    children = {}
    top_level = []
    for s in sessions:
        if s.parent_id:
            children.setdefault(s.parent_id, []).append(s)
        else:
            top_level.append(s)

    def _line(s, indent=""):
        mtime = s.mtime.strftime("%Y-%m-%d %H:%M") if s.mtime else "N/A"
        msgs = f"{s.message_count} msgs" if s.message_count else ""
        return f"{indent}{s.id}  {s.name[:50]}  {mtime}  {msgs}"

    lines = [f"Sessions for {agent} ({len(sessions)} total):"]
    shown = set()
    for s in top_level[:50]:
        lines.append(_line(s, "  "))
        shown.add(s.id)
        for child in children.get(s.id, []):
            lines.append(_line(child, "    └ "))
            shown.add(child.id)
    # Subagents whose parent is not in the list are shown top-level.
    for pid, kids in children.items():
        if pid not in shown:
            for child in kids:
                lines.append(_line(child, "  "))
                shown.add(child.id)

    if len(sessions) > 50:
        lines.append(f"  ... and {len(sessions) - 50} more")

    return "\n".join(lines)


@mcp.tool()
def search_sessions(
    pattern: str,
    agents: str = "*",
    ignore_case: bool = False,
    max_results: int = 50,
) -> str:
    """Search through conversation content across agents using regex patterns.

    Args:
        pattern: PCRE regex pattern to search for
        agents: Agent(s) to search, comma-separated or '*' for all (e.g. 'opencode' or 'opencode,claude-code')
        ignore_case: Case-insensitive search
        max_results: Maximum number of matches to return
    """
    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        return f"Invalid pattern: {e}"

    if agents == "*":
        targets: List[Agent] = list(AGENTS.values())
    else:
        targets = []
        for name in (a.strip() for a in agents.split(",")):
            agent = get_agent(name)
            if agent is None:
                return f"Unknown agent: {name}. Available: {', '.join(AGENTS)}"
            targets.append(agent)

    matches = []
    for agent in targets:
        if not agent.exists():
            continue
        try:
            sessions = agent.sessions()
        except AgentError:
            continue
        for session in sessions:
            try:
                lines = agent.lines(session.id)
            except AgentError:
                continue
            for line_num, line in lines:
                if compiled.search(line):
                    matches.append(f"{agent.name}/{session.id}:{line_num}: {line}")
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break

    if not matches:
        return f"No matches found for '{pattern}'"

    header = f"Found {len(matches)} match(es) for '{pattern}':"
    if len(matches) >= max_results:
        header += f" (capped at {max_results})"

    return header + "\n" + "\n".join(matches)


@mcp.tool()
def export_session(session: str, agent: Optional[str] = None,
                  max_messages: int = 100) -> str:
    """Export messages from a conversation session.

    Args:
        session: Session reference 'agent/session_id' (or a bare id with agent)
        agent: Agent name, when session is a bare id
        max_messages: Maximum number of messages to return
    """
    resolved, session_id, error = _resolve_session(session, agent)
    if error:
        return error

    try:
        messages = resolved.messages(session_id)
    except AgentError as exc:
        return str(exc)
    if not messages:
        return f"Session not found: {resolved.name}/{session_id}"

    lines = [f"Session: {resolved.name}/{session_id} ({len(messages)} messages)"]
    for msg in messages[:max_messages]:
        content = msg.content[:200] + "..." if len(msg.content) > 200 else msg.content
        lines.append(f"[{msg.role}] {content}")

    if len(messages) > max_messages:
        lines.append(f"... and {len(messages) - max_messages} more messages")

    return "\n".join(lines)


@mcp.tool()
def summarize_session(session: str, agent: Optional[str] = None) -> str:
    """Quick orientation for a session: title, date, size, first user message,
    and last assistant message. Use this to read up on a session cheaply; use
    export_session for the full transcript.

    Args:
        session: Session reference 'agent/session_id' (or a bare id with agent)
        agent: Agent name, when session is a bare id
    """
    resolved, session_id, error = _resolve_session(session, agent)
    if error:
        return error

    meta = resolved.session(session_id)
    if meta is None:
        return f"Session not found: {resolved.name}/{session_id}"

    try:
        messages = resolved.messages(session_id)
    except AgentError as exc:
        return str(exc)

    first_user = next((m for m in messages if m.role == "user"), None)
    last_assistant = next((m for m in reversed(messages)
                           if m.role == "assistant"), None)

    lines = [f"Session: {resolved.name}/{session_id}"]
    bits = []
    if meta.name:
        bits.append(f"title: {meta.name}")
    if meta.mtime:
        bits.append(f"modified: {meta.mtime.strftime('%Y-%m-%d %H:%M')}")
    if meta.message_count:
        bits.append(f"{meta.message_count} messages")
    if bits:
        lines.append("  " + " | ".join(bits))
    if first_user:
        lines.append(f"First user message: {first_user.content[:300]}")
    if last_assistant:
        lines.append(f"Last assistant message: {last_assistant.content[:300]}")
    return "\n".join(lines)


@mcp.tool()
def extract_concepts(session: str, agent: Optional[str] = None) -> str:
    """Extract concepts (constraints, goals, preferences) from a session.

    Scans conversation messages for 'Use the following <type>: <text>' patterns
    and returns them as structured concept objects.

    Args:
        session: Session reference 'agent/session_id' (or a bare id with agent)
        agent: Agent name, when session is a bare id
    """
    resolved, session_id, error = _resolve_session(session, agent)
    if error:
        return error

    concepts, error = _read_concepts(resolved, session_id)
    if error:
        return error
    if not concepts:
        return (
            f"No explicit concept markers in {resolved.name}/{session_id}. "
            "This tool only scans for 'Use the following <type>:' blocks; "
            "use summarize_session for a general overview."
        )

    lines = [f"Found {len(concepts)} concept(s) in {resolved.name}/{session_id}:"]
    lines.extend(f"  [{c['type']}] {c['short'][:100]}" for c in concepts)
    return "\n".join(lines)


@mcp.tool()
def copy_concepts(source: str, destination: str) -> str:
    """Copy concepts between sessions or concept files.

    Args:
        source: Source reference (e.g. '@opencode/ses_abc' or path to concept JSON file)
        destination: Destination reference (e.g. '@claude-code/ses_xyz' or path to concept JSON file)
    """
    if _is_session_ref(source):
        agent, session_id, error = _resolve_session(source)
        if error:
            return error
        concepts, error = _read_concepts(agent, session_id)
        if error:
            return error
    elif source.endswith(".json"):
        try:
            with open(source) as f:
                concepts = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            return f"Error reading {source}: {e}"
    else:
        return f"Unknown source type: {source}"

    if not concepts:
        return "No concepts found in source"

    if _is_session_ref(destination):
        agent, session_id, error = _resolve_session(destination)
        if error:
            return error
        try:
            agent.inject_system(session_id, concepts_to_text(concepts))
        except AgentError as exc:
            return f"Could not inject into {destination}: {exc}"
        return f"Injected {len(concepts)} concept(s) into {destination}"

    if destination.endswith(".json"):
        try:
            with open(destination, "w") as f:
                json.dump(concepts, f, indent=2)
                f.write("\n")
        except OSError as e:
            return f"Error writing {destination}: {e}"
        return f"Wrote {len(concepts)} concept(s) to {destination}"

    return f"Unknown destination type: {destination}"


@mcp.tool()
def get_session_concepts(session: str, agent: Optional[str] = None,
                        concept_type: str = "") -> str:
    """Get concepts from a session, optionally filtered by type.

    Args:
        session: Session reference 'agent/session_id' (or a bare id with agent)
        agent: Agent name, when session is a bare id
        concept_type: Filter by type (constraint, goal, preference, observation, reference). Empty for all.
    """
    resolved, session_id, error = _resolve_session(session, agent)
    if error:
        return error

    concepts, error = _read_concepts(resolved, session_id)
    if error:
        return error

    if concept_type:
        concepts = [c for c in concepts if c["type"] == concept_type]

    if not concepts:
        suffix = f" (type={concept_type})" if concept_type else ""
        return f"No concepts found in {resolved.name}/{session_id}{suffix}"

    lines = [f"Found {len(concepts)} concept(s) in {resolved.name}/{session_id}:"]
    lines.extend(json.dumps(c, indent=2) for c in concepts)
    return "\n".join(lines)


def _find_uv_tool_python() -> Optional[str]:
    """Return the venv interpreter from a `uv tool install ctxttools` if present."""
    import os
    uv_tools = os.path.expanduser("~/.local/share/uv/tools/ctxttools/bin/python")
    if os.path.exists(uv_tools):
        return uv_tools
    return None


def _find_pipx_python() -> Optional[str]:
    """Return the venv interpreter from a pipx install if present."""
    import os
    pipx_py = os.path.expanduser("~/.local/pipx/venvs/ctxttools/bin/python")
    if os.path.exists(pipx_py):
        return pipx_py
    return None


def _best_python() -> str:
    """Pick the best interpreter that has ctxtools[mcp] installed."""
    import sys
    for finder in (_find_uv_tool_python, _find_pipx_python):
        p = finder()
        if p:
            return p
    # Fall back to the running interpreter (e.g. from a venv or editable install)
    return sys.executable


def _verify_python(py: str) -> bool:
    """Check that `py -m ctools.ctools_mcp` starts and can import itself.

    Runs a subprocess with stdin closed so the MCP stdio server exits
    immediately (it would otherwise block reading stdin).
    """
    import subprocess
    result = subprocess.run(
        [py, "-m", "ctools.ctools_mcp"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15,
    )
    # Exit 0 means it started cleanly (and exited because stdin was closed).
    return result.returncode == 0


def _portable(path: str) -> str:
    """Prefer a '~'-relative form so the config survives user moves."""
    import os
    home = os.path.expanduser("~")
    if path.startswith(home + os.sep):
        return "~/" + path[len(home) + 1:]
    return path


SERVER_DESC = "Search, list, and copy LLM agent sessions (pi, claude, opencode, codex, goose, etc.)"


def _entry_split(py: str) -> dict:
    """The ctools server entry in the {command, args} shape (pi, claude)."""
    return {
        "command": _portable(py),
        "args": ["-m", "ctools.ctools_mcp"],
        "description": SERVER_DESC,
    }


def _entry_opencode(py: str) -> dict:
    """The ctools server entry in opencode's {command: [cmd, ...]} shape."""
    return {"type": "local",
            "command": [_portable(py), "-m", "ctools.ctools_mcp"],
            "description": SERVER_DESC}


def _entry_goose(py: str) -> dict:
    """The ctools extension entry for goose's YAML config (type: stdio)."""
    return {
        "enabled": True, "type": "stdio", "name": "ctools",
        "description": SERVER_DESC, "cmd": _portable(py),
        "args": ["-m", "ctools.ctools_mcp"],
        "envs": {}, "env_keys": [], "timeout": 300,
        "bundled": None, "available_tools": [],
    }


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


class _Target:
    """One client config file: knows its own path and entry shape, and can
    register / unregister / read the ctools entry in that native form."""

    def __init__(self, client, path, entry_builder, container_key="mcpServers"):
        self.client = client
        self.path = path
        self._build = entry_builder
        self.container_key = container_key

    def label(self):
        return self.client

    def register(self, py: str) -> None:
        data = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                data = json.load(f)
        data.setdefault(self.container_key, {})["ctools"] = self._build(py)
        _write_json(self.path, data)

    def unregister(self) -> bool:
        if not os.path.exists(self.path):
            return False
        with open(self.path) as f:
            data = json.load(f)
        if "ctools" in data.get(self.container_key, {}):
            del data[self.container_key]["ctools"]
            _write_json(self.path, data)
            return True
        return False

    def get_entry(self):
        """Return (command, args) if a ctools entry exists, else None."""
        if not os.path.exists(self.path):
            return None
        with open(self.path) as f:
            data = json.load(f)
        entry = data.get(self.container_key, {}).get("ctools")
        if not isinstance(entry, dict):
            return None
        cmd = entry.get("command")
        if isinstance(cmd, list) and cmd:      # opencode style
            return (cmd[0], list(cmd[1:]))
        if isinstance(cmd, str) and cmd:       # {command, args} style
            return (cmd, list(entry.get("args", [])))
        return None


_PI_GLOBAL = os.path.expanduser("~/.pi/agent/mcp.json")
_PI_PROJECT = os.path.join(os.getcwd(), ".pi", "mcp.json")


class _GooseTarget:
    """Goose's config is YAML (~/.config/goose/config.yaml, extensions: <name>:).

    PyYAML is required; it is found in this interpreter or a ctxttools env
    (uv tool / pipx) and loaded via sys.path so the module works either way.
    """

    path = os.path.expanduser("~/.config/goose/config.yaml")

    def __init__(self):
        self.client = "goose"
        self._py = _yaml_interpreter()

    def _yaml(self):
        import yaml
        return yaml

    def _load(self) -> dict:
        yaml = self._yaml()
        if os.path.exists(self.path):
            with open(self.path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def _dump(self, data) -> None:
        yaml = self._yaml()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    def label(self):
        return self.client

    def register(self, py: str) -> None:
        data = self._load()
        data.setdefault("extensions", {})["ctools"] = _entry_goose(py)
        self._dump(data)

    def unregister(self) -> bool:
        if not os.path.exists(self.path):
            return False
        data = self._load()
        if "ctools" in data.get("extensions", {}):
            del data["extensions"]["ctools"]
            self._dump(data)
            return True
        return False

    def get_entry(self):
        if not os.path.exists(self.path):
            return None
        entry = self._load().get("extensions", {}).get("ctools")
        if not isinstance(entry, dict) or not entry.get("cmd"):
            return None
        return (entry["cmd"], list(entry.get("args", [])))


def _yaml_interpreter() -> Optional[str]:
    """An interpreter that has PyYAML: this one, or a ctxttools env.

    Adds its site-packages to sys.path so `import yaml` works in-process.
    """
    import subprocess
    for py in [sys.executable,
               _find_uv_tool_python(), _find_pipx_python()]:
        if not py:
            continue
        try:
            sp = subprocess.run(
                [py, "-c",
                 "import yaml,os; print(os.path.dirname(os.path.dirname(yaml.__file__)))"],
                capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if not sp:            # this interpreter cannot import yaml
            continue
        if sp not in sys.path:
            sys.path.insert(0, sp)
        return py
    return None


def _all_targets():
    """Every known client target, in display order (goose only if PyYAML is present)."""
    targets = [
        _Target("pi", _PI_GLOBAL, _entry_split),
        _Target("claude", os.path.expanduser("~/.claude.json"), _entry_split),
        _Target("opencode", os.path.expanduser("~/.config/opencode/opencode.json"),
                _entry_opencode, container_key="mcp"),
    ]
    if _yaml_interpreter():
        targets.append(_GooseTarget())
    return targets


def _select_targets(args) -> list:
    """Which targets a command applies to, from the --client flags."""
    by_name = {t.client: t for t in _all_targets()}
    flags = ("pi", "claude", "opencode", "goose")
    if getattr(args, "goose", False) and "goose" not in by_name:
        raise SystemExit(
            "goose support needs PyYAML (its config is YAML). "
            "Easiest: uv tool install \".[mcp]\" (the mcp extra now includes pyyaml), "
            "or: uv pip install --python ~/.local/share/uv/tools/ctxttools/bin/python pyyaml")
    requested = [name for name in flags if getattr(args, name, False)]
    if getattr(args, "project", False):
        requested.append("pi-project")
    if not requested:
        # Default: pi (project-level when asked), plus every other client
        # that already has a config file on this machine.
        requested = ["pi-project" if getattr(args, "project", False) else "pi"]
        for name in ("claude", "opencode", "goose"):
            if name in by_name and os.path.exists(by_name[name].path):
                requested.append(name)
    out = []
    for name in requested:
        if name == "pi-project":
            out.append(_Target("pi (project)", _PI_PROJECT, _entry_split))
        else:
            out.append(by_name[name])
    return out


def cmd_install(args) -> None:
    """Ensure ctxtools[mcp] is installed and register the server with each selected client."""
    import subprocess, sys

    py = _best_python()
    if not _verify_python(py):
        print("No working ctxtools[mcp] install found. Installing with uv...")
        subprocess.run(["uv", "tool", "install", "ctxttools[mcp]", "--force"], check=True)
        py = _best_python()
        if not _verify_python(py):
            print(f"ERROR: cannot import ctools.ctools_mcp with {py}", file=sys.stderr)
            sys.exit(1)

    for target in _select_targets(args):
        target.register(py)
        print(f"Registered ctools MCP server with {target.label()}.")
        print(f"  config:  {target.path}")
        print(f"  command: {_portable(py)} -m ctools.ctools_mcp")
    print("")
    print("Verify:  ctools mcp check")
    print("Use in pi:  /mcp, or just ask the agent - tools are also reachable via codemode.")


def _verify_command(command: str, args: list) -> bool:
    """Check that an MCP server command starts and exits cleanly with stdin
    closed (a stdio server exits when stdin closes)."""
    import subprocess
    try:
        result = subprocess.run(
            [command, *args],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  start check failed: {e}")
        return False
    return result.returncode == 0


def cmd_check(args) -> None:
    """Verify the ctools entries in the selected clients' MCP configs."""
    import sys

    ok = True
    for target in _select_targets(args):
        flag = target.label().split()[0]
        fix = f"ctools mcp install --{flag}"
        entry = target.get_entry()
        if entry is None:
            print(f"{target.label()}: no ctools entry")
            print(f"  fix: {fix}")
            ok = False
            continue
        command, args_l = entry
        command = os.path.expanduser(command)
        if not os.path.exists(command):
            print(f"{target.label()}: command not found: {command}")
            print(f"  fix: {fix}")
            ok = False
            continue
        good = _verify_command(command, args_l)
        print(f"{target.label()}: {'OK' if good else 'FAILED'}  "
              f"({command} {' '.join(args_l)})")
        if not good:
            print(f"  fix: {fix}")
            ok = False
    if ok:
        print("All ctools MCP entries check out.")
    sys.exit(0 if ok else 1)


def cmd_uninstall(args) -> None:
    """Remove the ctools entry from the selected clients' configs."""
    removed = []
    for target in _select_targets(args):
        if target.unregister():
            removed.append(target.label())
    if removed:
        print("Removed ctools from: " + ", ".join(removed))
    else:
        print("ctools not found in any selected MCP config")


def cmd_print_config(args) -> None:
    """Print the config snippet for a client (default: all four)."""
    py = _portable(_best_python())
    clients = ([args.client] if getattr(args, "client", None)
               else ["pi", "claude", "opencode", "goose"])
    builders = {"pi": _entry_split, "claude": _entry_split,
                "opencode": _entry_opencode, "goose": _entry_goose}
    containers = {"pi": "mcpServers", "claude": "mcpServers",
                  "opencode": "mcp", "goose": "extensions"}
    paths = {"pi": "~/.pi/agent/mcp.json", "claude": "~/.claude.json",
             "opencode": "~/.config/opencode/opencode.json",
             "goose": "~/.config/goose/config.yaml"}
    for client in clients:
        snippet = {containers[client]: {"ctools": builders[client](py)}}
        print(f"# {client}:  {paths[client]}")
        print(json.dumps(snippet, indent=2))
        print("")


def _add_client_flags(parser, with_project=False):
    if with_project:
        parser.add_argument("--project", action="store_true",
                            help="pi: register in .pi/mcp.json in the current directory")
    parser.add_argument("--pi", action="store_true", help="target pi")
    parser.add_argument("--claude", action="store_true",
                        help="target Claude Code (~/.claude.json)")
    parser.add_argument("--opencode", action="store_true",
                        help="target opencode (~/.config/opencode/opencode.json)")
    parser.add_argument("--goose", action="store_true",
                        help="target goose (~/.config/goose/config.yaml)")


def main():
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        rest = sys.argv[2:]
        if cmd == "install":
            import argparse
            parser = argparse.ArgumentParser(prog="ctools mcp install")
            _add_client_flags(parser, with_project=True)
            parser.add_argument("--all", action="store_true",
                                help="all clients (pi, claude, opencode, goose)")
            args = parser.parse_args(rest)
            if getattr(args, "all", False):
                args.pi = args.claude = args.opencode = args.goose = True
            elif getattr(args, "project", False) and not any(
                    getattr(args, f, False) for f in ("pi", "claude", "opencode", "goose")):
                args.pi = True  # --project means "pi, in this project"
            cmd_install(args)
            return
        if cmd == "check":
            import argparse
            parser = argparse.ArgumentParser(prog="ctools mcp check")
            _add_client_flags(parser, with_project=True)
            parser.add_argument("--all", action="store_true",
                                help="all clients (pi, claude, opencode, goose)")
            args = parser.parse_args(rest)
            if getattr(args, "all", False):
                args.pi = args.claude = args.opencode = args.goose = True
            elif getattr(args, "project", False) and not any(
                    getattr(args, f, False) for f in ("pi", "claude", "opencode", "goose")):
                args.pi = True
            cmd_check(args)
            return
        if cmd == "uninstall":
            import argparse
            parser = argparse.ArgumentParser(prog="ctools mcp uninstall")
            _add_client_flags(parser, with_project=True)
            parser.add_argument("--all", action="store_true",
                                help="all clients (pi, claude, opencode, goose)")
            args = parser.parse_args(rest)
            if getattr(args, "all", False):
                args.pi = args.claude = args.opencode = args.goose = True
            cmd_uninstall(args)
            return
        if cmd == "print-config":
            import argparse
            parser = argparse.ArgumentParser(prog="ctools mcp print-config")
            _add_client_flags(parser)
            args = parser.parse_args(rest)
            chosen = [k for k in ("pi", "claude", "opencode", "goose")
                      if getattr(args, k, False)]
            args.client = chosen[0] if len(chosen) == 1 else None
            cmd_print_config(args)
            return
        print(f"Unknown subcommand: {cmd}", file=sys.stderr)
        print("Usage: ctools mcp [install | check | uninstall | print-config] "
              "[--pi --claude --opencode --goose | --all]", file=sys.stderr)
        sys.exit(1)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
