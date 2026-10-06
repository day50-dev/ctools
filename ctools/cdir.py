#!/usr/bin/env python3
"""
cdir - ls for LLM context windows

Lists agents and their conversation sessions, similar to DOS mtools
but for LLM context windows.

Usage:
    cdir                    # List all known agents
    cdir claude/            # List sessions for Claude
    cdir opencode/          # List sessions for opencode
    cdir -S codex/          # List codex sessions, largest first
    cdir -u codex/          # List codex sessions, oldest first (by creation)
    cdir -1 opencode/       # One session ID per line
    cdir codex/             # List sessions for codex
"""

import json
import sys
from datetime import datetime
from typing import List, Optional

import typer
from rich.console import Console

from ctools.agents import Agent, Session, REGISTRY as AGENTS
from ctools.cli import parse_ref, reporting, require_installed
from ctools.lib import format_datetime, format_size, get_formatter

__all__ = ['app']

app = typer.Typer()
console = Console()

# --- Output field registry (ps-style -o selection) ---

FIELDS = {
    'id': 'Session identifier',
    'name': 'Session title (or ID prefix when no title is set)',
    'ctime': 'Creation / start time',
    'mtime': 'Last modification time',
    'size': 'Size: token count for opencode, bytes for file-based agents',
    'msgs': 'Number of messages in the session',
    'model': 'Model used for the session',
    'path': 'Session working directory (where the conversation\'s code lives)',
    'parent': 'Parent session ID (present on subagent sessions)',
}

FIELD_LABELS = {
    'id': 'ID', 'name': 'NAME', 'ctime': 'CREATED', 'mtime': 'MODIFIED',
    'size': 'SIZE', 'msgs': 'MSGS', 'model': 'MODEL', 'path': 'PATH',
    'parent': 'PARENT',
}

RIGHT_ALIGNED = {'size', 'msgs'}
DEFAULT_FIELDS = ['id', 'name']
LONG_FIELDS = ['id', 'name', 'mtime', 'size', 'msgs', 'path']


def _model_name(model: Optional[str]) -> str:
    """Some agents store the model as a JSON blob rather than a name."""
    if not model:
        return "-"
    try:
        parsed = json.loads(model)
    except (ValueError, TypeError):
        return model
    if isinstance(parsed, dict) and parsed.get('id'):
        return parsed['id']
    return model


_FIELD_VALUES = {
    'id': lambda s: s.id,
    'name': lambda s: s.name,
    'ctime': lambda s: format_datetime(s.ctime),
    'mtime': lambda s: format_datetime(s.mtime),
    'size': lambda s: format_size(s.size),
    'msgs': lambda s: str(s.message_count) if s.message_count else "-",
    'model': lambda s: _model_name(s.model),
    'path': lambda s: s.path or "-",
    'parent': lambda s: s.parent_id or "-",
}


def _field_value(session: Session, field: str) -> str:
    """Return the display value for a session field."""
    try:
        return _FIELD_VALUES[field](session)
    except KeyError:
        raise ValueError(f"Unknown field: {field}")


def _session_values(session: Session, fields: List[str]) -> dict:
    """Build a {field: display_value} dict for a session."""
    return {f: _field_value(session, f) for f in fields}


def _print_field_help() -> None:
    """Print documentation for all -o output fields."""
    print("Output fields for -o/--output (comma-separated):")
    print()
    for name, desc in FIELDS.items():
        print(f"  {name:<10} {desc}")
    print()
    print(f"Default fields: {', '.join(DEFAULT_FIELDS)}")
    print(f"Long format (-l) fields: {', '.join(LONG_FIELDS)}")
    print()
    print("Example: cdir -o id,name,mtime,path opencode/")


def _resolve_fields(output: Optional[str]) -> Optional[List[str]]:
    """Resolve a -o value into a field list, handling 'help' and errors."""
    if not output:
        return None
    if output.lower() == 'help':
        _print_field_help()
        raise typer.Exit(0)
    fields = [f.strip() for f in output.split(',') if f.strip()]
    for f in fields:
        if f not in FIELDS:
            console.print(f"[red]Unknown field: {f}[/red]")
            console.print(f"[dim]Available fields: {', '.join(FIELDS.keys())}[/dim]")
            console.print("[dim]Use 'cdir -o help' for field descriptions.[/dim]")
            raise typer.Exit(1)
    return fields


def _render_table(body_rows: list, fields: List[str], color: bool = False) -> None:
    """Render an aligned table with a header row.

    body_rows is a list of (is_parent, values) tuples where values is a
    {field: display_value} dict. Tree connectors are embedded in the
    anchor field's value so later columns stay aligned.
    """
    BOLD = "\033[1m" if color else ""
    RESET = "\033[0m" if color else ""
    if not body_rows:
        return

    widths = {f: len(FIELD_LABELS[f]) for f in fields}
    for _, values in body_rows:
        for f in fields:
            widths[f] = max(widths[f], len(values[f]))

    def fmt(f: str, v: str) -> str:
        if f in RIGHT_ALIGNED:
            return f"{v:>{widths[f]}}"
        return f"{v:<{widths[f]}}"

    print("  " + "  ".join(fmt(f, FIELD_LABELS[f]) for f in fields))
    for is_parent, values in body_rows:
        cells = []
        for f in fields:
            cell = fmt(f, values[f])
            if is_parent and f in ('id', 'name'):
                cell = f"{BOLD}{cell}{RESET}"
            cells.append(cell)
        print("  " + "  ".join(cells))


def _sort_callback(sort: str):
    """Build a click option callback that records sort flags in argv order.

    click fires option callbacks left to right, so the last sort flag seen on
    the command line is the one that wins — `ls` semantics for `cdir -tS` vs
    `cdir -St`.
    """
    def callback(ctx, value):
        if value:
            if ctx.obj is None:
                ctx.obj = {}
            ctx.obj.setdefault('sort_flags', []).append(sort)
        return value
    return callback


def _resolve_sort(ctx, by_time: bool, by_size: bool, by_ctime: bool) -> str:
    """Return 'time', 'size', or 'ctime'; the last flag on the command line wins."""
    sort_flags = (ctx.obj or {}).get('sort_flags')
    if sort_flags:
        return sort_flags[-1]
    if by_ctime:
        return 'ctime'
    if by_size:
        return 'size'
    return 'time'


def _sort_key(sort: str):
    if sort == 'size':
        return lambda s: s.size
    if sort == 'ctime':
        return lambda s: s.ctime or s.mtime or datetime.min
    return lambda s: s.mtime or s.ctime or datetime.min


def _use_color(mode: str) -> bool:
    """Resolve --color never|auto|always against the terminal."""
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty()


def _print_sessions(sessions, agent_name, sort, reverse,
                    formatter=None, long_format=False, fields=None,
                    one_line=False, color=False):
    """Print sessions in aligned columns with a header row. agent_name shown if provided."""
    if not sessions:
        console.print("[yellow]No sessions found[/yellow]")
        return

    if fields is None:
        fields = LONG_FIELDS if long_format else DEFAULT_FIELDS

    if formatter:
        print(formatter.format_sessions(sessions, agent_name))
        return

    # Build parent-child mapping
    children_map = {}
    top_level = []
    for s in sessions:
        if s.parent_id:
            children_map.setdefault(s.parent_id, []).append(s)
        else:
            top_level.append(s)

    key = _sort_key(sort)
    top_level.sort(key=key, reverse=not reverse)
    for parent_id in children_map:
        children_map[parent_id].sort(key=key, reverse=not reverse)

    def _id_of(s):
        return f"{agent_name}/{s.id}" if agent_name else s.id

    if one_line:
        for s in top_level:
            print(_id_of(s))
            for child in children_map.get(s.id, []):
                print(_id_of(child))
        return

    # Build rows with nesting info and tree prefix
    body_rows = []
    for s in top_level:
        body_rows.append((True, _session_values(s, fields)))

        # Add children with tree prefix folded into the anchor field
        children = children_map.get(s.id, [])
        for i, child in enumerate(children):
            prefix = "┗━ " if i == len(children) - 1 else "┣━ "
            values = _session_values(child, fields)
            anchor = 'id' if 'id' in fields else fields[0]
            values[anchor] = prefix + values[anchor]
            body_rows.append((False, values))

    _render_table(body_rows, fields, color=color)
    print(f"\n  {len(top_level)} session(s), {len(sessions) - len(top_level)} subagent(s)")


def _list_agents(formatter) -> None:
    """Print the agent registry, installed ones first."""
    if formatter:
        # The agent list has no formatter-specific shape; JSON serves all.
        print(json.dumps([{
            'name': agent.name,
            'description': agent.description,
            'path': str(agent.base_path),
            'files_read': agent.files_read,
            'exists': agent.exists(),
        } for agent in AGENTS.values()], indent=2))
        return

    # The registry key, not the display name: this column is what the user
    # types back at us as `cdir <agent>/`, so it has to be shell-safe.
    rows = [(agent.name, agent.description, str(agent.base_path),
             agent.files_read or agent.storage_format, agent.exists())
            for agent in AGENTS.values()]
    found = [r for r in rows if r[4]]
    missing = [r for r in rows if not r[4]]
    if not rows:
        return

    w_name = max(len(r[0]) for r in rows)
    w_desc = max(len(r[1]) for r in rows)

    for label, group in (("Found:", found), ("Not Found:", missing)):
        if not group:
            continue
        print(label)
        for name, desc, path, files_read, _ in group:
            print(f"  {name:<{w_name}}  {desc:<{w_desc}}  {path}/{files_read}")


def _export_session(agent: Agent, session_id: str, formatter) -> None:
    """Print one session's messages."""
    with reporting():
        messages = agent.messages(session_id)
    if not messages:
        console.print(f"[yellow]Session not found: {session_id}[/yellow]")
        raise typer.Exit(1)

    if formatter:
        print(formatter.format_session_export(messages, session_id, agent.name))
    else:
        print(json.dumps([{'role': m.role, 'content': m.content} for m in messages], indent=2))


def _list_all_sessions(sort, reverse, formatter, fields,
                       one_line=False, color=False) -> None:
    """List every installed agent's sessions, newest first, agent-prefixed."""
    all_sessions = []
    for agent in AGENTS.values():
        if not agent.exists():
            continue
        all_sessions.extend((agent.name, s) for s in agent.sessions())

    if not all_sessions:
        console.print("[yellow]No sessions found[/yellow]")
        return

    key = _sort_key(sort)
    all_sessions.sort(key=lambda pair: key(pair[1]), reverse=not reverse)

    if one_line:
        for agent_name, session in all_sessions:
            print(f"{agent_name}/{session.id}")
        return

    if formatter:
        print(formatter.format_sessions([s for _, s in all_sessions]))
        return

    rfields = fields if fields is not None else LONG_FIELDS
    body_rows = []
    for agent_name, session in all_sessions:
        values = _session_values(session, rfields)
        if 'id' in values:
            values['id'] = f"{agent_name}/{session.id}"
        body_rows.append((True, values))
    _render_table(body_rows, rfields, color=color)
    print(f"\n  {len(body_rows)} session(s)")


@app.command()
def main(
    ctx: typer.Context,
    path: Optional[str] = typer.Argument(None, help="Agent or agent/session_id"),
    by_time: bool = typer.Option(False, "--time", "-t", callback=_sort_callback('time'), help="Sort by modification time"),
    by_size: bool = typer.Option(False, "--size", "-S", "-s", callback=_sort_callback('size'), help="Sort by size"),
    by_ctime: bool = typer.Option(False, "--ctime", "-u", callback=_sort_callback('ctime'), help="Sort by creation time"),
    reverse: bool = typer.Option(False, "--reverse", "-r", help="Reverse sort order"),
    recursive: bool = typer.Option(False, "--recursive", "-R", help="Show agent name, recurse all agents if no path given"),
    long_format: bool = typer.Option(False, "--long", "-l", help="Show details: modified, size, messages, path"),
    one_line: bool = typer.Option(False, "--one-line", "-1", help="Print one session ID per line, with no header"),
    color: str = typer.Option("auto", "--color", help="Colorize output: never, auto, or always"),
    fmt: str = typer.Option("default", "--format", "-f", help="Output format: json, xml, md, or default"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Select output fields (comma-separated). Use 'help' to list available fields."),
):
    """
    List agents and their conversation sessions.

    Without arguments, lists all known agents.
    With an agent name, lists sessions for that agent.
    With agent/session_id, exports that session.
    With -R, shows agent name and recurse all agents if no path given.
    With -l, shows full details (modified, size, message count, path).
    With -o, selects the output fields shown (see 'cdir -o help').
    With -1, prints one session ID per line (no header, no tree).
    Sort flags are ls-style: -t by time, -S by size, -u by creation time,
    and when several are given the last one wins (-tS == -S).
    """
    if color not in ('never', 'auto', 'always'):
        raise typer.BadParameter("must be one of: never, auto, always", param_hint="--color")

    fields = _resolve_fields(output)
    sort = _resolve_sort(ctx, by_time, by_size, by_ctime)
    use_color = _use_color(color)

    formatter = None
    if fmt != "default":
        try:
            formatter = get_formatter(fmt)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    if path is None:
        if recursive:
            _list_all_sessions(sort, reverse, formatter, fields,
                               one_line=one_line, color=use_color)
        else:
            _list_agents(formatter)
        return

    agent_name, session_id = parse_ref(path)
    agent = require_installed(agent_name)

    if session_id:
        _export_session(agent, session_id, formatter)
        return

    with reporting():
        sessions = agent.sessions()

    if not sessions:
        console.print(f"[yellow]No sessions found for {agent.name}[/yellow]")
        return

    if not formatter and not one_line:
        print(f"  Source: {agent.source}")
        print()

    _print_sessions(sessions, agent.name if recursive else None,
                    sort, reverse, formatter, long_format, fields,
                    one_line=one_line, color=use_color)


if __name__ == "__main__":
    app()
