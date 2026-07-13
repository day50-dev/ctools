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
import sqlite3
import typer
from pathlib import Path
from typing import List, Optional, Dict, Tuple
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

from ctools.lib import Session, Agent, AGENTS, Message, format_size
from ctools.cdir import SESSION_EXTRACTORS, EXPORTERS

app = typer.Typer()
console = Console()


def get_session_tokens(agent: str, session_id: str) -> Dict[str, int]:
    """Get token breakdown for a session.
    
    Returns dict with keys: total, user, assistant, system, estimated.
    'estimated' is True when tokens were estimated from content length.
    """
    agent_info = AGENTS.get(agent)
    if not agent_info or not agent_info.base_path.exists():
        return {}

    # For opencode, we have actual token counts
    if agent == "opencode":
        return _get_opencode_tokens(agent_info, session_id)

    # For others, estimate from content
    exporter = EXPORTERS.get(agent)
    if not exporter:
        return {}

    messages = exporter(agent_info, session_id)
    if not messages:
        return {}

    tokens_by_role = {}
    for msg in messages:
        tokens_by_role[msg.role] = tokens_by_role.get(msg.role, 0) + count_tokens(msg.content)

    total = sum(tokens_by_role.values())
    return {
        "total": total,
        "user": tokens_by_role.get("user", 0),
        "assistant": tokens_by_role.get("assistant", 0),
        "system": tokens_by_role.get("system", 0),
        "estimated": True,
    }


def _get_opencode_tokens(agent_info: Agent, session_id: str) -> Dict[str, int]:
    """Get actual token counts from opencode SQLite database."""
    db_path = agent_info.base_path / "opencode.db"
    if not db_path.exists():
        return {}

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        cursor.execute(
            "SELECT tokens_input, tokens_output FROM session WHERE id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return {}

        tokens_input, tokens_output = row
        return {
            "total": (tokens_input or 0) + (tokens_output or 0),
            "input": tokens_input or 0,
            "output": tokens_output or 0,
            "estimated": False,
        }
    except sqlite3.Error:
        return {}


def format_tokens(tokens: int) -> str:
    """Format token count in human-readable form."""
    if tokens < 1000:
        return f"{tokens}"
    elif tokens < 1_000_000:
        return f"{tokens / 1000:.1f}k"
    else:
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
    else:
        parts = path.strip("/").split("/", 1)
        agent_name = parts[0]
        session_id = parts[1] if len(parts) > 1 else None

        if agent_name not in AGENTS:
            console.print(f"[red]Unknown agent: {agent_name}[/red]")
            console.print(f"[dim]Available: {', '.join(AGENTS.keys())}[/dim]")
            raise typer.Exit(1)

        agent_info = AGENTS[agent_name]
        if not agent_info.base_path.exists():
            console.print(f"[yellow]Agent {agent_name} not found at {agent_info.base_path}[/yellow]")
            raise typer.Exit(1)

        if session_id:
            _show_session_tokens(agent_name, session_id, json_output)
        else:
            _show_agent_sessions(agent_name, json_output)


def _show_all_agents(json_output: bool):
    """Show total token usage across all agents."""
    results = []
    for name, agent_info in AGENTS.items():
        if not agent_info.base_path.exists():
            continue

        extractor = SESSION_EXTRACTORS.get(name)
        if not extractor:
            continue

        sessions = extractor(agent_info)
        total_tokens = sum(s.size for s in sessions)
        results.append({
            "agent": name,
            "sessions": len(sessions),
            "tokens": total_tokens,
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


def _show_agent_sessions(agent_name: str, json_output: bool):
    """Show sessions for an agent sorted by token usage."""
    agent_info = AGENTS[agent_name]
    extractor = SESSION_EXTRACTORS.get(agent_name)
    if not extractor:
        console.print(f"[red]No session extractor for {agent_name}[/red]")
        raise typer.Exit(1)

    sessions = extractor(agent_info)
    if not sessions:
        console.print(f"[yellow]No sessions found for {agent_name}[/yellow]")
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

    table = Table(title=f"Context Usage — {agent_name}")
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


def _show_session_tokens(agent_name: str, session_id: str, json_output: bool):
    """Show token breakdown for a specific session."""
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
