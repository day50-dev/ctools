#!/usr/bin/env python3
"""
ccat - cat for LLM context windows

Print a conversation's messages, the way `cat` prints a file. Where cdir
lists the sessions and ccopy moves them, ccat shows you the actual
conversation, one or more at a time.

By default the output is a JSON list of {role, content} objects (one array per
session) — the llcat/OpenAI spine — so it is importable and pipeable: feed it to
jq, to another tool, or back into a session. Use --text for a human-readable
transcript, or --raw for the lossless envelope (the context plus the source
agent's verbatim records and session metadata).

Usage:
    ccat opencode/ses_abc123                 # print one conversation as JSON
    ccat opencode/ses_a opencode/ses_b       # several, each a JSON array
    ccat --text opencode/ses_abc            # human-readable transcript
    ccat --raw opencode/ses_abc > session.json   # lossless envelope
    ccat ssh://chris@remote/opencode/ses_a  # a conversation on another host
    ccat opencode/ses_abc | jq .            # it is JSON, so pipe it anywhere
    cgrep -l ctool opencode | xargs ccat    # dump every matching conversation

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


def _render_raw(label: str, context: List[dict],
                raw: List[dict], info: Optional[dict]) -> None:
    """Print the lossless envelope: the portable context plus the agent's
    verbatim records and session metadata.

    ``context`` is the llcat/OpenAI-compatible {role, content} array; ``raw`` is
    the source agent's verbatim per-message records (parallel, may be empty)
    so a copy back into the *same* agent loses nothing; the session facts
    (source, model, timestamps) sit at the top, not bolted onto each record.
    """
    import json
    doc = {
        'context': context,
        'source': label,
    }
    if info:
        for key in ('model', 'created', 'modified'):
            if info.get(key):
                doc[key] = info[key]
    if raw:
        doc['raw'] = raw
    print(json.dumps(doc, indent=2))


def _fetch(ref: str) -> Tuple[str, List[dict], List[dict], Optional[dict], Optional[str]]:
    """Resolve a (possibly remote) session ref.

    Returns (label, context, raw, info, error). `context` is the portable
    {role, content} list; `raw` is the agent's verbatim records (may be empty);
    `info` is session metadata (may be None). Exactly one of context / error is
    meaningful: on success error is None; on failure error is set.
    """
    remote, local_ref = parse_remote_ref(ref)
    name, session_id = parse_ref(local_ref)
    if not session_id:
        return ref, [], [], None, f'no session ID in {ref} (expected agent/session_id)'
    agent = get_agent(name)
    if agent is None:
        return ref, [], [], None, f'unknown agent: {name} (available: {", ".join(AGENTS)})'

    if remote is None:
        if not agent.exists():
            return ref, [], [], None, f'{agent.name} is not installed'
        with reporting():
            try:
                context = [{"role": m.role, "content": m.content}
                           for m in agent.messages(session_id)]
                raw = agent.raw_records(session_id)
                info = agent.session_info(session_id)
            except SessionNotFound:
                return ref, [], [], None, f'no such session: {name}/{session_id}'
        return f'{name}/{session_id}', context, raw, info, None

    # Remote: pull the agent's storage over ssh and read it locally.
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        try:
            _pull(remote, agent, base)
        except typer.Exit:
            return ref, [], [], None, f'could not pull {name} storage from {remote}'
        probe = _local_agent(agent, base)
        try:
            context = [{"role": m.role, "content": m.content}
                       for m in probe.messages(session_id)]
            raw = probe.raw_records(session_id)
            info = probe.session_info(session_id)
        except SessionNotFound:
            return ref, [], [], None, f'no such session on {remote.target}: {name}/{session_id}'
    label = f'{name}/{session_id} on {remote.target}'
    if not context:
        return label, [], [], None, f'no conversation in {label}'
    return label, context, raw, info, None


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
    text_out: bool = typer.Option(False, "--text", "-t",
        help="Print a human-readable transcript instead of JSON"),
    raw_out: bool = typer.Option(False, "--raw", "-r",
        help="Print the lossless envelope: context plus the agent's verbatim "
             "records and session metadata (JSON)"),
    color: str = typer.Option("auto", "--color",
        help="Colorize the --text transcript: never, auto, or always"),
    version: bool = version_option("ccat"),
):
    """
    Print one or more conversations, like cat.

    `ccat AGENT/SESSION` prints the session's messages. The default output is a
    JSON list of {role, content} objects (one array per session) — the llcat/OpenAI
    spine — so it can be imported or piped to another tool. Pass --text for a
    readable transcript instead, or --raw for the lossless envelope (the context
    plus the source agent's verbatim records and session metadata). Several
    sessions print in order, separated by a blank line. A remote source
    (ssh://...) is fetched over ssh first. The source is never modified.

    Examples:
        ccat opencode/ses_abc123
        ccat opencode/ses_a opencode/ses_b
        ccat --text opencode/ses_abc
        ccat --raw opencode/ses_abc > session.json
        ccat opencode/ses_abc | jq .
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
        label, records, raw, info, error = _fetch(ref)
        if error:
            console.print(f"[yellow]{error}[/yellow]")
            continue
        if i:
            print()  # blank line between consecutive sessions
        if text_out:
            _render_transcript(records, color=use_color)
        elif raw_out:
            _render_raw(label, records, raw, info)
        else:
            _render_json(records)
        printed_any = True

    # cat exits non-zero only when nothing could be read; a single bad ref
    # among good ones still prints and exits 0.
    if not printed_any:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
