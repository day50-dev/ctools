#!/usr/bin/env python3
"""
ctools MCP server - search and manage LLM conversations from any MCP host.

Provides tools for listing agents, searching sessions, and copying concepts
between conversations. Runs over stdio for integration with Claude, opencode,
and other MCP-compatible clients.
"""

import re
import json
import typer
from mcp.server.fastmcp import FastMCP

from ctools.lib import AGENTS
from ctools.cdir import SESSION_EXTRACTORS, EXPORTERS
from ctools.cgrep import CONTENT_EXTRACTORS
from ctools.ccopy import (
    extract_concepts_from_messages,
    inject_concepts_to_session,
    get_session_messages,
)

mcp = FastMCP("ctools")


# --- Tools ---

@mcp.tool()
def list_agents() -> str:
    """List all supported LLM agents and whether they are installed."""
    lines = []
    for name, agent in AGENTS.items():
        exists = agent.base_path.exists()
        status = "installed" if exists else "not found"
        lines.append(f"{name}: {agent.description} [{status}] ({agent.storage_format})")
    return "\n".join(lines)


@mcp.tool()
def list_sessions(agent: str, sort: str = "time") -> str:
    """List conversation sessions for an agent.

    Args:
        agent: Agent name (claude, claude-code, opencode, codex)
        sort: Sort by 'time' or 'size'
    """
    if agent not in AGENTS:
        return f"Unknown agent: {agent}. Available: {', '.join(AGENTS.keys())}"

    agent_info = AGENTS[agent]
    if not agent_info.base_path.exists():
        return f"Agent {agent} not found at {agent_info.base_path}"

    extractor = SESSION_EXTRACTORS.get(agent)
    if not extractor:
        return f"No session extractor for {agent}"

    sessions = extractor(agent_info)
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

    # Determine which agents to search
    if agents == "*":
        search_agents = list(AGENTS.keys())
    else:
        search_agents = [a.strip() for a in agents.split(",")]
        for a in search_agents:
            if a not in AGENTS:
                return f"Unknown agent: {a}. Available: {', '.join(AGENTS.keys())}"

    all_matches = []
    for agent_name in search_agents:
        agent_info = AGENTS[agent_name]
        if not agent_info.base_path.exists():
            continue

        extractor = SESSION_EXTRACTORS.get(agent_name)
        if not extractor:
            continue

        sessions = extractor(agent_info)
        for session in sessions:
            content_extractor = CONTENT_EXTRACTORS.get(agent_name)
            if not content_extractor:
                continue

            lines = content_extractor(agent_info.base_path, session.id)
            for line_num, line in lines:
                if compiled.search(line):
                    all_matches.append(f"{agent_name}/{session.id}:{line_num}: {line}")
                    if len(all_matches) >= max_results:
                        break
            if len(all_matches) >= max_results:
                break
        if len(all_matches) >= max_results:
            break

    if not all_matches:
        return f"No matches found for '{pattern}'"

    header = f"Found {len(all_matches)} match(es) for '{pattern}':"
    if len(all_matches) >= max_results:
        header += f" (capped at {max_results})"

    return header + "\n" + "\n".join(all_matches)


@mcp.tool()
def export_session(agent: str, session_id: str, max_messages: int = 100) -> str:
    """Export messages from a conversation session.

    Args:
        agent: Agent name (claude, claude-code, opencode, codex)
        session_id: Session ID to export
        max_messages: Maximum number of messages to return
    """
    if agent not in AGENTS:
        return f"Unknown agent: {agent}. Available: {', '.join(AGENTS.keys())}"

    agent_info = AGENTS[agent]
    if not agent_info.base_path.exists():
        return f"Agent {agent} not found at {agent_info.base_path}"

    exporter = EXPORTERS.get(agent)
    if not exporter:
        return f"No exporter for {agent}"

    messages = exporter(agent_info, session_id)
    if not messages:
        return f"Session not found: {agent}/{session_id}"

    lines = [f"Session: {agent}/{session_id} ({len(messages)} messages)"]
    for i, msg in enumerate(messages[:max_messages]):
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
        agent: Agent name (claude, claude-code, opencode, codex)
        session_id: Session ID to extract concepts from
    """
    if agent not in AGENTS:
        return f"Unknown agent: {agent}. Available: {', '.join(AGENTS.keys())}"

    try:
        messages = get_session_messages(agent, session_id)
    except (typer.Exit, Exception):
        return f"Session not found: {agent}/{session_id}"

    concepts = extract_concepts_from_messages(messages)
    if not concepts:
        return f"No concepts found in {agent}/{session_id}"

    lines = [f"Found {len(concepts)} concept(s) in {agent}/{session_id}:"]
    for c in concepts:
        lines.append(f"  [{c['type']}] {c['short'][:100]}")

    return "\n".join(lines)


@mcp.tool()
def copy_concepts(source: str, destination: str) -> str:
    """Copy concepts between sessions or concept files.

    Args:
        source: Source reference (e.g. 'opencode/ses_abc' or path to concept JSON file)
        destination: Destination reference (e.g. 'claude-code/ses_xyz' or path to concept JSON file)
    """
    # Determine source type
    if source.startswith("@"):
        ref = source[1:]
        parts = ref.split("/", 1)
        if len(parts) < 2 or parts[0] not in AGENTS:
            return f"Invalid session reference: {source}"
        try:
            messages = get_session_messages(parts[0], parts[1])
        except (typer.Exit, Exception):
            return f"Session not found: {ref}"
        concepts = extract_concepts_from_messages(messages)
    elif source.endswith(".json"):
        try:
            with open(source) as f:
                concepts = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            return f"Error reading {source}: {e}"
    else:
        return f"Unknown source type: {source}"

    if not concepts:
        return "No concepts found in source"

    # Determine destination type
    if destination.startswith("@"):
        ref = destination[1:]
        parts = ref.split("/", 1)
        if len(parts) < 2 or parts[0] not in AGENTS:
            return f"Invalid session reference: {destination}"
        try:
            inject_concepts_to_session(parts[0], parts[1], concepts)
        except (typer.Exit, Exception):
            return f"Could not inject into {ref}"
        return f"Injected {len(concepts)} concept(s) into {destination}"
    elif destination.endswith(".json"):
        try:
            with open(destination, "w") as f:
                json.dump(concepts, f, indent=2)
                f.write("\n")
        except OSError as e:
            return f"Error writing {destination}: {e}"
        return f"Wrote {len(concepts)} concept(s) to {destination}"
    else:
        return f"Unknown destination type: {destination}"


@mcp.tool()
def get_session_concepts(agent: str, session_id: str, concept_type: str = "") -> str:
    """Get concepts from a session, optionally filtered by type.

    Args:
        agent: Agent name (claude, claude-code, opencode, codex)
        session_id: Session ID to search
        concept_type: Filter by type (constraint, goal, preference, observation, reference). Empty for all.
    """
    if agent not in AGENTS:
        return f"Unknown agent: {agent}. Available: {', '.join(AGENTS.keys())}"

    try:
        messages = get_session_messages(agent, session_id)
    except (typer.Exit, Exception):
        return f"Session not found: {agent}/{session_id}"

    concepts = extract_concepts_from_messages(messages)
    if concept_type:
        concepts = [c for c in concepts if c["type"] == concept_type]

    if not concepts:
        return f"No concepts found in {agent}/{session_id}" + (f" (type={concept_type})" if concept_type else "")

    lines = [f"Found {len(concepts)} concept(s) in {agent}/{session_id}:"]
    for c in concepts:
        lines.append(json.dumps(c, indent=2))

    return "\n".join(lines)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
