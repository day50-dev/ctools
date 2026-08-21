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

from ctools.agents import Agent, AgentError, REGISTRY as AGENTS
from ctools.ccopy import concepts_to_text, extract_concepts_from_messages

mcp = FastMCP("ctools")


def _resolve(agent_name: str) -> Tuple[Optional[Agent], Optional[str]]:
    """Look up an installed agent. Returns (agent, error_message)."""
    agent = AGENTS.get(agent_name)
    if agent is None:
        return None, f"Unknown agent: {agent_name}. Available: {', '.join(AGENTS)}"
    if not agent.exists():
        return None, f"Agent {agent_name} not found at {agent.base_path}"
    return agent, None


def _session_ref(ref: str) -> Tuple[Optional[Agent], Optional[str], Optional[str]]:
    """Resolve an '@agent/session_id' reference. Returns (agent, id, error)."""
    agent_name, _, session_id = ref.lstrip("@").partition("/")
    if not session_id:
        return None, None, f"Invalid session reference: {ref}"
    agent, error = _resolve(agent_name)
    if error:
        return None, None, error
    return agent, session_id, None


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

    lines = [f"Sessions for {agent} ({len(sessions)} total):"]
    for s in sessions[:50]:
        mtime = s.mtime.strftime("%Y-%m-%d %H:%M") if s.mtime else "N/A"
        msgs = f"{s.message_count} msgs" if s.message_count else ""
        lines.append(f"  {s.id}  {s.name[:50]}  {mtime}  {msgs}")

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
            if name not in AGENTS:
                return f"Unknown agent: {name}. Available: {', '.join(AGENTS)}"
            targets.append(AGENTS[name])

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
def export_session(agent: str, session_id: str, max_messages: int = 100) -> str:
    """Export messages from a conversation session.

    Args:
        agent: Agent name (claude, claude-code, opencode, codex, pi, goose)
        session_id: Session ID to export
        max_messages: Maximum number of messages to return
    """
    resolved, error = _resolve(agent)
    if error:
        return error

    try:
        messages = resolved.messages(session_id)
    except AgentError as exc:
        return str(exc)
    if not messages:
        return f"Session not found: {agent}/{session_id}"

    lines = [f"Session: {agent}/{session_id} ({len(messages)} messages)"]
    for msg in messages[:max_messages]:
        content = msg.content[:200] + "..." if len(msg.content) > 200 else msg.content
        lines.append(f"[{msg.role}] {content}")

    if len(messages) > max_messages:
        lines.append(f"... and {len(messages) - max_messages} more messages")

    return "\n".join(lines)


@mcp.tool()
def extract_concepts(agent: str, session_id: str) -> str:
    """Extract concepts (constraints, goals, preferences) from a session.

    Scans conversation messages for 'Use the following <type>: <text>' patterns
    and returns them as structured concept objects.

    Args:
        agent: Agent name (claude, claude-code, opencode, codex, pi, goose)
        session_id: Session ID to extract concepts from
    """
    resolved, error = _resolve(agent)
    if error:
        return error

    concepts, error = _read_concepts(resolved, session_id)
    if error:
        return error
    if not concepts:
        return f"No concepts found in {agent}/{session_id}"

    lines = [f"Found {len(concepts)} concept(s) in {agent}/{session_id}:"]
    lines.extend(f"  [{c['type']}] {c['short'][:100]}" for c in concepts)
    return "\n".join(lines)


@mcp.tool()
def copy_concepts(source: str, destination: str) -> str:
    """Copy concepts between sessions or concept files.

    Args:
        source: Source reference (e.g. '@opencode/ses_abc' or path to concept JSON file)
        destination: Destination reference (e.g. '@claude-code/ses_xyz' or path to concept JSON file)
    """
    if source.startswith("@"):
        agent, session_id, error = _session_ref(source)
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

    if destination.startswith("@"):
        agent, session_id, error = _session_ref(destination)
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
def get_session_concepts(agent: str, session_id: str, concept_type: str = "") -> str:
    """Get concepts from a session, optionally filtered by type.

    Args:
        agent: Agent name (claude, claude-code, opencode, codex, pi, goose)
        session_id: Session ID to search
        concept_type: Filter by type (constraint, goal, preference, observation, reference). Empty for all.
    """
    resolved, error = _resolve(agent)
    if error:
        return error

    concepts, error = _read_concepts(resolved, session_id)
    if error:
        return error

    if concept_type:
        concepts = [c for c in concepts if c["type"] == concept_type]

    if not concepts:
        suffix = f" (type={concept_type})" if concept_type else ""
        return f"No concepts found in {agent}/{session_id}{suffix}"

    lines = [f"Found {len(concepts)} concept(s) in {agent}/{session_id}:"]
    lines.extend(json.dumps(c, indent=2) for c in concepts)
    return "\n".join(lines)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
