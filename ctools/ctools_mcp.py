#!/usr/bin/env python3
"""
ctools MCP server - search and manage LLM conversations from any MCP host.

Provides tools for listing agents, searching sessions, and copying concepts
between conversations. Runs over stdio for integration with Claude, opencode,
and other MCP-compatible clients.
"""

import json
import re
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


def _register(path: str, py: str) -> None:
    """Write/replace the ctools entry in an mcp.json file."""
    import os, json

    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {
        "command": _portable(py),
        "args": ["-m", "ctools.ctools_mcp"],
        "description": "Search, list, and copy LLM agent sessions (pi, claude, opencode, codex, goose, etc.)",
    }
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
    else:
        existing = {}
    existing.setdefault("mcpServers", {})[
        "ctools"] = entry

    with open(path, "w") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")


def cmd_install(args) -> None:
    """Ensure ctxtools[mcp] is installed and register the server with pi."""
    import os, subprocess, sys

    py = _best_python()
    if not _verify_python(py):
        print("No working ctxtools[mcp] install found. Installing with uv...")
        subprocess.run(["uv", "tool", "install", "ctxttools[mcp]", "--force"], check=True)
        py = _best_python()
        if not _verify_python(py):
            print(f"ERROR: cannot import ctools.ctools_mcp with {py}", file=sys.stderr)
            sys.exit(1)

    target = (
        os.path.join(os.getcwd(), ".pi", "mcp.json")
        if getattr(args, "project", False)
        else os.path.expanduser("~/.pi/agent/mcp.json")
    )
    _register(target, py)
    print("Registered ctools MCP server with pi.")
    print(f"  python: {py}")
    print(f"  config: {target}")
    print("")
    print("Verify with:  pi mcp list")
    print("Use in pi:     /mcp, or just ask the agent - tools are also reachable via codemode.")


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


def cmd_check() -> None:
    """Verify the ctools entries in pi's MCP configs (global and project)."""
    import os, sys

    targets = [
        (os.path.expanduser("~/.pi/agent/mcp.json"), "ctools-mcp install"),
        (os.path.join(os.getcwd(), ".pi", "mcp.json"), "ctools-mcp install --project"),
    ]
    ok = True
    for path, fix in targets:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"{path}: cannot read ({e})")
            ok = False
            continue
        entry = data.get("mcpServers", {}).get("ctools")
        if not entry:
            print(f"{path}: no 'ctools' entry")
            print(f"  fix: {fix}")
            ok = False
            continue
        command = os.path.expanduser(entry.get("command", ""))
        args = entry.get("args", [])
        if not os.path.exists(command):
            print(f"{path}: command not found: {entry.get('command')}")
            print(f"  fix: {fix}")
            ok = False
            continue
        good = _verify_command(command, args)
        print(f"{path}: {'OK' if good else 'FAILED'}  "
              f"({entry.get('command')} {' '.join(args)})")
        if not good:
            print(f"  fix: {fix}")
            ok = False
    if ok:
        print("All ctools MCP entries check out.")
    sys.exit(0 if ok else 1)


def cmd_uninstall(args) -> None:
    """Remove ctools from pi's mcp.json (and optionally other clients with --all)."""
    import os, json

    removed = []

    pi_mcp_json = os.path.expanduser("~/.pi/agent/mcp.json")
    if os.path.exists(pi_mcp_json):
        with open(pi_mcp_json) as f:
            data = json.load(f)
        if "ctools" in data.get("mcpServers", {}):
            del data["mcpServers"]["ctools"]
            with open(pi_mcp_json, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            removed.append(pi_mcp_json)

    if getattr(args, "all", False):
        for path, key in [
            (os.path.expanduser("~/.claude.json"), "mcpServers"),
            (os.path.expanduser("~/.config/opencode/opencode.json"), "mcp"),
        ]:
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                if "ctools" in data.get(key, {}):
                    del data[key]["ctools"]
                    with open(path, "w") as f:
                        json.dump(data, f, indent=2)
                        f.write("\n")
                    removed.append(path)

    if removed:
        print("Removed ctools from: " + ", ".join(removed))
    else:
        print("ctools not found in any MCP config")


def cmd_print_config(args) -> None:
    """Print the mcpServers JSON snippet for any MCP client."""
    import json
    cfg = {
        "mcpServers": {
            "ctools": {
                "command": _portable(_best_python()),
                "args": ["-m", "ctools.ctools_mcp"],
                "description": "Search, list, and copy LLM agent sessions",
            }
        }
    }
    print(json.dumps(cfg, indent=2))


def main():
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        rest = sys.argv[2:]
        if cmd == "install":
            import argparse
            parser = argparse.ArgumentParser(prog="ctools-mcp install")
            parser.add_argument("--project", action="store_true",
                               help="register in .pi/mcp.json in the current directory")
            cmd_install(parser.parse_args(rest))
            return
        if cmd == "check":
            cmd_check()
            return
        if cmd == "uninstall":
            import argparse
            parser = argparse.ArgumentParser(prog="ctools-mcp uninstall")
            parser.add_argument("--all", action="store_true",
                                help="also remove Claude Code and opencode entries")
            cmd_uninstall(parser.parse_args(rest))
            return
        if cmd == "print-config":
            cmd_print_config(None)
            return
        print(f"Unknown subcommand: {cmd}", file=sys.stderr)
        print("Usage: ctools-mcp [install [--project] | uninstall [--all] | check | print-config]", file=sys.stderr)
        sys.exit(1)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
