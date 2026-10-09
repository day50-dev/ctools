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
import io
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import argparse
from rich.console import Console

from ctools.agents import (
    Agent, Message, REGISTRY,
    get_agent, get_resume_command, SessionNotFound, UnsupportedOperation,
)
from ctools.cli import (Remote, handle_version, parse_ref, parse_remote_ref,
                        version_option)
from ctools.log import configure_logging, get_logger

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


def export_common(agent: Agent, session_id: str) -> dict:
    """Export a session to the common interchange format (the ccat --raw envelope)."""
    return agent.to_common(session_id)


def import_common(agent: Agent, doc: dict) -> str:
    """Import a common-format envelope into a NEW session in `agent`; return id."""
    return agent.from_common(doc)


def common_copy(source: Agent, source_id: str, destination: Agent) -> Tuple[str, int]:
    """Copy via the common format: to_common(source) -> from_common(destination).

    This is the 2N path -- every agent is proven against the common format
    independently, so A->B is the composition. Returns (new id, message count).
    """
    doc = source.to_common(source_id)
    new_id = destination.from_common(doc)
    return new_id, len(doc.get('context') or [])


def _validate_records(records) -> List[dict]:
    if (not isinstance(records, list)
            or not all(isinstance(r, dict) and "role" in r and "content" in r
                       for r in records)):
        console.print('[red]Expected a JSON list of {"role", "content"} objects[/red]')
        raise SystemExit(1)
    return records


def _is_json_source(source: str) -> bool:
    """True if `source` is a conversation JSON to read from a file, a process
    substitution (/dev/fd/N), or stdin ('-'), rather than an agent/session
    reference or an ssh:// URI.

    This is what makes the Unix-y form work:
    ccopy <(ssh remote ccat AGENT/SES) AGENT
    """
    if source == '-':
        return True
    if source.startswith('/dev/fd/'):
        return True
    return os.path.isfile(source)


def _read_records_from(source: str) -> List[dict]:
    """Read a conversation JSON from a file path, an fd path, or stdin ('-')."""
    if source == '-':
        data = sys.stdin.read()
    else:
        try:
            with open(source, 'r', encoding='utf-8') as f:
                data = f.read()
        except OSError as e:
            console.print(f"[red]Could not read {source}: {e}[/red]")
            raise SystemExit(1)
    try:
        records = json.loads(data)
    except json.JSONDecodeError as e:
        console.print(f"[red]{source} is not valid conversation JSON: {e}[/red]")
        raise SystemExit(1)
    return _validate_records(records)


def _do_export(ref: str) -> None:
    """--export-json agent/session: print the conversation as JSON on stdout."""
    name, session_id = parse_ref(ref)
    if not session_id:
        console.print(f"[red]--export-json needs agent/session_id, got {ref}[/red]")
        raise SystemExit(1)
    agent = get_agent(name)
    if agent is None:
        console.print(f"[red]Unknown agent: {name}[/red]")
        raise SystemExit(1)
    if not agent.exists():
        console.print(f"[yellow]Agent path not found: {agent.base_path}[/yellow]")
        raise SystemExit(1)
    if not session_id or not agent.messages(session_id):
        console.print(f"[yellow]No conversation in {name}/{session_id}[/yellow]")
        raise SystemExit(1)
    json.dump(export_conversation(agent, session_id), sys.stdout, indent=2)
    sys.stdout.write("\n")


def _do_import(agent_name: str) -> None:
    """--import-json agent: read a conversation JSON from stdin into a new
    session in agent, and print the new session id on stdout."""
    dest = get_agent(agent_name)
    if dest is None:
        console.print(f"[red]Unknown agent: {agent_name}[/red]")
        console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
        raise SystemExit(1)
    if not dest.supports_create():
        console.print(f"[red]{dest.label} does not support session creation[/red]")
        raise SystemExit(1)
    if not dest.exists():
        console.print(f"[yellow]Agent path not found: {dest.base_path}[/yellow]")
        raise SystemExit(1)
    if sys.stdin.isatty():
        console.print("[red]No input on stdin; pipe a conversation JSON in "
                      "(ccopy --export-json AGENT/SESSION | ccopy --import-json AGENT)[/red]")
        raise SystemExit(1)
    try:
        records = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON on stdin: {e}[/red]")
        raise SystemExit(1)
    records = _validate_records(records)
    if not records:
        console.print("[yellow]No messages to import[/yellow]")
        raise SystemExit(1)
    print(import_conversation(dest, records))


# --- Remote transport ---
#
# Cross-host copies move the agent's *storage files*, not ctools. The far
# side runs only plain ``tar`` over ssh: the agent's storage directory (or
# single database) is tarred, streamed over, and unpacked into a local temp
# directory that mirrors the remote layout. The conversation is then read or
# written locally with the normal agent code, and changed files are pushed
# back the same way. The remote host needs nothing but sshd and tar.

def _run(argv: List[str], input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=input_text, capture_output=True, text=True)

def _ssh(remote: Remote, command: str, input_text: Optional[str] = None) -> str:
    """Run `command` on `remote` and return its stdout; fail with the
    remote's stderr if the command errors."""
    if shutil.which("ssh") is None:
        console.print("[red]ssh is not installed; cannot reach "
                      f"{remote}[/red]")
        raise SystemExit(1)
    proc = _run(remote.ssh_command(command), input_text)
    if proc.returncode != 0:
        console.print(f"[red]ssh to {remote} failed (exit {proc.returncode})[/red]")
        if proc.stderr.strip():
            print(proc.stderr.rstrip())
        console.print("[dim]The remote needs only sshd and a POSIX tar; "
                      "nothing ctools-related runs on it.[/dim]")
        raise SystemExit(1)
    return proc.stdout


def _remote_storage_rel(agent: Agent) -> str:
    """The storage path on the remote host, relative to $HOME.

    For the standard installs this is e.g. ``.local/share/opencode/opencode.db``
    or ``.pi/agent/sessions``.
    """
    return agent.storage_relative()


def _pull(remote: Remote, agent: Agent, dest_base: Path) -> None:
    """Fetch `agent`'s storage from `remote` into `dest_base`.

    Piped over ssh: the far side runs only ``tar -cf -``; we untar into
    `dest_base`, which then mirrors the remote layout
    (``dest_base/<agent-dir>/<storage>``).
    """
    rel = _remote_storage_rel(agent)
    local_tar = "tar -cf - -C ~ " + shlex.quote(rel)
    proc = subprocess.Popen(
        remote.ssh_command(local_tar), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    tar_stdout, tar_stderr = proc.communicate()
    if proc.returncode != 0:
        console.print(f"[red]could not pull {agent.name} storage from {remote} "
                      f"(exit {proc.returncode})[/red]")
        if tar_stderr.decode(errors='replace').strip():
            print(tar_stderr.decode(errors='replace').rstrip())
        raise SystemExit(1)
    with tarfile.open(fileobj=io.BytesIO(tar_stdout)) as tar:
        tar.extractall(dest_base)


def _push(remote: Remote, agent: Agent, base: Path) -> None:
    """Push `base`/<agent-dir>/<storage> back to `remote`, overwriting the
    remote's storage in place. Piped: local ``tar -cf -`` -> ssh ``tar -xf -``.
    """
    rel = _remote_storage_rel(agent)
    src = base / rel
    if not src.exists():
        console.print(f"[red]nothing to push: {src}[/red]")
        raise SystemExit(1)
    remote_tar = f"tar -xf - -C ~ {shlex.quote(rel)}"
    local_tar = subprocess.Popen(
        ["tar", "-cf", "-", "-C", str(base), rel],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    remote_proc = subprocess.Popen(
        remote.ssh_command(remote_tar), stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    # Stream the local tarball into the remote tar's stdin.
    try:
        def _copy():
            while True:
                chunk = local_tar.stdout.read(1 << 20)
                if not chunk:
                    break
                remote_proc.stdin.write(chunk)
        _copy()
    except (BrokenPipeError, OSError):
        pass
    finally:
        remote_proc.stdin.close()
    local_tar.stdout.close()
    local_tar.wait()
    remote_proc.wait()
    if local_tar.returncode != 0 or remote_proc.returncode != 0:
        code = local_tar.returncode or remote_proc.returncode
        console.print(f"[red]could not push {agent.name} storage to {remote} "
                      f"(exit {code})[/red]")
        raise SystemExit(1)


def _local_agent(agent: Agent, base: Path) -> Agent:
    """A fresh instance of `agent`'s class pointed at the local mirror.

    `base` is a stand-in for the remote $HOME; the agent's own base_path is
    the home-relative storage root beneath it.
    """
    root = agent.home_relative_storage()
    return type(agent)(Path(base) / root)


def _remote_destination(remote: Remote, dest: Agent,
                        records: List[dict]) -> str:
    """Create a new session in `dest` on `remote`: pull its storage, create
    the session against the local mirror, push the storage back. Return the
    new session id."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _pull(remote, dest, base)
        mirror = _local_agent(dest, base)
        if not mirror.source.exists():
            console.print(f"[yellow]{dest.name} is not set up on {remote.target} "
                          f"(no storage at {dest.default_base_path()})[/yellow]")
            raise SystemExit(1)
        try:
            new_id = import_conversation(mirror, records)
        except UnsupportedOperation:
            raise SystemExit(1)
        _push(remote, dest, base)
    return new_id


# --- Main copy flow ---

def copy_conversation(source: Agent, source_id: str,
                      destination: Agent):
    """Copy `source`/`source_id`'s conversation into a NEW session in
    `destination`, leaving the source untouched; return (new id, message count)."""
    messages = source.messages(source_id)
    if not messages:
        console.print(f"[yellow]No conversation in {source.name}/{source_id}[/yellow]")
        raise SystemExit(1)
    new_id = destination.create_session(messages)
    return new_id, len(messages)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccopy",
        description=("Copy a conversation from one agent to a new session in another.\n\n"
                     "The source session is never modified. The destination agent "
                     "receives a fresh session holding the copied conversation, "
                     "and ccopy prints how to resume it. The source can be "
                     "addressed three ways: agent/session_id (a session in a "
                     "local agent), ssh://[user@]host[:port]/agent/session_id "
                     "(a session on a remote host), or FILE, /dev/fd/N, or - "
                     "(a conversation JSON read from a file or stdin)."),
    )
    parser.add_argument("source", nargs="?",
                        help="Source session (agent/session_id), a conversation "
                             "JSON file/fd/'-', or ssh://[user@]host[:port]/agent/session_id")
    parser.add_argument("destination", nargs="?",
                        help="Destination agent (bare name) or "
                             "ssh://[user@]host[:port]/agent")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Show what would happen without writing")
    parser.add_argument("--export-json", metavar="AGENT/SESSION",
                        dest="export_json",
                        help="Print a session's conversation as JSON and exit "
                             "(raw wire format; also what runs over ssh)")
    parser.add_argument("--import-json", metavar="AGENT", dest="import_json",
                        help="Read a conversation JSON from stdin into a NEW "
                             "session in AGENT and print the new id (raw wire "
                             "format; also what runs over ssh)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output")
    version_option(parser, "ccopy")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if (v := handle_version(args)) is not None:
        return v

    source = args.source
    destination = args.destination
    dry_run = args.dry_run
    export_json = args.export_json
    import_json = args.import_json
    verbose = args.verbose

    configure_logging(verbose=verbose)

    if import_json is not None:
        _do_import(import_json)
        return 0
    if export_json is not None:
        _do_export(export_json)
        return 0

    if source is None or destination is None:
        console.print("[red]Usage: ccopy SOURCE DESTINATION "
                      "(or --export-json AGENT/SESSION, or --import-json AGENT)[/red]")
        return 1

    src_remote, src_ref = parse_remote_ref(source)
    dst_remote, dst_ref = parse_remote_ref(destination)

    # The destination is always an agent (bare name, or ssh://agent).
    if not dst_ref:
        console.print(f"[red]No destination agent in {destination}[/red]")
        raise SystemExit(1)
    dst_agent = get_agent(dst_ref)
    if dst_agent is None:
        console.print(f"[red]Unknown agent: {dst_ref}[/red]")
        console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
        raise SystemExit(1)
    if not dst_agent.supports_create():
        console.print(f"[red]{dst_agent.label} does not support session creation[/red]")
        console.print("[dim]The conversation could not be seeded into this agent's storage format.[/dim]")
        raise SystemExit(1)
    if dst_remote is None and not dst_agent.exists():
        console.print(f"[yellow]Agent path not found: {dst_agent.base_path}[/yellow]")
        console.print(f"[dim]Is {dst_agent.name} installed?[/dim]")
        raise SystemExit(1)

    # The source is either a conversation JSON (a file, an fd from process
    # substitution, or stdin), or an agent/session reference, local or remote
    # via ssh://. We build a common-format `doc` so the copy is to_common ->
    # from_common (the 2N path).
    if src_remote is None and _is_json_source(source):
        records = _read_records_from(source)
        if not records:
            console.print(f"[yellow]No conversation in {source}[/yellow]")
            raise SystemExit(1)
        common_doc = {'context': records, 'source': source}
        src_desc = source
    else:
        src_name, src_id = parse_ref(src_ref)
        if not src_id:
            console.print(f"[red]No session ID in {source} (expected agent/session_id)[/red]")
            raise SystemExit(1)
        src_agent = get_agent(src_name)
        if src_agent is None:
            console.print(f"[red]Unknown agent: {src_name}[/red]")
            console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
            raise SystemExit(1)
        if src_remote is None and not src_agent.exists():
            console.print(f"[yellow]Agent path not found: {src_agent.base_path}[/yellow]")
            console.print(f"[dim]Is {src_agent.name} installed?[/dim]")
            raise SystemExit(1)
        # Fetch the conversation as a common envelope (read-only; for a remote
        # source, pull the storage files locally and read them with agent code).
        if src_remote is None:
            common_doc = export_common(src_agent, src_id)
            records = common_doc.get('context') or []
            if not records:
                console.print(f"[yellow]No conversation in {src_name}/{src_id}[/yellow]")
                raise SystemExit(1)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                _pull(src_remote, src_agent, Path(tmp))
                probe = _local_agent(src_agent, Path(tmp))
                try:
                    common_doc = export_common(probe, src_id)
                    records = common_doc.get('context') or []
                except SessionNotFound:
                    records = []
            if not records:
                console.print(f"[yellow]No conversation in {src_name}/{src_id} on "
                              f"{src_remote.target}[/yellow]")
                raise SystemExit(1)
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
        return 0

    # Import into a brand-new session: locally via the common format, or by
    # pushing the updated storage back when the destination is remote.
    if dst_remote is None:
        new_id = import_common(dst_agent, common_doc)
    else:
        new_id = _remote_destination(dst_remote, dst_agent, records)

    where = f" on {dst_remote.target}" if dst_remote else ""
    console.print(f"[green]Copied {len(records)} message(s) from {src_desc} "
                  f"to a new {dst_ref} session{where}: {new_id}[/green]")
    resume = get_resume_command(dst_ref, new_id)
    if resume:
        suffix = f" on {dst_remote.target}" if dst_remote else ""
        console.print(f"[green]Resume it{suffix} with:[/green]")
        console.print(f"  {resume}")
    return 0


app = main


if __name__ == "__main__":
    raise SystemExit(main())
