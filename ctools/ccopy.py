#!/usr/bin/env python3
"""
ccopy - copy a conversation from one agent to a new session in another

Copies an entire conversation from a source session into a brand-new session in
a destination agent. The source is never touched, so it is a true copy, not a
move. No @ prefix: the source is `agent/session_id`, the destination is a bare
agent name.

Usage:
    ccopy opencode/ses_abc claude-code     # opencode session -> new claude-code session
    ccopy claude-code/ses_123 codex        # claude-code -> new codex session
    ccopy opencode/ses_abc opencode        # duplicate within the same agent
"""

import typer
from rich.console import Console

from ctools.agents import (
    Agent, REGISTRY,
    get_agent, get_resume_command,
)
from ctools.cli import parse_ref, version_option
from ctools.log import configure_logging, get_logger

app = typer.Typer()
console = Console()
log = get_logger()

__all__ = ['app']


def copy_conversation(source: Agent, source_id: str,
                      destination: Agent):
    """Copy `source`/`source_id`'s conversation into a NEW session in
    `destination`, leaving the source untouched; return (new id, message count)."""
    messages = source.messages(source_id)
    if not messages:
        console.print(f"[yellow]No conversation in {source.name}/{source_id}[/yellow]")
        raise typer.Exit(1)
    new_id = destination.create_session(messages)
    return new_id, len(messages)


def _resolve_source(source_ref: str) -> Agent:
    """Resolve `agent/session_id` to an installed agent + session id."""
    name, source_id = parse_ref(source_ref)
    if not source_id:
        console.print(f"[red]No session ID in {source_ref} (expected agent/session_id)[/red]")
        raise typer.Exit(1)
    agent = get_agent(name)
    if agent is None:
        console.print(f"[red]Unknown agent: {name}[/red]")
        console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
        raise typer.Exit(1)
    if not agent.exists():
        console.print(f"[yellow]Agent path not found: {agent.base_path}[/yellow]")
        console.print(f"[dim]Is {agent.name} installed?[/dim]")
        raise typer.Exit(1)
    return agent


def _resolve_destination(agent_name: str) -> Agent:
    """Resolve a bare agent name to an installed agent that supports creation."""
    dest = get_agent(agent_name)
    if dest is None:
        console.print(f"[red]Unknown agent: {agent_name}[/red]")
        console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
        raise typer.Exit(1)
    if not dest.supports_create():
        console.print(f"[red]{dest.label} does not support session creation[/red]")
        console.print("[dim]The conversation could not be seeded into this agent's storage format.[/dim]")
        raise typer.Exit(1)
    if not dest.exists():
        console.print(f"[yellow]Agent path not found: {dest.base_path}[/yellow]")
        console.print(f"[dim]Is {dest.name} installed?[/dim]")
        raise typer.Exit(1)
    return dest


@app.command()
def main(
    source: str = typer.Argument(..., help="Source session (agent/session_id)"),
    destination: str = typer.Argument(..., help="Destination agent (bare name)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen without writing"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    version: bool = version_option("ccopy"),
):
    """
    Copy a conversation from one agent to a new session in another.

    The source session is never modified. The destination agent receives a fresh
    session holding the copied conversation, and ccopy prints how to resume it.

    Examples:
        ccopy opencode/ses_abc claude-code
        ccopy claude-code/ses_123 codex
        ccopy opencode/ses_abc opencode --dry-run
    """
    configure_logging(verbose=verbose)

    source_agent = _resolve_source(source)
    dest_agent = _resolve_destination(destination)
    _, source_id = parse_ref(source)

    count = len(source_agent.messages(source_id))
    log.info("conversation_copied_dry_run" if dry_run else "conversation_copied",
             source=f"{source_agent.name}/{source_id}",
             destination=dest_agent.name, messages=count)

    if dry_run:
        console.print(f"[cyan]Would copy {count} message(s) from {source_agent.name}/{source_id} "
                      f"to a new {dest_agent.name} session.[/cyan]")
        resume = get_resume_command(dest_agent.name, "<new-id>")
        if resume:
            console.print(f"[dim]Resume command shape: {resume}[/dim]")
        return

    new_id, copied = copy_conversation(source_agent, source_id, dest_agent)
    console.print(f"[green]Copied {copied} message(s) from {source_agent.name}/{source_id} "
                  f"to a new {dest_agent.name} session: {new_id}[/green]")
    resume = get_resume_command(dest_agent.name, new_id)
    if resume:
        console.print("[green]Resume it with:[/green]")
        console.print(f"  {resume}")


if __name__ == "__main__":
    app()
