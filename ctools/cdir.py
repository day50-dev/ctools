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
    cdir -q opencode/       # Quiet: no header/count/hint/source/indent (for scraping)
    cdir claude/            # List sessions for claude
    cdir opencode/*llcat*   # Filter sessions: glob on id, name, or path
    cdir opencode .         # Sessions relevant to this directory (pwd)
    cdir opencode ~/day50   # Sessions from ~/day50 or anything under it
    cdir .                  # Cross-agent: every agent, sessions for this dir
    cdir claude/ -a         # Include subagent sessions (hidden by default)
"""

import argparse
import fnmatch
import json
import os
import shutil
import sys
from datetime import datetime
from typing import List, Optional, Tuple

from rich.console import Console

from ctools.agents import (Session, RESUME_COMMANDS, _resume_override,
                          REGISTRY as AGENTS)
from ctools.cli import (Remote, handle_version, parse_ref, parse_remote_ref,
                        reporting, require_installed, version_option)
from ctools.lib import format_datetime, format_size, get_formatter, install_clean_sigint

__all__ = ['app']

console = Console()

# --- Output field registry (ps-style -o selection) ---

FIELDS = {
    'id': 'Session identifier',
    'name': 'Session title (or ID prefix when no title is set)',
    'ctime': 'Creation / start time',
    'mtime': 'Last modification time',
    'size': 'Stored content size in bytes',
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


def _matches_pattern(session: Session, pattern: str) -> bool:
    """Return True if the glob pattern matches the session id, name, or path.

    Matching is case-insensitive so a quick find is forgiving (llcat ~ LLMCat).
    """
    p = pattern.lower()
    return (fnmatch.fnmatch(session.id.lower(), p)
            or fnmatch.fnmatch((session.name or "").lower(), p)
            or fnmatch.fnmatch((session.path or "").lower(), p))


def _looks_like_path(part: str) -> bool:
    """Is a session-part token a filesystem path rather than a session id?

    A path is absolute, ~-anchored, rooted at `.` or `..` (including the bare
    `.`), or carries an inner slash. No agent's session ids look like that,
    so the two shapes never collide.
    """
    return (part.startswith('/') or part.startswith('~')
            or part.startswith('.') or '/' in part)


def _canonical_dir(p: str) -> str:
    """Canonical form of a directory for comparison: expand ~, make absolute,
    resolve symlinks. realpath does not require the path to exist, so stale
    session cwds canonicalize as far as their prefix allows."""
    try:
        expanded = os.path.expanduser(p)
    except RuntimeError:
        expanded = p
    try:
        return os.path.realpath(expanded)
    except (OSError, ValueError):
        return os.path.abspath(expanded)


def _session_path_matches(session: Session, path_arg: str) -> bool:
    """Is the session's working directory *relevant* to `path_arg`?

    Relevant means the session ran in the given directory itself or *inside*
    it: `cdir opencode .` lists the sessions of this tree, not of its
    ancestors (a session started at `~` is not relevant to `~/day50`).
    Comparisons are case-insensitive on canonical (symlink-resolved) paths.
    Glob characters turn the argument into a case-insensitive pattern over
    the session path.
    """
    if not session.path:
        return False
    if any(c in path_arg for c in '*?['):
        pattern = os.path.expanduser(path_arg)
        if not os.path.isabs(pattern):
            pattern = os.path.abspath(pattern)
        haystack = _canonical_dir(session.path)
        return (fnmatch.fnmatch(haystack.lower(), pattern.lower())
                or fnmatch.fnmatch(session.path.lower(), pattern.lower()))
    # Compare on lowered canonical keys: matching is case-insensitive (the
    # keys are never used to touch the filesystem).
    target = _canonical_dir(path_arg).lower()
    session_dir = _canonical_dir(session.path).lower()
    if session_dir == target:
        return True
    # The query at the filesystem root: every session dir is under it.
    if target == os.sep:
        return True
    # Session dir inside the query dir. A session started *above* the query
    # dir is not relevant to it — `cdir pi ..` finds those instead.
    return (session_dir + os.sep).startswith(target + os.sep)


def _resolve_fields(output: Optional[str]) -> Optional[List[str]]:
    """Resolve a -o value into a field list, handling 'help' and errors."""
    if not output:
        return None
    if output.lower() == 'help':
        _print_field_help()
        raise SystemExit(0)
    fields = [f.strip() for f in output.split(',') if f.strip()]
    for f in fields:
        if f not in FIELDS:
            console.print(f"[red]Unknown field: {f}[/red]")
            console.print(f"[dim]Available fields: {', '.join(FIELDS.keys())}[/dim]")
            console.print("[dim]Use 'cdir -o help' for field descriptions.[/dim]")
            raise SystemExit(1)
    return fields


def _render_table(body_rows: list, fields: List[str], color: bool = False,
                  header: bool = True, indent: bool = True) -> None:
    """Render an aligned table with a header row.

    body_rows is a list of {field: display_value} dicts. Tree connectors are
    embedded in the anchor field's value so later columns stay aligned. With
    header=False the column-label row is omitted; with indent=False the two-space
    left indent is dropped (clean for screen-scraping).
    """
    if not body_rows:
        return

    fields = list(fields)  # _fit_to_terminal may drop columns; never mutate
                           # the caller's list (it can be the LONG_FIELDS global)

    widths = {f: len(FIELD_LABELS[f]) for f in fields}
    for values in body_rows:
        for f in fields:
            widths[f] = max(widths[f], len(values[f]))
    _fit_to_terminal(widths, fields, indent)

    pad = "  " if indent else ""

    def fmt(f: str, v: str) -> str:
        w = widths[f]
        if len(v) > w:
            v = v[:w - 1] + '…' if w > 1 else v[:1]
        if f in RIGHT_ALIGNED:
            return f"{v:>{w}}"
        return f"{v:<{w}}"

    if header:
        print(pad + "  ".join(fmt(f, FIELD_LABELS[f]) for f in fields))
    for values in body_rows:
        cells = [fmt(f, values[f]) for f in fields]
        print(pad + "  ".join(cells))


def _term_width(default: int = 10**9) -> int:
    """Column count of the terminal attached to stdout.

    shutil.get_terminal_size is the cross-platform way: COLUMNS env var,
    then TIOCGWINSZ on POSIX / the console buffer on Windows, then the
    fallback. Non-terminal stdout (pipes, files, pagers like `less`) has no
    width to fit to: the huge default means "never truncate".
    """
    if not sys.stdout.isatty():
        return default
    return shutil.get_terminal_size((default, 24)).columns


def _fit_to_terminal(widths: dict, fields: List[str], indent: bool) -> None:
    """Squeeze then drop columns so the table fits the terminal.

    The free-text columns absorb the squeeze with ellipses — PATH first (the
    redundant column), then NAME, which keeps at least its label. A column
    that would be squeezed below ~8 chars conveys nothing, so it is dropped
    outright and its space goes to the other flex column. IDs, dates, sizes
    and counts are never truncated; a terminal narrower than the fixed
    columns simply overflows. No-op when stdout is not a TTY or the table
    fits. Mutates `widths` and `fields` (call with a copy).
    """
    useful_min = 8  # narrower than this, a column is noise, not signal
    if 'name' not in fields and 'path' not in fields:
        return
    avail = _term_width()

    def total() -> int:
        return sum(widths[f] for f in fields) + 2 * (len(fields) - 1) \
            + (2 if indent else 0)

    if total() <= avail:
        return
    # Squeeze order: PATH (floor 1), then NAME (floor: its label).
    for f in ('path', 'name'):
        if f not in fields:
            continue
        if total() <= avail:
            break
        room = avail - (total() - widths[f])
        floor = len(FIELD_LABELS[f]) if f == 'name' else 1
        other_flex = [x for x in ('path', 'name') if x in fields and x != f]
        if room < useful_min and other_flex:
            fields.remove(f)  # give the space to the other flex column
            continue
        if widths[f] > max(room, floor):
            widths[f] = max(room, floor)
    # Still too wide: drop PATH (the least useful column; the path is the
    # filter, and the rows are already sorted and ID-addressed).
    if 'path' in fields and total() > avail:
        fields.remove('path')


_SORT_ALIASES = {
    '-t': 'time', '--time': 'time',
    '-S': 'size', '--size': 'size',
    '-u': 'ctime', '--ctime': 'ctime',
}


def _resolve_sort(argv: List[str]) -> str:
    """Return 'time', 'size', or 'ctime'; the last sort flag on the command
    line wins (ls semantics for `cdir -tS` vs `cdir -St`). Scans the raw argv
    because the order of boolean flags is what matters, not their set."""
    last = None
    for token in argv:
        if token in _SORT_ALIASES:
            last = _SORT_ALIASES[token]
    return last or 'time'


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


def _resume_hint(agent_name: Optional[str]) -> None:
    """Tell the user how to pick a session back up under this agent.

    Shown only for a single-agent listing (not -R / cross-agent globs, where
    the per-agent command would differ row to row). Human output only —
    machine formats print no prose at all.
    """
    if not agent_name:
        return
    template = _resume_override().get(agent_name) or RESUME_COMMANDS.get(agent_name)
    if template:
        print(f"\n  Resume a session with: {template.replace('{id}', '<id>')}  (or ccat {agent_name}/<id> to read it)")


def _print_sessions(sessions, agent_name, sort, reverse,
                    formatter=None, long_format=False, fields=None,
                    one_line=False, color=False, include_subagents=False,
                    suppress=False):
    """Print sessions in aligned columns with a header row. agent_name shown if provided.

    Subagent sessions (parent_id set) are hidden by default: you can't resume
    one, so a resumable-session listing shouldn't be cluttered with them.
    include_subagents=True (the -a flag) lists them nested under their parent.
    suppress=True (the -q flag) strips every non-data decoration — column
    headers, the session count, the resume hint, and the leading indent — so
    the rows are clean for piping and screen-scraping.
    """
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

    n_sub = len(sessions) - len(top_level)

    if one_line:
        for s in top_level:
            print(_id_of(s))
            if include_subagents:
                for child in children_map.get(s.id, []):
                    print(_id_of(child))
        return

    # Build rows with nesting info and tree prefix
    body_rows = []
    for s in top_level:
        body_rows.append(_session_values(s, fields))
        if not include_subagents:
            continue
        # Add children with tree prefix folded into the anchor field
        children = children_map.get(s.id, [])
        for i, child in enumerate(children):
            prefix = "┗━ " if i == len(children) - 1 else "┣━ "
            values = _session_values(child, fields)
            anchor = 'id' if 'id' in fields else fields[0]
            values[anchor] = prefix + values[anchor]
            body_rows.append(values)

    _render_table(body_rows, fields, color=color,
                  header=not suppress, indent=not suppress)
    if suppress:
        return
    if include_subagents:
        print(f"\n  {len(top_level)} session(s), {n_sub} subagent(s)")
    elif n_sub:
        print(f"\n  {len(top_level)} session(s), {n_sub} subagent(s) hidden "
              f"(-a to show)")
    else:
        print(f"\n  {len(top_level)} session(s)")
    if one_line or formatter:
        return
    _resume_hint(agent_name)


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
    rows = [(agent.name, agent.description,
             f"{agent.base_path}/{agent.files_read or agent.storage_format}",
             agent.exists()) for agent in AGENTS.values()]
    if not rows:
        return

    w_name = max(len(r[0]) for r in rows)
    w_desc = max(len(r[1]) for r in rows)
    w_tail = max(len(r[2]) for r in rows)

    # Fit the terminal like the session tables: squeeze the path column,
    # drop it if it would be too narrow to read. Non-TTY never truncates.
    base = 2 + w_name + 2 + w_desc + 2  # indent + name + gap + desc + gap
    if base + w_tail > _term_width():
        room = _term_width() - base
        w_tail = room if room >= 8 else None  # < 8 chars: noise, drop it

    for label, group in (("Found:", [r for r in rows if r[3]]),
                         ("Not Found:", [r for r in rows if not r[3]])):
        if not group:
            continue
        print(label)
        for name, desc, tail, _ in group:
            line = f"  {name:<{w_name}}  {desc:<{w_desc}}"
            if w_tail is not None:
                if w_tail < len(tail):
                    tail = tail[:w_tail - 1] + '…'
                line += f"  {tail}"
            print(line)


def _remote_agents_installed(remote: Remote) -> list:
    """Which agents have their storage present on `remote`?

    One ssh round trip: the far side just `test -e`'s each agent's default
    install location (anchored at its $HOME), so nothing ctools-related runs
    there. Returns the list of agent names whose storage exists.
    """
    import shlex
    checks = []
    for name, agent in AGENTS.items():
        rel = agent.storage_relative()
        checks.append(f"test -e \"$HOME/{rel}\" && printf '%s\\n' {shlex.quote(name)}")
    cmd = "\n".join(checks) + "\ntrue"
    proc = _remote_probe(remote, cmd)
    if proc is None:
        return []
    return proc.stdout.split()


def _remote_probe(remote: Remote, command: str):
    """Run `command` on `remote`, returning the CompletedProcess, or None if
    ssh itself is unavailable/failed to even start."""
    import shutil
    import subprocess
    if shutil.which("ssh") is None:
        return None
    try:
        return subprocess.run(remote.ssh_command(command),
                              capture_output=True, text=True)
    except OSError:
        return None


def _list_remote_agents(remote: Remote, formatter) -> None:
    """`cdir ssh://host` — list which agents are installed on the remote.

    Mirrors the local `cdir` (Found/Not Found) but resolves the storage
    presence on the far side, so you can see what a host actually has before
    a `ccopy ... ssh://host/agent`.
    """
    if formatter:
        installed = _remote_agents_installed(remote)
        print(json.dumps([{"name": n, "installed": n in installed}
                          for n in AGENTS], indent=2))
        return

    installed = _remote_agents_installed(remote)
    installed_set = set(installed)
    rows = [(name, agent.description, agent.storage_relative(), name in installed_set)
            for name, agent in AGENTS.items()]
    w_name = max(len(r[0]) for r in rows)
    w_desc = max(len(r[1]) for r in rows)
    print(f"{remote.target}:")
    for label, group in (("Found:", [r for r in rows if r[3]]),
                         ("Not Found:", [r for r in rows if not r[3]])):
        if not group:
            continue
        print(label)
        for name, desc, rel, _ in group:
            print(f"  {name:<{w_name}}  {desc:<{w_desc}}  ~/{rel}")


def _handle_remote_ref(ref: str, formatter) -> None:
    """Dispatch a `ssh://[user@]host[:port][/agent]` reference.

    `ssh://host` lists the agents installed on the host (the "what's there?"
    check before a `ccopy ... ssh://host/agent`). A bare `ssh://host/agent` is
    the same listing filtered to that agent. Deeper paths (`.../agent/session`)
    are session refs -- ccat/ccopy's domain -- so cdir points there.
    """
    remote, path = parse_remote_ref(ref)
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        console.print(f"[dim]Session reference {ref} is a ccat/ccopy job, "
                      "not a cdir listing.[/dim]")
        return
    if parts:
        agent_name = parts[0]
        if agent_name not in AGENTS:
            console.print(f"[red]Unknown agent: {agent_name}[/red]")
            console.print(f"[dim]Available agents: {', '.join(AGENTS)}[/dim]")
            raise SystemExit(1)
        _list_remote_agent(remote, agent_name, formatter)
    else:
        _list_remote_agents(remote, formatter)


def _list_remote_agent(remote: Remote, agent_name: str, formatter) -> None:
    """`cdir ssh://host/agent` — is this one agent's storage present there?"""
    installed = _remote_agents_installed(remote)
    present = agent_name in installed
    if formatter:
        print(json.dumps({"name": agent_name, "installed": present,
                          "path": f"~/{AGENTS[agent_name].storage_relative()}"},
                         indent=2))
        return
    if present:
        print(f"{remote.target}: {agent_name} is installed "
              f"(~/{AGENTS[agent_name].storage_relative()})")
    else:
        print(f"{remote.target}: {agent_name} is NOT installed "
              f"(no ~/{AGENTS[agent_name].storage_relative()})")


def _list_all_sessions(sort, reverse, formatter, fields,
                       one_line=False, color=False,
                       include_subagents=False, suppress=False) -> None:
    """List every installed agent's sessions, newest first, agent-prefixed.

    Subagent sessions are hidden unless include_subagents is True (-a).
    """
    all_sessions = []
    for agent in AGENTS.values():
        if not agent.exists():
            continue
        for s in agent.sessions():
            if s.parent_id and not include_subagents:
                continue
            all_sessions.append((agent.name, s))

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
        body_rows.append(values)
    _render_table(body_rows, rfields, color=color,
                  header=not suppress, indent=not suppress)
    if not suppress:
        print(f"\n  {len(body_rows)} session(s)")


def _show_specific_sessions(refs: List[str], sort: str, reverse: bool,
                            formatter, fields, long_format: bool,
                            one_line: bool, color: bool,
                            suppress: bool = False) -> None:
    """Render a unified row listing for a set of exact `agent/session_id`
    references (ls file1 file2 semantics). Sessions that don't exist are
    reported and skipped.
    """
    rows: List[Session] = []
    for ref in refs:
        agent_name, session_id = parse_ref(ref)
        agent = require_installed(agent_name)
        with reporting():
            s = agent.session(session_id)
        if s is None:
            console.print(f"[yellow]No such session: {ref}[/yellow]")
            continue
        rows.append(s)

    if not rows:
        console.print("[yellow]No sessions found[/yellow]")
        return

    if one_line:
        for s in rows:
            print(s.id)
        return

    if formatter:
        print(formatter.format_sessions(rows))
        return

    rfields = fields if fields is not None else (LONG_FIELDS if long_format else DEFAULT_FIELDS)
    key = _sort_key(sort)
    rows.sort(key=key, reverse=not reverse)
    body_rows = [_session_values(s, rfields) for s in rows]
    _render_table(body_rows, rfields, color=color,
                  header=not suppress, indent=not suppress)
    if not suppress:
        print(f"\n  {len(rows)} session(s)")


def _show_agent_glob_sessions(refs: List[str], sort: str, reverse: bool,
                              formatter, fields, long_format: bool,
                              one_line: bool, color: bool,
                              include_subagents: bool = False,
                              suppress: bool = False) -> None:
    """Render rows for references whose agent part is a glob.

    ls segment semantics for `*/...`, `p*/...` and friends: the agent segment
    selects installed agents; the session segment is an exact id, a glob over
    id/name/path (as in `agent/*pattern`), or empty (all sessions). Rows carry
    the agent prefix in the id column so the merged listing is unambiguous.
    """
    rows: List[Tuple[str, Session]] = []
    for ref in refs:
        agent_pat, session_pat = parse_ref(ref)
        _is_glob = bool(session_pat) and any(c in session_pat for c in "*?[")
        _is_path = (bool(session_pat) and not _is_glob
                    and _looks_like_path(session_pat))
        for name, agent in AGENTS.items():
            if not fnmatch.fnmatch(name, agent_pat) or not agent.exists():
                continue
            with reporting():
                if _is_glob:
                    sessions = [s for s in agent.sessions()
                                if _matches_pattern(s, session_pat)]
                    if not include_subagents:
                        sessions = [s for s in sessions if not s.parent_id]
                elif _is_path:
                    sessions = [s for s in agent.sessions()
                                if _session_path_matches(s, session_pat)]
                    if not include_subagents:
                        sessions = [s for s in sessions if not s.parent_id]
                elif session_pat:
                    s = agent.session(session_pat)
                    sessions = [s] if s is not None else []
                else:
                    sessions = agent.sessions()
                    if not include_subagents:
                        sessions = [s for s in sessions if not s.parent_id]
            rows.extend((name, s) for s in sessions)

    if not rows:
        console.print(f"[yellow]No sessions found matching "
                      f"{', '.join(refs)}[/yellow]")
        return

    if one_line:
        for name, s in rows:
            print(f"{name}/{s.id}")
        return

    if formatter:
        print(formatter.format_sessions([s for _, s in rows]))
        return

    rfields = fields if fields is not None else (LONG_FIELDS if long_format
                                                 else DEFAULT_FIELDS)
    rows.sort(key=lambda ns: _sort_key(sort)(ns[1]), reverse=not reverse)
    body_rows = []
    for name, s in rows:
        values = _session_values(s, rfields)
        if 'id' in values:
            values['id'] = f"{name}/{s.id}"
        body_rows.append(values)
    _render_table(body_rows, rfields, color=color,
                  header=not suppress, indent=not suppress)
    if not suppress:
        print(f"\n  {len(body_rows)} session(s)")


def _handle_one_ref(path: str, sort: str, reverse: bool, formatter, fields,
                    long_format: bool, one_line: bool, recursive: bool,
                    color: bool, include_subagents: bool = False,
                    suppress: bool = False) -> None:
    """Dispatch a single `agent[/session_id]` reference.

    A bare agent name (or agent glob) lists/filters sessions. An exact
    `agent/session_id` lists that one session as a row (ls file semantics;
    main() routes exact refs here too). Session contents are ccat's job.
    """
    agent_name, session_id = parse_ref(path)
    agent = require_installed(agent_name)

    # Glob pattern in the session part: filter sessions by id, name, or path.
    # A session part that looks like a filesystem path (no globs of its own)
    # filters by working directory instead: cdir opencode . lists the
    # sessions relevant to that directory.
    _is_glob = any(c in (session_id or "") for c in "*?[")
    _is_path = bool(session_id) and not _is_glob and _looks_like_path(session_id)

    if session_id and not _is_glob and not _is_path:
        _show_specific_sessions([path], sort, reverse, formatter, fields,
                                long_format, one_line, color, suppress)
        return

    with reporting():
        sessions = agent.sessions()

    if session_id and _is_glob:
        sessions = [s for s in sessions if _matches_pattern(s, session_id)]
    elif _is_path:
        sessions = [s for s in sessions
                    if _session_path_matches(s, session_id)]

    if not sessions:
        if session_id:
            console.print(f"[yellow]No sessions matching {agent.name}/{session_id}[/yellow]")
        else:
            console.print(f"[yellow]No sessions found for {agent.name}[/yellow]")
        return

    if not formatter and not one_line and not suppress:
        print(f"  Source: {agent.source}")
        print()

    # Subagent hiding (and the footer's "N hidden" note) happens in
    # _print_sessions, which still sees the full list so it can count them.
    # The agent name drives the resume hint and the per-row prefix. -1 keeps
    # bare IDs (shell-script friendly) so it omits the prefix.
    _print_sessions(sessions, agent.name if not one_line else None,
                    sort, reverse, formatter, long_format, fields,
                    one_line=one_line, color=color,
                    include_subagents=include_subagents,
                    suppress=suppress)


def _merge_path_args(paths: List[str]) -> List[str]:
    """Fold `cdir agent .` (two words) into the single ref `agent/.`.

    A bare agent name or agent glob (no session part) immediately followed by
    a token that looks like a filesystem path is merged: `opencode .` →
    `opencode/.`, `* .` → `*/.`, `p* .` → `p*/.`. The path is just a filter
    on the listing, so it applies to any agent selection. Anything with a
    session part, or a remote ref, passes through untouched.
    """
    out: List[str] = []
    i = 0
    while i < len(paths):
        ref = paths[i]
        nxt = paths[i + 1] if i + 1 < len(paths) else None
        bare = ref.lstrip('@').rstrip('/')
        if (nxt is not None and not ref.startswith('ssh://')
                and '/' not in bare
                and _looks_like_path(nxt)):
            out.append(f'{bare}/{nxt}')
            i += 2
            continue
        out.append(ref)
        i += 1
    return out


def _bare_path_refs(paths: List[str]) -> List[str]:
    """A bare path argument (no agent part) becomes a cross-agent ref.

    `cdir .` / `cdir ~/day50` / `cdir /home/user/proj` behave like `cdir
    '*/.'` / `cdir '*/~/day50'` / `cdir '*/home/user/proj'`: every installed
    agent is searched for sessions relevant to that directory, and rows carry
    the agent prefix. A ref whose first component is a known agent is an
    agent ref, never a path (`pi/...` stays `pi/...` even though it contains
    a slash).
    """
    out: List[str] = []
    for ref in paths:
        if ref.startswith('ssh://'):
            out.append(ref)
            continue
        r = ref.lstrip('@')
        first = r.lstrip('/').split('/', 1)[0]
        # A glob in the first component (`*/*x*`, `p*/...`) is a cross-agent
        # agent glob, not a path; known agent names are agent refs too.
        if (_looks_like_path(r) and first not in AGENTS
                and not any(c in first for c in '*?[')):
            out.append(f'*/{r}')
            continue
        out.append(ref)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cdir",
        description=("List agents and their conversation sessions.\n\n"
                     "Accepts one or more `agent`, `agent/session_id`, or "
                     "`agent/<path>` references. With no arguments, lists all "
                     "known agents. With an agent name, lists sessions for "
                     "that agent. With agent/session_id, shows that session's "
                     "row (contents live in ccat). The session part may also be "
                     "a directory: `cdir opencode .` (or `cdir opencode/.`) "
                     "lists the sessions relevant to that directory — the ones "
                     "that ran there or in a subdirectory of it — and a bare "
                     "path (`cdir .`) does that across every agent. With "
                     "-R and no arguments, recurses all agents. Sort flags are "
                     "ls-style: -t by time, -S by size, -u by creation time; "
                     "when several are given the last one wins (-tS == -S). "
                     "Subagent sessions are hidden by default (they can't be "
                     "resumed); -a/--all includes them."),
    )
    parser.add_argument("paths", nargs="*",
                        help=("One or more agent, agent/session_id, or "
                              "agent/<path> references (a path filters "
                              "sessions by working directory)"))
    parser.add_argument("-t", "--time", action="store_true",
                        help="Sort by modification time")
    parser.add_argument("-S", "--size", action="store_true", help="Sort by size")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Strip decoration for scraping: no column headers, "
                             "no count footer, no resume hint, no Source line, "
                             "no leading indent")
    parser.add_argument("-u", "--ctime", action="store_true",
                        help="Sort by creation time")
    parser.add_argument("-r", "--reverse", action="store_true",
                        help="Reverse sort order")
    parser.add_argument("-R", "--recursive", action="store_true",
                        help="Show agent name, recurse all agents if no path given")
    parser.add_argument("-l", "--long", action="store_true", dest="long_format",
                        help="Show details: modified, size, messages, path")
    parser.add_argument("-1", "--one-line", action="store_true",
                        help="Print one session ID per line, with no header")
    parser.add_argument("-a", "--all", action="store_true",
                        help=("Include subagent sessions (hidden by default: "
                              "they can't be resumed)"))
    parser.add_argument("--color", default="auto",
                        help="Colorize output: never, auto, or always")
    parser.add_argument("-f", "--format", dest="fmt", default="default",
                        help="Output format: json, xml, md, or default")
    parser.add_argument("-o", "--output",
                        help=("Select output fields (comma-separated). "
                              "Use 'help' to list available fields."))
    version_option(parser, "cdir")
    return parser


def main(argv=None) -> int:
    install_clean_sigint()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if (v := handle_version(args)) is not None:
        return v

    paths = args.paths or None
    color = args.color
    fmt = args.fmt
    output = args.output
    reverse = args.reverse
    recursive = args.recursive
    long_format = args.long_format
    one_line = args.one_line

    if color not in ('never', 'auto', 'always'):
        print("Error: --color must be one of: never, auto, always", file=sys.stderr)
        return 2

    fields = _resolve_fields(output)
    sort = _resolve_sort(argv)
    use_color = _use_color(color)
    include_subagents = args.all
    suppress = args.quiet

    formatter = None
    if fmt != "default":
        try:
            formatter = get_formatter(fmt)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return 1

    if not paths:
        if recursive:
            _list_all_sessions(sort, reverse, formatter, fields,
                               one_line=one_line, color=use_color,
                               include_subagents=include_subagents,
                               suppress=suppress)
        else:
            _list_agents(formatter)
        return 0

    def _is_agent_glob(ref: str) -> bool:
        agent, _ = parse_ref(ref)
        return any(c in agent for c in "*?[")

    def _is_exact(ref: str) -> bool:
        _, sid = parse_ref(ref)
        if not sid or any(c in sid for c in "*?["):
            return False
        return not _looks_like_path(sid)

    # Remote references (ssh://...) are handled before the local agent glob /
    # exact split, which assumes an `agent[/session]` local shape.
    # `cdir opencode .` (two words) is the same listing as `cdir opencode/.`:
    # fold a bare agent name followed by a path-looking token into one ref so
    # the path filter rides the usual `agent/session` machinery.
    paths = _merge_path_args(paths)

    # A bare path argument (no agent part) filters every installed agent:
    # `cdir .` is `cdir '*/.'` — every agent, rows agent-prefixed.
    paths = _bare_path_refs(paths)

    remotes = [p for p in paths if p.startswith("ssh://")]
    local = [p for p in paths if not p.startswith("ssh://")]

    emitted = False

    def _sep() -> None:
        nonlocal emitted
        if emitted and not (one_line or formatter):
            print()
        emitted = True

    for ref in remotes:
        _sep()
        _handle_remote_ref(ref, formatter)

    agent_globs = [p for p in local if _is_agent_glob(p)]
    exact = [p for p in local if not _is_agent_glob(p) and _is_exact(p)]
    rest = [p for p in local if not _is_agent_glob(p) and not _is_exact(p)]

    # Cross-agent globs (*/..., p*/...) list matching sessions as rows.
    if agent_globs:
        _sep()
        _show_agent_glob_sessions(agent_globs, sort, reverse, formatter,
                                  fields, long_format, one_line, use_color,
                                  include_subagents, suppress)

    # Exact agent/session references list as rows (ls file semantics),
    # one or many.
    if exact:
        _sep()
        _show_specific_sessions(exact, sort, reverse, formatter, fields,
                                long_format, one_line, use_color, suppress)

    for path in rest:
        _sep()
        _handle_one_ref(path, sort, reverse, formatter, fields, long_format,
                        one_line, recursive, use_color, include_subagents,
                        suppress)
    return 0


app = main


if __name__ == "__main__":
    raise SystemExit(main())
