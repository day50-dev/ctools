#!/usr/bin/env python3
"""
ccopy - copy a conversation from one agent to a new session in another

Copies an entire conversation from a source session into a brand-new session in
a destination agent. The source is never touched, so it is a true copy, not a
move. No @ prefix: the source is `agent/session_id`, the destination is a bare
agent name. Either side may be on another host, addressed as
`ssh://[user@]host[:port]/agent[/session_id]`. A remote side is probed first:
if it runs ctools, only the conversation JSON crosses the wire; otherwise the
agent's storage files are moved with plain tar, so the remote needs nothing
but sshd and tar.

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
from ctools.backup import guard_write, BackupError
from ctools.log import configure_logging, get_logger

console = Console()
# The wire modes (--export-json / --import-json) must keep stdout clean so
# they can be piped locally or run on the far side of ssh; their diagnostics
# go to stderr.
err_console = Console(stderr=True)
log = get_logger()

__all__ = ['app']


# --- Wire format ---
#
# A conversation is a JSON list of {"role", "content"} objects. The raw
# modes below are the wire format: they are what ccopy runs over ssh on the
# far side (when the remote has ctools), and they work in a local pipe just as
# well. Reading a remote session uses `ccat --raw` (the lossless envelope);
# seeding a remote session uses `ccopy --import-json`.

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
        err_console.print('[red]Expected a JSON list of {"role", "content"} objects[/red]')
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
    """--export-json agent/session: print the conversation as JSON on stdout.

    Diagnostic output goes to stderr so the JSON on stdout is always clean
    -- this mode is what `ccopy` pipes locally (ccopy --export-json A | ccopy
    --import-json B) and what it runs on the far side of ssh.
    """
    name, session_id = parse_ref(ref)
    if not session_id:
        err_console.print(f"[red]--export-json needs agent/session_id, got {ref}[/red]")
        raise SystemExit(1)
    agent = get_agent(name)
    if agent is None:
        err_console.print(f"[red]Unknown agent: {name}[/red]")
        raise SystemExit(1)
    if not agent.exists():
        err_console.print(f"[yellow]Agent path not found: {agent.base_path}[/yellow]")
        raise SystemExit(1)
    if not session_id:
        err_console.print(f"[yellow]No conversation in {name}/{session_id}[/yellow]")
        raise SystemExit(1)
    try:
        messages = agent.messages(session_id)
    except SessionNotFound:
        messages = []
    if not messages:
        err_console.print(f"[yellow]No conversation in {name}/{session_id}[/yellow]")
        raise SystemExit(1)
    json.dump(export_conversation(agent, session_id), sys.stdout, indent=2)
    sys.stdout.write("\n")


def _do_import(agent_name: str) -> None:
    """--import-json agent: read a conversation JSON from stdin into a new
    session in agent, and print the new session id on stdout.

    The only thing written to stdout is the new session id (diagnostics go to
    stderr), so `... | ccopy --import-json AGENT` yields just the id.
    """
    dest = get_agent(agent_name)
    if dest is None:
        err_console.print(f"[red]Unknown agent: {agent_name}[/red]")
        err_console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
        raise SystemExit(1)
    if not dest.supports_create():
        err_console.print(f"[red]{dest.label} does not support session creation[/red]")
        raise SystemExit(1)
    if not dest.exists():
        err_console.print(f"[yellow]Agent path not found: {dest.base_path}[/yellow]")
        raise SystemExit(1)
    if sys.stdin.isatty():
        err_console.print("[red]No input on stdin; pipe a conversation JSON in "
                          "(ccopy --export-json AGENT/SESSION | ccopy --import-json AGENT)[/red]")
        raise SystemExit(1)
    try:
        records = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        err_console.print(f"[red]Invalid JSON on stdin: {e}[/red]")
        raise SystemExit(1)
    records = _validate_records(records)
    if not records:
        err_console.print("[yellow]No messages to import[/yellow]")
        raise SystemExit(1)
    # A local write into the agent's live storage: guard it the same way the
    # main copy path does (snapshot -> write -> verify/restore).
    guard = guard_write(dest, note="ccopy --import-json")
    try:
        new_id = import_conversation(dest, records)
    except BaseException:
        guard.rollback()
        raise
    try:
        guard.finish()
    except BackupError as e:
        err_console.print(f"[red]{e}[/red]")
        err_console.print(f"[dim]Your {agent_name} storage was restored from "
                          f"the backup taken just before this import.[/dim]")
        raise SystemExit(1)
    print(new_id)


# --- Remote transport ---
#
# Cross-host copies happen in two tiers. First pass: probe whether the
# remote host runs ctools. If it does, the cheap path: the far side runs
# the wire modes itself (``ccopy --export-json AGENT/SESSION`` to read,
# ``ccopy --import-json AGENT`` to write) and only the conversation JSON
# crosses the wire -- one ssh round trip, no storage. Second pass: the
# remote has no ctools, so the agent's *storage files* are moved instead:
# the storage directory (or single database) is tarred, streamed over,
# and unpacked into a local temp directory that mirrors the remote layout;
# the conversation is read or written locally with the normal agent code
# and changed files are pushed back. The tar path needs nothing but sshd
# and tar.

def _run(argv: List[str], input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=input_text, capture_output=True, text=True)


_ctools_probes: dict = {}


def _remote_has_ctools(remote: Remote) -> bool:
    """First pass: does the remote host have ctools (ccopy on PATH)?

    A cheap probe -- one small ssh round trip, no storage. If the remote
    has ctools, the conversation can be exported/seeded there over the
    wire modes and only the conversation JSON crosses the wire; otherwise
    the caller falls back to the expensive tar storage pull. The answer is
    cached per host:port for the life of this process.
    """
    key = f"{remote.user or ''}@{remote.host}:{remote.port or ''}"
    if key in _ctools_probes:
        return _ctools_probes[key]
    if shutil.which("ssh") is None:
        return False
    proc = _run(remote.ssh_command("command -v ccopy >/dev/null 2>&1 && echo ok"))
    found = proc.returncode == 0 and proc.stdout.strip() == "ok"
    _ctools_probes[key] = found
    return found


def _remote_common_doc(remote: Remote, agent: Agent, session_id: str) -> Optional[dict]:
    """Fetch the common envelope from a remote that runs ctools, over one ssh:

    the far side runs ``ccat --raw AGENT/SESSION`` and streams the lossless
    envelope (the same shape as a local ``to_common``: context plus the
    agent's verbatim records and session metadata); we parse it here. Returns
    None on any failure (ssh error, agent not installed there, no such
    session) so the caller can fall back to pulling the storage.
    """
    cmd = f"ccat --raw {shlex.quote(agent.name)}/{shlex.quote(session_id)}"
    proc = _run(remote.ssh_command(cmd))
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get('context'), list):
        return None
    return doc


def _remote_import_records(remote: Remote, agent: Agent, records: List[dict]) -> Optional[str]:
    """Seed a new session in `agent` on `remote` over one ssh: the records
    are piped into the far side's ``ccopy --import-json AGENT``, whose stdout
    is the new session id. Returns None on any failure so the caller can
    fall back to the tar pull/create/push.
    """
    cmd = f"ccopy --import-json {shlex.quote(agent.name)}"
    proc = _run(remote.ssh_command(cmd), input_text=json.dumps(records))
    if proc.returncode != 0:
        # Silent: the caller falls back to the tar path, which reports its
        # own errors.
        return None
    new_id = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    return new_id or None


def _remote_storage_rel(agent: Agent) -> str:
    """The storage path on the remote host, relative to $HOME.

    For the standard installs this is e.g. ``.local/share/opencode/opencode.db``
    or ``.pi/agent/sessions``.
    """
    return agent.storage_relative()


def _remote_storage_exists(remote: Remote, agent: Agent) -> bool:
    """One ssh round trip: does `agent`'s default storage exist on `remote`?

    Used to turn a raw ``tar: Cannot stat`` failure into the plain truth --
    the agent isn't installed there.
    """
    rel = _remote_storage_rel(agent)
    cmd = f'test -e "$HOME/{rel}" && printf ok'
    proc = _run(remote.ssh_command(cmd))
    return proc.returncode == 0 and proc.stdout.strip() == "ok"


def _pull(remote: Remote, agent: Agent, dest_base: Path) -> None:
    """Fetch `agent`'s storage from `remote` into `dest_base`.

    Piped over ssh: the far side runs only ``tar -cf -``; we untar into
    `dest_base`, which then mirrors the remote layout
    (``dest_base/<agent-dir>/<storage>``).
    """
    rel = _remote_storage_rel(agent)
    if not _remote_storage_exists(remote, agent):
        console.print(f"[red]{agent.name} is not installed on {remote.target} "
                      f"(no ~/{rel})[/red]")
        console.print(f"[dim]Check what is there with: "
                      f"cdir {remote}[/dim]")
        raise SystemExit(1)
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
        console.print(f"[dim]Check what is there with: cdir {remote}[/dim]")
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
        console.print(f"[dim]Check what is there with: cdir {remote}[/dim]")
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
    """Create a new session in `dest` on `remote` and return its id.

    First pass: if the remote runs ctools, pipe the records into its
    ``ccopy --import-json`` -- one ssh, only the conversation crosses. Second
    pass: pull its storage, create the session against the local mirror,
    push the storage back.
    """
    if _remote_has_ctools(remote):
        new_id = _remote_import_records(remote, dest, records)
        if new_id:
            return new_id
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

    try:
        src_remote, src_ref = parse_remote_ref(source)
        dst_remote, dst_ref = parse_remote_ref(destination)
    except ValueError as e:
        # e.g. ssh:// with no host, or ssh://user@/agent (empty host).
        console.print(f"[red]Invalid reference: {e}[/red]")
        raise SystemExit(1)

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
        # A source that looks like a file path (absolute, or a *.json name)
        # but is not a readable file is a missing-file mistake, not an
        # agent/session reference. Say so before parse_ref mangles it.
        looks_like_file = source.startswith(("/", "~", "./", "../")) or \
            source.lower().endswith(".json")
        if looks_like_file and src_remote is None and not _is_json_source(source):
            console.print(f"[red]No such file: {source}[/red]")
            raise SystemExit(1)
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
        # Fetch the conversation as a common envelope. For a remote source:
        # first pass, if it runs ctools, have it export the envelope over one
        # ssh; second pass, pull its storage files and read them locally.
        if src_remote is None:
            try:
                common_doc = export_common(src_agent, src_id)
                records = common_doc.get('context') or []
            except SessionNotFound:
                records = []
            if not records:
                console.print(f"[yellow]No conversation in {src_name}/{src_id}[/yellow]")
                raise SystemExit(1)
        else:
            common_doc = None
            if _remote_has_ctools(src_remote):
                common_doc = _remote_common_doc(src_remote, src_agent, src_id)
            if common_doc is None:
                with tempfile.TemporaryDirectory() as tmp:
                    _pull(src_remote, src_agent, Path(tmp))
                    probe = _local_agent(src_agent, Path(tmp))
                    try:
                        common_doc = export_common(probe, src_id)
                    except SessionNotFound:
                        common_doc = None
            records = (common_doc or {}).get('context') or []
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
    #
    # A local write lands in the agent's *live* storage, so we guard it: take
    # a snapshot first, verify integrity after, and auto-restore if the write
    # corrupted the database. (A remote write happens on the other host, so
    # there is no local storage to protect here.)
    if dst_remote is None:
        guard = guard_write(dst_agent, note="ccopy")
        try:
            new_id = import_common(dst_agent, common_doc)
        except BaseException:
            guard.rollback()
            raise
        try:
            guard.finish()
        except BackupError as e:
            console.print(f"[red]{e}[/red]")
            console.print(f"[dim]Your {dst_ref} storage was restored from the "
                          f"backup taken just before this copy.[/dim]")
            raise SystemExit(1)
        backup_path = guard.backup.path if guard.backup else None
    else:
        new_id = _remote_destination(dst_remote, dst_agent, records)
        backup_path = None

    where = f" on {dst_remote.target}" if dst_remote else ""
    console.print(f"[green]Copied {len(records)} message(s) from {src_desc} "
                  f"to a new {dst_ref} session{where}: {new_id}[/green]")
    if backup_path is not None:
        console.print(f"[dim]Backup of {dst_ref} storage saved before this "
                      f"write: {backup_path}[/dim]")
        console.print(f"[dim]Restore it with: crecover restore {dst_ref} "
                      f"{backup_path}[/dim]")
    resume = get_resume_command(dst_ref, new_id)
    if resume:
        suffix = f" on {dst_remote.target}" if dst_remote else ""
        console.print(f"[green]Resume it{suffix} with:[/green]")
        console.print(f"  {resume}")
    return 0


app = main


if __name__ == "__main__":
    raise SystemExit(main())
