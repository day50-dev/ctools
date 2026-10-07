#!/usr/bin/env python3
"""
ccopy - copy a conversation from one agent to a new session in another

Copies an entire conversation from a source session into a brand-new session in
a destination agent. The source is never touched, so it is a true copy, not a
move. No @ prefix: the source is `agent/session_id`, the destination is a bare
agent name. Either side may be on another host, addressed as
`ssh://[user@]host[:port]/agent[/session_id]`.

Usage:
    ccopy opencode/ses_abc claude-code                # opencode -> new claude-code session
    ccopy claude-code/ses_123 codex                   # claude-code -> new codex session
    ccopy opencode/ses_abc ssh://chris@remote/codex   # local -> new session on a remote host
    ccopy ssh://chris@remote/opencode/ses_abc codex   # pull a remote conversation
    ccopy --export-json opencode/ses_abc | ccopy --import-json codex   # raw wire format
"""

import json
import shlex
import shutil
import subprocess
import sys
from typing import List, Optional

import typer
from rich.console import Console

from ctools.agents import (
    Agent, Message, REGISTRY,
    get_agent, get_resume_command,
)
from ctools.cli import Remote, parse_ref, parse_remote_ref, version_option
from ctools.log import configure_logging, get_logger

app = typer.Typer()
console = Console()
log = get_logger()

__all__ = ['app']


# --- Wire format ---
#
# A conversation is a JSON list of {"role", "content"} objects. The two raw
# modes below are the wire format: they are what ccopy runs over ssh on the
# far side, and they work in a local pipe just as well.

def export_conversation(agent: Agent, session_id: str) -> List[dict]:
    """A session's conversation as plain records."""
    return [{"role": m.role, "content": m.content} for m in agent.messages(session_id)]


def import_conversation(agent: Agent, records: List[dict]) -> str:
    """Create a NEW session in `agent` holding `records`; return its id."""
    messages = [Message(role=r["role"], content=r["content"]) for r in records]
    return agent.create_session(messages)


def _validate_records(records) -> List[dict]:
    if (not isinstance(records, list)
            or not all(isinstance(r, dict) and "role" in r and "content" in r
                       for r in records)):
        console.print('[red]Expected a JSON list of {"role", "content"} objects[/red]')
        raise typer.Exit(1)
    return records


def _do_export(ref: str) -> None:
    """--export-json agent/session: print the conversation as JSON on stdout."""
    name, session_id = parse_ref(ref)
    if not session_id:
        console.print(f"[red]--export-json needs agent/session_id, got {ref}[/red]")
        raise typer.Exit(1)
    agent = get_agent(name)
    if agent is None:
        console.print(f"[red]Unknown agent: {name}[/red]")
        raise typer.Exit(1)
    if not agent.exists():
        console.print(f"[yellow]Agent path not found: {agent.base_path}[/yellow]")
        raise typer.Exit(1)
    if not session_id or not agent.messages(session_id):
        console.print(f"[yellow]No conversation in {name}/{session_id}[/yellow]")
        raise typer.Exit(1)
    json.dump(export_conversation(agent, session_id), sys.stdout, indent=2)
    sys.stdout.write("\n")


def _do_import(agent_name: str) -> None:
    """--import-json agent: read a conversation JSON from stdin into a new
    session in agent, and print the new session id on stdout."""
    dest = get_agent(agent_name)
    if dest is None:
        console.print(f"[red]Unknown agent: {agent_name}[/red]")
        console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
        raise typer.Exit(1)
    if not dest.supports_create():
        console.print(f"[red]{dest.label} does not support session creation[/red]")
        raise typer.Exit(1)
    if not dest.exists():
        console.print(f"[yellow]Agent path not found: {dest.base_path}[/yellow]")
        raise typer.Exit(1)
    if sys.stdin.isatty():
        console.print("[red]No input on stdin; pipe a conversation JSON in "
                      "(ccopy --export-json AGENT/SESSION | ccopy --import-json AGENT)[/red]")
        raise typer.Exit(1)
    try:
        records = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON on stdin: {e}[/red]")
        raise typer.Exit(1)
    records = _validate_records(records)
    if not records:
        console.print("[yellow]No messages to import[/yellow]")
        raise typer.Exit(1)
    print(import_conversation(dest, records))


# --- Remote transport ---

def _run(argv: List[str], input_text: Optional[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=input_text, capture_output=True, text=True)


def _fail_remote(remote: Remote, proc: subprocess.CompletedProcess) -> None:
    console.print(f"[red]ssh to {remote} failed (exit {proc.returncode})[/red]")
    if proc.stderr.strip():
        print(proc.stderr.rstrip())
    console.print("[dim]The remote host needs ctxttools installed "
                  "(pip install ctxttools), or its bin directory on the "
                  "non-interactive ssh PATH.[/dim]")
    raise typer.Exit(1)


def run_remote(remote: Remote, command: str, input_text: Optional[str] = None) -> str:
    """Run `command` on `remote` over ssh and return its stdout.

    Uses the caller's normal ssh setup (keys, ~/.ssh/config, jump hosts). The
    remote host must have ctxttools installed, since `command` is a ctools
    invocation. Because non-interactive ssh often has a minimal PATH, a failed
    lookup of `ccopy` is retried as `python -m ctools.cli run ccopy ...` so an
    install that is on the remote python path but not on the ssh PATH still works.
    """
    if shutil.which("ssh") is None:
        console.print("[red]ssh is not installed; cannot reach "
                      f"{remote}[/red]")
        raise typer.Exit(1)
    proc = _run(remote.ssh_command(command), input_text)
    if proc.returncode != 0 and "not found" in proc.stderr:
        # `ccopy` not on the remote ssh PATH. Retry from the source tree so a
        # host that has ctxttools checked out (but no venv on the ssh PATH)
        # still serves it.
        module_cmd = _fallback_python_command(command)
        proc = _run(remote.ssh_command(module_cmd), input_text)
    if proc.returncode != 0:
        _fail_remote(remote, proc)
    return proc.stdout


def _fallback_python_command(command: str) -> str:
    """A remote-safe command that runs `command` from the ctxtools source tree.

    Runs `python -m ctools.cli run ccopy ...` from $CTOOLS_DIR (or the current
    directory), which puts the package on sys.path without needing a venv on
    the non-interactive ssh PATH.
    """
    argv = shlex.quote(" ".join(command.split()))
    return f"cd ${{CTOOLS_DIR:-.}} && python -m ctools.cli run {argv}"


# --- Main copy flow ---

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


@app.command()
def main(
    source: Optional[str] = typer.Argument(None,
                                           help="Source session (agent/session_id) or ssh://[user@]host[:port]/agent/session_id"),
    destination: Optional[str] = typer.Argument(None,
                                                help="Destination agent (bare name) or ssh://[user@]host[:port]/agent"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen without writing"),
    export_json: Optional[str] = typer.Option(None, "--export-json", metavar="AGENT/SESSION",
                                              help="Print a session's conversation as JSON and exit (raw wire format; also what runs over ssh)"),
    import_json: Optional[str] = typer.Option(None, "--import-json", metavar="AGENT",
                                              help="Read a conversation JSON from stdin into a NEW session in AGENT and print the new id (raw wire format; also what runs over ssh)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    version: bool = version_option("ccopy"),
):
    """
    Copy a conversation from one agent to a new session in another.

    The source session is never modified. The destination agent receives a fresh
    session holding the copied conversation, and ccopy prints how to resume it.
    Either side may be remote: address it as ssh://[user@]host[:port]/... and it
    runs over your normal ssh setup (the remote host needs ctxttools installed).

    Examples:
        ccopy opencode/ses_abc claude-code
        ccopy claude-code/ses_123 codex
        ccopy opencode/ses_abc ssh://chris@remote/codex
        ccopy ssh://chris@remote/opencode/ses_abc codex
        ccopy opencode/ses_abc opencode --dry-run
    """
    configure_logging(verbose=verbose)

    if import_json is not None:
        _do_import(import_json)
        return
    if export_json is not None:
        _do_export(export_json)
        return

    if source is None or destination is None:
        console.print("[red]Usage: ccopy SOURCE DESTINATION "
                      "(or --export-json AGENT/SESSION, or --import-json AGENT)[/red]")
        raise typer.Exit(1)

    src_remote, src_ref = parse_remote_ref(source)
    dst_remote, dst_ref = parse_remote_ref(destination)

    # Validate both ends locally, so errors are friendly before any ssh happens.
    src_name, src_id = parse_ref(src_ref)
    if not src_id:
        console.print(f"[red]No session ID in {source} (expected agent/session_id)[/red]")
        raise typer.Exit(1)
    src_agent = get_agent(src_name)
    if src_agent is None:
        console.print(f"[red]Unknown agent: {src_name}[/red]")
        console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
        raise typer.Exit(1)
    if src_remote is None and not src_agent.exists():
        console.print(f"[yellow]Agent path not found: {src_agent.base_path}[/yellow]")
        console.print(f"[dim]Is {src_agent.name} installed?[/dim]")
        raise typer.Exit(1)

    if not dst_ref:
        console.print(f"[red]No destination agent in {destination}[/red]")
        raise typer.Exit(1)
    dst_agent = get_agent(dst_ref)
    if dst_agent is None:
        console.print(f"[red]Unknown agent: {dst_ref}[/red]")
        console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
        raise typer.Exit(1)
    if not dst_agent.supports_create():
        console.print(f"[red]{dst_agent.label} does not support session creation[/red]")
        console.print("[dim]The conversation could not be seeded into this agent's storage format.[/dim]")
        raise typer.Exit(1)
    if dst_remote is None and not dst_agent.exists():
        console.print(f"[yellow]Agent path not found: {dst_agent.base_path}[/yellow]")
        console.print(f"[dim]Is {dst_agent.name} installed?[/dim]")
        raise typer.Exit(1)

    # Fetch the conversation (read-only; over ssh when the source is remote).
    if src_remote is None:
        records = export_conversation(src_agent, src_id)
        if not records:
            console.print(f"[yellow]No conversation in {src_name}/{src_id}[/yellow]")
            raise typer.Exit(1)
    else:
        out = run_remote(src_remote, f"ccopy --export-json {shlex.quote(src_ref)}")
        try:
            records = _validate_records(json.loads(out))
        except json.JSONDecodeError:
            console.print(f"[red]Malformed JSON from {src_remote}[/red]")
            raise typer.Exit(1)
        if not records:
            console.print(f"[yellow]No conversation in {src_name}/{src_id} on {src_remote.target}[/yellow]")
            raise typer.Exit(1)

    src_desc = f"{src_name}/{src_id}" + (f" on {src_remote.target}" if src_remote else "")

    log.info("conversation_copy_dry_run" if dry_run else "conversation_copy",
             source=src_desc, destination=dst_ref,
             remote=bool(src_remote or dst_remote), messages=len(records))

    if dry_run:
        console.print(f"[cyan]Would copy {len(records)} message(s) from {src_desc} "
                      f"to a new {dst_ref} session"
                      + (f" on {dst_remote.target}" if dst_remote else "") + ".[/cyan]")
        resume = get_resume_command(dst_ref, "<new-id>")
        if resume:
            console.print(f"[dim]Resume command shape: {resume}[/dim]")
        return

    # Import into a brand-new session (locally, or over ssh when the destination
    # is remote).
    if dst_remote is None:
        new_id = import_conversation(dst_agent, records)
    else:
        new_id = run_remote(dst_remote,
                            f"ccopy --import-json {shlex.quote(dst_ref)}",
                            input_text=json.dumps(records)).strip()
        if not new_id:
            console.print(f"[red]No session id returned from {dst_remote}[/red]")
            raise typer.Exit(1)

    where = f" on {dst_remote.target}" if dst_remote else ""
    console.print(f"[green]Copied {len(records)} message(s) from {src_desc} "
                  f"to a new {dst_ref} session{where}: {new_id}[/green]")
    resume = get_resume_command(dst_ref, new_id)
    if resume:
        console.print(f"[green]Resume it{' on ' + dst_remote.target if dst_remote else ''} with:[/green]")
        console.print(f"  {resume}")


if __name__ == "__main__":
    app()
