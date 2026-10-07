#!/usr/bin/env python3
"""
ccat - cat for LLM context windows

Print a conversation's messages, the way `cat` prints a file. Where cdir
lists the sessions and ccopy moves them, ccat shows you the actual
conversation, one or more at a time.

Usage:
    ccat opencode/ses_abc123                 # show one conversation
    ccat opencode/ses_a opencode/ses_b       # show several, in order
    ccat --json opencode/ses_abc            # machine-readable JSON
    ccat ssh://chris@remote/opencode/ses_a   # show a conversation on another host
    cgrep -l ctool opencode | xargs ccat     # dump every matching conversation

The source is never touched. A remote source is fetched the same way ccopy
does (the agent's storage is pulled over ssh and read locally), so the
remote host only needs sshd and tar.
"""

import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import typer
from rich.console import Console
from rich.text import Text

from ctools.agents import REGISTRY as AGENTS, SessionNotFound, get_agent
from ctools.cli import parse_ref, parse_remote_ref, reporting, version_option
from ctools.ccopy import _local_agent, _pull

__all__ = ['app']

app = typer.Typer()
console = Console()

# Role -> (label, rich style). A short left margin so the transcript reads
# top-to-bottom; the label is dimmed and the text is the real content.
_ROLE = {
    'user':      ('you',     'bold cyan'),
    'assistant': ('assistant', 'bold green'),
    'system':    ('system',  'bold yellow'),
    'tool':      ('tool',    'magenta'),
}
_DEFAULT_ROLE = ('msg', 'bold')


def _render_transcript(messages: List[dict], color: bool) -> None:
    """Print one conversation as a readable transcript on stdout.

    Each message becomes `    <role>  <content>`; the role is a short dimmed
    label in a fixed column so the content lines up.
    """
    width = 9  # len("assistant")
    for m in messages:
        role = m.get('role', '')
        label, style = _ROLE.get(role, _DEFAULT_ROLE)
        if color:
            line = Text()
            line.append('    ')
            line.append(f'{label:<{width}}', style=style)
            line.append('  ')
            line.append(m.get('content', ''))
            print(line)
        else:
            print(f'    {label:<{width}}  {m.get("content", "")}')


def _render_json(messages: List[dict]) -> None:
    import json
    print(json.dumps(messages, indent=2))


def _fetch(ref: str) -> Tuple[str, List[dict], Optional[str]]:
    """Resolve a (possibly remote) session ref to (label, records, error).

    Returns the display label, the conversation records, and an error string.
    Exactly one of records / error is meaningful: on success records is a list
    (possibly empty) and error is None; on failure error is set.
    """
    remote, local_ref = parse_remote_ref(ref)
    name, session_id = parse_ref(local_ref)
    if not session_id:
        return ref, [], f'no session ID in {ref} (expected agent/session_id)'
    agent = get_agent(name)
    if agent is None:
        return ref, [], f'unknown agent: {name} (available: {", ".join(AGENTS)})'

    if remote is None:
        if not agent.exists():
            return ref, [], f'{agent.name} is not installed'
        with reporting():
            try:
                records = [{"role": m.role, "content": m.content}
                           for m in agent.messages(session_id)]
            except SessionNotFound:
                return ref, [], f'no such session: {name}/{session_id}'
        return f'{name}/{session_id}', records, None

    # Remote: pull the agent's storage over ssh and read it locally.
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        try:
            _pull(remote, agent, base)
        except typer.Exit:
            return ref, [], f'could not pull {name} storage from {remote}'
        probe = _local_agent(agent, base)
        try:
            records = [{"role": m.role, "content": m.content}
                       for m in probe.messages(session_id)]
        except SessionNotFound:
            return ref, [], f'no such session on {remote.target}: {name}/{session_id}'
    label = f'{name}/{session_id} on {remote.target}'
    if not records:
        return label, [], f'no conversation in {label}'
    return label, records, None


def _use_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty()


@app.command()
def main(
    refs: Optional[List[str]] = typer.Argument(None,
        help="One or more sessions to print (agent/session_id, or "
             "ssh://[user@]host[:port]/agent/session_id)"),
    json_out: bool = typer.Option(False, "--json", "-j",
        help="Print the conversation as a JSON list of {role, content} objects"),
    color: str = typer.Option("auto", "--color",
        help="Colorize the transcript: never, auto, or always"),
    version: bool = version_option("ccat"),
):
    """
    Print one or more conversations, like cat.

    `ccat AGENT/SESSION` shows the session's messages as a readable transcript.
    Pass several sessions and they are printed in order, separated by a blank
    line. A remote source (ssh://...) is fetched over ssh first, so it works
    exactly like the local case. The source is never modified.

    Examples:
        ccat opencode/ses_abc123
        ccat opencode/ses_a opencode/ses_b
        ccat --json opencode/ses_abc
        ccat ssh://chris@remote/opencode/ses_a
    """
    if color not in ('never', 'auto', 'always'):
        raise typer.BadParameter("must be one of: never, auto, always",
                                 param_hint="--color")
    use_color = _use_color(color)

    if not refs:
        # No argument: cat from stdin? No — ccat is explicitly ref-based. Be
        # explicit rather than surprising the user.
        console.print("[red]Usage: ccat AGENT/SESSION [AGENT/SESSION ...][/red]")
        raise typer.Exit(1)

    printed_any = False
    for i, ref in enumerate(refs):
        label, records, error = _fetch(ref)
        if error:
            console.print(f"[yellow]{error}[/yellow]")
            continue
        if i and not json_out:
            print()  # blank line between consecutive transcripts
        if json_out:
            _render_json(records)
        else:
            _render_transcript(records, color=use_color)
        printed_any = True

    # cat exits non-zero only when nothing could be read; a single bad ref
    # among good ones still prints and exits 0.
    if not printed_any:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
