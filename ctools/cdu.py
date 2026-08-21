#!/usr/bin/env python3
"""
cdu - context disk usage for LLM conversations

Shows token length of conversations, similar to DOS mdu
but for LLM context windows. Uses tiktoken for accurate counts.

Usage:
    cdu                     # Show all agents with total token usage
    cdu opencode/           # Show sessions with token usage
    cdu opencode/ses_abc    # Show token breakdown for a session
"""

import json
from typing import Dict, Optional

import typer
from rich.console import Console
from rich.table import Table

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_enc.encode(text))
except ImportError:
    def count_tokens(text: str) -> int:
        return len(text) // 4

from ctools.agents import Agent, AgentError, REGISTRY as AGENTS
from ctools.cli import parse_ref, require_installed

app = typer.Typer()
console = Console()

__all__ = ['app', 'count_tokens', 'format_tokens', 'get_session_tokens']


def get_session_tokens(agent_name: str, session_id: str) -> Dict[str, int]:
    """Token breakdown for a session.

    Prefers the counts an agent recorded itself; otherwise estimates from
    message text and flags the result as estimated.
    """
    agent = AGENTS.get(agent_name)
    if agent is None or not agent.exists():
        return {}

    try:
        recorded = agent.token_usage(session_id)
        if recorded:
            return {**recorded, "estimated": False}

        messages = agent.messages(session_id)
    except AgentError:
        return {}

    if not messages:
        return {}

    by_role = {}
    for msg in messages:
        by_role[msg.role] = by_role.get(msg.role, 0) + count_tokens(msg.content)

    return {
        "total": sum(by_role.values()),
        "user": by_role.get("user", 0),
        "assistant": by_role.get("assistant", 0),
        "system": by_role.get("system", 0),
        "estimated": True,
    }


def format_tokens(tokens: int) -> str:
    """Format token count in human-readable form."""
    if tokens < 1000:
        return f"{tokens}"
    if tokens < 1_000_000:
        return f"{tokens / 1000:.1f}k"
    return f"{tokens / 1_000_000:.1f}M"


@app.command()
def main(
    path: Optional[str] = typer.Argument(None, help="Agent or agent/session_id"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
):
    """
    Show token length of conversations.

    Uses tiktoken for accurate counts, falls back to ~4 chars/token estimation.
    With no arguments, shows total usage across all agents.
    With an agent name, shows sessions sorted by token usage.
    With agent/session_id, shows token breakdown for that session.
    """
    if path is None:
        _show_all_agents(json_output)
        return

    agent_name, session_id = parse_ref(path)
    agent = require_installed(agent_name)

    if session_id:
        _show_session_tokens(agent, session_id, json_output)
    else:
        _show_agent_sessions(agent, json_output)


def _show_all_agents(json_output: bool):
    """Show total token usage across all agents."""
    results = []
    for agent in AGENTS.values():
        if not agent.exists():
            continue
        try:
            sessions = agent.sessions()
        except AgentError:
            continue
        results.append({
            "agent": agent.name,
            "sessions": len(sessions),
            "tokens": sum(s.size for s in sessions),
        })

    if json_output:
        print(json.dumps(results, indent=2))
        return

    if not results:
        console.print("[yellow]No agents found[/yellow]")
        return

    table = Table(title="Context Usage by Agent")
    table.add_column("Agent", style="cyan")
    table.add_column("Sessions", justify="right")
    table.add_column("Tokens", justify="right", style="green")

    grand_total = 0
    for r in sorted(results, key=lambda x: x["tokens"], reverse=True):
        grand_total += r["tokens"]
        table.add_row(r["agent"], str(r["sessions"]), format_tokens(r["tokens"]))

    table.add_section()
    table.add_row("TOTAL", "", format_tokens(grand_total), style="bold")

    console.print(table)


def _show_agent_sessions(agent: Agent, json_output: bool):
    """Show sessions for an agent sorted by token usage."""
    try:
        sessions = agent.sessions()
    except AgentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if not sessions:
        console.print(f"[yellow]No sessions found for {agent.name}[/yellow]")
        return

    sessions.sort(key=lambda s: s.size, reverse=True)

    if json_output:
        data = []
        for s in sessions:
            data.append({
                "id": s.id,
                "name": s.name,
                "tokens": s.size,
                "messages": s.message_count,
            })
        print(json.dumps(data, indent=2))
        return

    table = Table(title=f"Context Usage — {agent.name}")
    table.add_column("Session", style="cyan")
    table.add_column("Name")
    table.add_column("Tokens", justify="right", style="green")
    table.add_column("Messages", justify="right")

    total = 0
    for s in sessions[:50]:
        total += s.size
        name = s.name[:40] + "..." if len(s.name) > 40 else s.name
        msgs = str(s.message_count) if s.message_count else "-"
        table.add_row(s.id, name, format_tokens(s.size), msgs)

    if len(sessions) > 50:
        table.add_row("...", f"{len(sessions) - 50} more", "", "")

    table.add_section()
    table.add_row("TOTAL", "", format_tokens(total), f"{len(sessions)} sessions", style="bold")

    console.print(table)


def _show_session_tokens(agent: Agent, session_id: str, json_output: bool):
    """Show token breakdown for a specific session."""
    agent_name = agent.name
    tokens = get_session_tokens(agent_name, session_id)
    if not tokens:
        console.print(f"[yellow]Session not found: {agent_name}/{session_id}[/yellow]")
        raise typer.Exit(1)

    if json_output:
        data = {"session": f"{agent_name}/{session_id}", **tokens}
        print(json.dumps(data, indent=2))
        return

    est = " (estimated)" if tokens.get("estimated") else " (actual)"

    table = Table(title=f"Token Usage — {agent_name}/{session_id}")
    table.add_column("Metric", style="cyan")
    table.add_column("Tokens", justify="right", style="green")

    table.add_row("Total", format_tokens(tokens["total"]))

    if "input" in tokens:
        table.add_row("Input", format_tokens(tokens["input"]))
        table.add_row("Output", format_tokens(tokens["output"]))
    else:
        if tokens.get("user", 0):
            table.add_row("User", format_tokens(tokens["user"]))
        if tokens.get("assistant", 0):
            table.add_row("Assistant", format_tokens(tokens["assistant"]))
        if tokens.get("system", 0):
            table.add_row("System", format_tokens(tokens["system"]))

    table.add_section()
    table.add_row("Source", est)

    console.print(table)


if __name__ == "__main__":
    app()
