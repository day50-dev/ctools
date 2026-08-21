#!/usr/bin/env python3
"""
cli - plumbing shared by the ctools commands.

Reference parsing ("opencode/ses_abc"), agent lookup and uniform error
reporting live here so that every command resolves and complains the
same way.
"""

from contextlib import contextmanager
from typing import Optional, Tuple

import typer
from rich.console import Console

from ctools.agents import Agent, AgentError, REGISTRY

console = Console()


def parse_ref(ref: str) -> Tuple[str, Optional[str]]:
    """Split an ``agent`` or ``agent/session_id`` reference.

    A leading ``@`` (used by ccopy and cconnect to mark session arguments)
    and surrounding slashes are ignored.
    """
    parts = ref.lstrip('@').strip('/').split('/', 1)
    return parts[0], (parts[1] if len(parts) > 1 else None)


def require_agent(name: str) -> Agent:
    """Look up an agent by name, or exit with a usage message."""
    agent = REGISTRY.get(name)
    if agent is None:
        console.print(f"[red]Unknown agent: {name}[/red]")
        console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
        raise typer.Exit(1)
    return agent


def require_installed(name: str) -> Agent:
    """Look up an agent and confirm its storage is present."""
    agent = require_agent(name)
    if not agent.exists():
        console.print(f"[yellow]Agent path not found: {agent.base_path}[/yellow]")
        console.print(f"[dim]Is {agent.name} installed?[/dim]")
        raise typer.Exit(1)
    return agent


def require_session(ref: str) -> Tuple[Agent, str]:
    """Resolve ``agent/session_id`` to an installed agent and a session id."""
    name, session_id = parse_ref(ref)
    agent = require_installed(name)
    if not session_id:
        console.print(f"[red]No session ID in {ref}[/red]")
        raise typer.Exit(1)
    return agent, session_id


@contextmanager
def reporting():
    """Turn an :class:`AgentError` into a clean message and exit code 1."""
    try:
        yield
    except AgentError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1)
