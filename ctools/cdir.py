#!/usr/bin/env python3
"""
cdir - ls for LLM context windows

Lists agents and their conversation sessions, similar to DOS mtools
but for LLM context windows.

Usage:
    cdir                    # List all known agents
    cdir claude/            # List sessions for Claude
    cdir opencode/          # List sessions for opencode
    cdir codex/             # List sessions for codex
"""

import os
import sys
import json
import sqlite3
import typer
from pathlib import Path
from datetime import datetime
from typing import List, Optional
from rich.console import Console

from ctools.lib import (
    Session, Agent, AGENTS, Message,
    get_formatter, format_size, format_datetime,
    JsonFormatter, XmlFormatter, MarkdownFormatter
)

# Re-export for backward compatibility
__all__ = ['Session', 'Agent', 'AGENTS', 'app']

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
    'path': 'Source path where the session is stored',
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


def _field_value(session: Session, field: str) -> str:
    """Return the display value for a session field."""
    if field == 'id':
        return session.id
    if field == 'name':
        return session.name
    if field == 'ctime':
        return format_datetime(session.ctime)
    if field == 'mtime':
        return format_datetime(session.mtime)
    if field == 'size':
        return format_size(session.size)
    if field == 'msgs':
        return str(session.message_count) if session.message_count else "-"
    if field == 'model':
        if not session.model:
            return "-"
        try:
            parsed = json.loads(session.model)
            if isinstance(parsed, dict) and parsed.get('id'):
                return parsed['id']
        except (ValueError, TypeError):
            pass
        return session.model
    if field == 'path':
        return session.path or "-"
    if field == 'parent':
        return session.parent_id or "-"
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


def _render_table(body_rows: list, fields: List[str]) -> None:
    """Render an aligned table with a header row.

    body_rows is a list of (is_parent, values) tuples where values is a
    {field: display_value} dict. Tree connectors are embedded in the
    anchor field's value so later columns stay aligned.
    """
    BOLD = "\033[1m"
    RESET = "\033[0m"
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


def get_file_metadata(path: Path) -> tuple:
    """Get creation time, modification time, and size of a file."""
    stat = path.stat()
    ctime = datetime.fromtimestamp(stat.st_ctime)
    mtime = datetime.fromtimestamp(stat.st_mtime)
    size = stat.st_size
    return ctime, mtime, size


def get_claude_sessions(agent: Agent) -> List[Session]:
    """Extract sessions from Claude Desktop."""
    sessions = []
    if not agent.base_path.exists():
        return sessions
    
    # Config files to skip (not conversation sessions)
    SKIP_FILES = {'manifest.json', 'plugin.json', 'scheduled-tasks.json', 
                  'remote-session-spaces.json', 'ant-device-registry.json'}
    
    for session_file in agent.base_path.glob(agent.session_pattern):
        # Skip config/manifest files
        if session_file.name in SKIP_FILES:
            continue
        # Skip .claude-plugin directories
        if '.claude-plugin' in session_file.parts:
            continue
            
        try:
            ctime, mtime, size = get_file_metadata(session_file)
            
            # Try to extract session info from JSON
            with open(session_file, 'r') as f:
                data = json.load(f)
            
            session_id = session_file.stem
            name = data.get('name', session_id[:8])
            
            sessions.append(Session(
                id=session_id,
                name=name,
                ctime=ctime,
                mtime=mtime,
                size=size,
                path=str(session_file)
            ))
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    
    return sessions


def get_claude_code_sessions(agent: Agent) -> List[Session]:
    """Extract sessions from Claude Code CLI."""
    sessions = []
    if not agent.base_path.exists():
        return sessions
    
    for session_file in agent.base_path.glob(agent.session_pattern):
        try:
            ctime, mtime, size = get_file_metadata(session_file)
            
            # Claude Code uses JSONL format
            with open(session_file, 'r') as f:
                lines = f.readlines()
            
            session_id = session_file.stem
            name = session_id[:8]
            
            # Try to extract name from first message
            if lines:
                try:
                    first_msg = json.loads(lines[0])
                    if 'type' in first_msg and first_msg['type'] == 'human':
                        name = first_msg.get('message', {}).get('content', name)[:50]
                except (json.JSONDecodeError, KeyError):
                    pass
            
            sessions.append(Session(
                id=session_id,
                name=name,
                ctime=ctime,
                mtime=mtime,
                size=size,
                path=str(session_file),
                message_count=len(lines)
            ))
        except (OSError, IndexError):
            continue
    
    return sessions


def get_opencode_sessions(agent: Agent) -> List[Session]:
    """Extract sessions from opencode SQLite database."""
    sessions = []
    db_path = agent.base_path / 'opencode.db'
    
    if not db_path.exists():
        return sessions
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        # Query sessions table with actual column names
        # Columns: id, title, time_created (ms), time_updated (ms), 
        #          tokens_input, tokens_output, model, directory
        cursor.execute('''
            SELECT id, title, time_created, time_updated, 
                   tokens_input, tokens_output, model, directory, parent_id
            FROM session 
            ORDER BY time_updated DESC
        ''')
        
        for row in cursor.fetchall():
            session_id, title, time_created, time_updated, tokens_input, tokens_output, model, directory, parent_id = row
            
            # Parse timestamps (milliseconds since epoch)
            ctime = None
            mtime = None
            if time_created:
                try:
                    ctime = datetime.fromtimestamp(time_created / 1000)
                except (ValueError, TypeError, OSError):
                    pass
            if time_updated:
                try:
                    mtime = datetime.fromtimestamp(time_updated / 1000)
                except (ValueError, TypeError, OSError):
                    pass
            
            # Calculate size from tokens
            size = (tokens_input or 0) + (tokens_output or 0)
            
            # Get message count from message table
            msg_count = None
            try:
                cursor.execute('SELECT COUNT(*) FROM message WHERE session_id = ?', (session_id,))
                msg_count = cursor.fetchone()[0]
            except sqlite3.Error:
                pass
            
            sessions.append(Session(
                id=session_id,
                name=title or session_id[:8],
                ctime=ctime,
                mtime=mtime,
                size=size,
                path=directory or str(db_path),
                model=model,
                message_count=msg_count,
                parent_id=parent_id
            ))
        
        conn.close()
    except sqlite3.Error:
        pass
    
    return sessions


def get_codex_sessions(agent: Agent) -> List[Session]:
    """Extract sessions from OpenAI Codex CLI."""
    sessions = []
    if not agent.base_path.exists():
        return sessions
    
    # Check for SQLite index first (more reliable)
    sqlite_path = agent.base_path / 'state_5.sqlite'
    if sqlite_path.exists():
        try:
            conn = sqlite3.connect(str(sqlite_path))
            cursor = conn.cursor()
            
            # Query sessions from SQLite
            cursor.execute('''
                SELECT id, title, cwd, model, created_at, updated_at
                FROM sessions 
                ORDER BY updated_at DESC
            ''')
            
            for row in cursor.fetchall():
                session_id, title, cwd, model, created_at, updated_at = row
                
                ctime = None
                mtime = None
                if created_at:
                    try:
                        ctime = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    except (ValueError, TypeError):
                        pass
                if updated_at:
                    try:
                        mtime = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                    except (ValueError, TypeError):
                        pass
                
                sessions.append(Session(
                    id=session_id,
                    name=title or session_id[:8],
                    ctime=ctime,
                    mtime=mtime,
                    size=0,  # Will be updated from rollout files
                    path=cwd or str(sqlite_path),
                    model=model
                ))
            
            conn.close()
        except sqlite3.Error:
            pass
    
    # Fall back to JSONL rollout files
    if not sessions:
        for session_file in agent.base_path.glob(agent.session_pattern):
            try:
                ctime, mtime, size = get_file_metadata(session_file)
                
                session_id = session_file.stem
                name = session_id[:8]
                
                # Try to extract metadata from first line
                with open(session_file, 'r') as f:
                    first_line = f.readline()
                    if first_line:
                        try:
                            data = json.loads(first_line)
                            if 'session_meta' in data:
                                meta = data['session_meta']
                                session_id = meta.get('id', session_id)
                                name = meta.get('title', name)
                        except json.JSONDecodeError:
                            pass
                
                sessions.append(Session(
                    id=session_id,
                    name=name,
                    ctime=ctime,
                    mtime=mtime,
                    size=size,
                    path=str(session_file)
                ))
            except OSError:
                continue
    
    return sessions


# --- Pi Coding Agent ---

def _pi_parse_timestamp(ts) -> Optional[datetime]:
    """Parse a pi ISO-8601 timestamp (UTC, may end in 'Z').

    Returns a naive local datetime to match the rest of the codebase.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _pi_message_text(message) -> str:
    """Extract plain text from a pi message (string or block-list content)."""
    content = message.get('content', '') if isinstance(message, dict) else message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                text = block.get('text', '')
                if text:
                    parts.append(text)
        return '\n'.join(parts)
    return ''


def _pi_session_uuid(path_str) -> Optional[str]:
    """Extract a pi session uuid from a parentSession path or uuid string."""
    if not path_str:
        return None
    stem = Path(str(path_str)).stem
    if '_' in stem:
        return stem.split('_', 1)[1]
    return stem


def _find_pi_session_file(agent_path: Path, session_id: str) -> Optional[Path]:
    """Locate a pi session file by filename suffix or header uuid."""
    if not agent_path.exists():
        return None
    for session_file in agent_path.glob('**/*.jsonl'):
        if session_id in session_file.name:
            return session_file
        try:
            with open(session_file, 'r') as f:
                header = json.loads(f.readline())
            if header.get('type') == 'session' and header.get('id') == session_id:
                return session_file
        except (json.JSONDecodeError, OSError):
            continue
    return None


def get_pi_sessions(agent: Agent) -> List[Session]:
    """Extract sessions from Pi Coding Agent.

    Pi stores each session as a JSONL tree file under
    ~/.pi/agent/sessions/--<cwd-with-slashes-as--->/<timestamp>_<uuid>.jsonl.
    The first line is a session header with id, timestamp, and cwd.
    """
    sessions = []
    if not agent.base_path.exists():
        return sessions

    for session_file in sorted(agent.base_path.glob(agent.session_pattern)):
        try:
            header = None
            name = None
            cwd = None
            parent_session = None
            model = None
            message_count = 0
            total_tokens = 0
            first_user_text = None
            header_ts = None
            last_ts = None

            with open(session_file, 'r') as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    etype = entry.get('type')
                    ts = entry.get('timestamp')
                    if etype == 'session':
                        header = entry
                        header_ts = ts
                        cwd = entry.get('cwd')
                        parent_session = entry.get('parentSession')
                        continue
                    if ts:
                        last_ts = ts
                    if etype == 'session_info':
                        if entry.get('name') and not name:
                            name = entry['name']
                    elif etype == 'model_change':
                        model = entry.get('modelId') or model
                    elif etype == 'message':
                        message_count += 1
                        msg = entry.get('message') or {}
                        role = msg.get('role')
                        if role == 'user' and first_user_text is None:
                            first_user_text = _pi_message_text(msg)
                        model = msg.get('model') or model
                        usage = msg.get('usage') or {}
                        total_tokens += usage.get('totalTokens', 0) or 0
                    elif etype in ('compaction', 'branch_summary'):
                        usage = entry.get('usage') or {}
                        total_tokens += usage.get('totalTokens', 0) or 0

            if not header:
                continue

            session_id = header.get('id') or session_file.stem
            if not name:
                name = first_user_text.strip() if first_user_text else ''
                if len(name) > 80:
                    name = name[:80] + '...'
                if not name:
                    name = session_id[:8]
            ctime = _pi_parse_timestamp(header_ts)
            mtime = _pi_parse_timestamp(last_ts) or ctime

            sessions.append(Session(
                id=session_id,
                name=name,
                ctime=ctime,
                mtime=mtime,
                size=total_tokens or session_file.stat().st_size,
                path=cwd or str(session_file),
                model=model,
                message_count=message_count,
                parent_id=_pi_session_uuid(parent_session),
            ))
        except (OSError, ValueError):
            continue

    return sessions


def export_pi_session(agent_info: Agent, session_id: str) -> List[Message]:
    """Export a pi session's active branch as Message objects.

    Walks the active leaf branch (newest leaf in the message tree) back to the
    root via parentId links, returning user/assistant messages in order.
    """
    session_file = _find_pi_session_file(agent_info.base_path, session_id)
    if not session_file:
        return []

    entries = []
    parents = {}
    children = {}
    by_id = {}
    with open(session_file, 'r') as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            eid = entry.get('id')
            if not eid:
                continue
            entries.append(entry)
            by_id[eid] = entry
            pid = entry.get('parentId')
            parents[eid] = pid
            children.setdefault(pid, []).append(eid)

    if not entries:
        return []

    leaves = [eid for eid in by_id if not children.get(eid)]
    if leaves:
        leaves.sort(key=lambda eid: by_id[eid].get('timestamp') or '')
        active_leaf = leaves[-1]
    else:
        active_leaf = entries[-1].get('id')

    branch = []
    node = active_leaf
    seen = set()
    while node and node in by_id and node not in seen:
        seen.add(node)
        branch.append(by_id[node])
        node = parents.get(node)
    branch.reverse()

    messages = []
    for entry in branch:
        if entry.get('type') != 'message':
            continue
        msg = entry.get('message') or {}
        role = msg.get('role')
        if role not in ('user', 'assistant'):
            continue
        content = _pi_message_text(msg)
        if content:
            messages.append(Message(role=role, content=content))
    return messages


# Session extractors for each agent
SESSION_EXTRACTORS = {
    'claude': get_claude_sessions,
    'claude-code': get_claude_code_sessions,
    'opencode': get_opencode_sessions,
    'codex': get_codex_sessions,
    'pi': get_pi_sessions,
}


def export_opencode_session(agent_info: Agent, session_id: str) -> List[Message]:
    """Export an opencode session as Message objects."""
    db_path = agent_info.base_path / 'opencode.db'
    if not db_path.exists():
        return []
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # Get messages for this session, ordered by time
    cursor.execute('''
        SELECT m.id, m.data, m.time_created
        FROM message m
        WHERE m.session_id = ?
        ORDER BY m.time_created
    ''', (session_id,))
    
    messages = []
    for msg_id, msg_data, time_created in cursor.fetchall():
        data = json.loads(msg_data)
        role = data.get('role', 'user')
        
        # Get parts for this message
        cursor.execute('''
            SELECT data FROM part
            WHERE message_id = ?
            ORDER BY time_created
        ''', (msg_id,))
        
        content_parts = []
        for (part_data,) in cursor.fetchall():
            part = json.loads(part_data)
            if part.get('type') == 'text':
                content_parts.append(part.get('text', ''))
        
        content = '\n'.join(content_parts) if content_parts else ''
        
        if role in ('user', 'assistant') and content:
            messages.append(Message(role=role, content=content))
    
    conn.close()
    return messages


def export_claude_code_session(agent_info: Agent, session_id: str) -> List[Message]:
    """Export a claude-code session as Message objects."""
    # Find the JSONL file for this session
    for session_file in agent_info.base_path.glob(agent_info.session_pattern):
        if session_file.stem == session_id:
            messages = []
            with open(session_file, 'r') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        msg_type = data.get('type', '')
                        if msg_type == 'human':
                            content = data.get('message', {}).get('content', '')
                            if content:
                                messages.append(Message(role='user', content=content))
                        elif msg_type == 'assistant':
                            content = data.get('message', {}).get('content', '')
                            if content:
                                messages.append(Message(role='assistant', content=content))
                    except json.JSONDecodeError:
                        continue
            return messages
    return []


def export_claude_session(agent_info: Agent, session_id: str) -> List[Message]:
    """Export a claude-desktop session as Message objects."""
    for session_file in agent_info.base_path.glob(agent_info.session_pattern):
        if session_file.stem == session_id:
            try:
                with open(session_file, 'r') as f:
                    data = json.load(f)
                # Claude Desktop stores messages directly
                messages_raw = data if isinstance(data, list) else data.get('messages', [])
                return [Message(role=m.get('role', ''), content=m.get('content', '')) 
                        for m in messages_raw if m.get('content')]
            except (json.JSONDecodeError, KeyError):
                pass
    return []


EXPORTERS = {
    'opencode': export_opencode_session,
    'claude-code': export_claude_code_session,
    'claude': export_claude_session,
    'pi': export_pi_session,
}


def _print_sessions(sessions, agent_name, by_time, by_size, reverse, formatter=None, long_format=False, fields=None):
    """Print sessions in aligned columns with a header row. agent_name shown if provided."""
    if not sessions:
        console.print(f"[yellow]No sessions found[/yellow]")
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

    # Sort function
    def sort_key(s):
        if by_size:
            return s.size
        return s.mtime or s.ctime or datetime.min

    top_level.sort(key=sort_key, reverse=not reverse)
    for parent_id in children_map:
        children_map[parent_id].sort(key=sort_key, reverse=not reverse)

    # Build rows with nesting info and tree prefix
    body_rows = []
    for s in top_level:
        body_rows.append((True, _session_values(s, fields)))

        # Add children with tree prefix folded into the anchor field
        children = children_map.get(s.id, [])
        for i, child in enumerate(children):
            is_last = (i == len(children) - 1)
            prefix = "┗━ " if is_last else "┣━ "
            values = _session_values(child, fields)
            anchor = 'id' if 'id' in fields else fields[0]
            values[anchor] = prefix + values[anchor]
            body_rows.append((False, values))

    _render_table(body_rows, fields)
    print(f"\n  {len(top_level)} session(s), {len(sessions) - len(top_level)} subagent(s)")


@app.command()
def main(
    path: Optional[str] = typer.Argument(None, help="Agent or agent/session_id"),
    by_time: bool = typer.Option(False, "--time", "-t", help="Sort by modification time"),
    by_size: bool = typer.Option(False, "--size", "-s", help="Sort by size"),
    reverse: bool = typer.Option(False, "--reverse", "-r", help="Reverse sort order"),
    recursive: bool = typer.Option(False, "--recursive", "-R", help="Show agent name, recurse all agents if no path given"),
    long_format: bool = typer.Option(False, "--long", "-l", help="Show details: modified, size, messages, path"),
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
    """
    # Resolve -o field selection (handles 'help' and validation)
    fields = _resolve_fields(output)

    # Get formatter if specified
    formatter = None
    if fmt != "default":
        try:
            formatter = get_formatter(fmt)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    
    if path is None and not recursive:
        # List all agents with aligned columns
        if formatter:
            # For agents list, use JSON format as base
            data = []
            for name, agent_info in AGENTS.items():
                data.append({
                    'name': name,
                    'description': agent_info.description,
                    'path': str(agent_info.base_path),
                    'files_read': agent_info.files_read,
                    'exists': agent_info.base_path.exists(),
                })
            print(json.dumps(data, indent=2))
        else:
            found = []
            missing = []
            for name, agent_info in AGENTS.items():
                display = agent_info.display_name or name
                path = agent_info.base_path
                exists = path.exists()
                
                display_path = str(path)
                entry = (display, agent_info.description, display_path, agent_info.files_read or agent_info.storage_format, exists)
                if exists:
                    found.append(entry)
                else:
                    missing.append(entry)

            all_entries = found + missing
            if all_entries:
                w_name = max(len(r[0]) for r in all_entries)
                w_desc = max(len(r[1]) for r in all_entries)
                w_path = max(len(r[2]) for r in all_entries)

            if found:
                print("Found:")
                for name, desc, path, files_read, exists in found:
                    print(f"  {name:<{w_name}}  {desc:<{w_desc}}  {path}/{files_read}")

            if missing:
                print("Not Found:")
                for name, desc, path, files_read, exists in missing:
                    print(f"  {name:<{w_name}}  {desc:<{w_desc}}  {path}/{files_read}")
    elif path is not None:
        # Parse agent/session_id format
        parts = path.strip('/').split('/', 1)
        agent_name = parts[0]
        session_id = parts[1] if len(parts) > 1 else None
        
        if agent_name not in AGENTS:
            console.print(f"[red]Unknown agent: {agent_name}[/red]")
            console.print(f"[dim]Available agents: {', '.join(AGENTS.keys())}[/dim]")
            raise typer.Exit(1)
        
        agent_info = AGENTS[agent_name]
        
        if not agent_info.base_path.exists():
            console.print(f"[yellow]Agent path not found: {agent_info.base_path}[/yellow]")
            console.print(f"[dim]Is {agent_name} installed?[/dim]")
            raise typer.Exit(1)
        
        if session_id:
            # Export specific session
            exporter = EXPORTERS.get(agent_name)
            if not exporter:
                console.print(f"[red]No exporter for {agent_name}[/red]")
                raise typer.Exit(1)
            
            messages = exporter(agent_info, session_id)
            if not messages:
                console.print(f"[yellow]Session not found: {session_id}[/yellow]")
                raise typer.Exit(1)
            
            if formatter:
                print(formatter.format_session_export(messages, session_id, agent_name))
            else:
                # Legacy JSON format for backward compatibility
                print(json.dumps([{'role': m.role, 'content': m.content} for m in messages], indent=2))
        else:
            # List sessions for agent
            extractor = SESSION_EXTRACTORS.get(agent_name)
            if not extractor:
                console.print(f"[red]No session extractor for {agent_name}[/red]")
                raise typer.Exit(1)
            
            sessions = extractor(agent_info)
            
            if not sessions:
                console.print(f"[yellow]No sessions found for {agent_name}[/yellow]")
                return
            
            if sessions and not formatter:
                source = agent_info.base_path / agent_info.files_read if agent_info.files_read else agent_info.base_path
                print(f"  Source: {source}")
                print()
            
            _print_sessions(sessions, agent_name if recursive else None, by_time, by_size, reverse, formatter, long_format, fields)
    else:
        if recursive:
            # List all agents' sessions with agent name prefix
            all_sessions = []
            for name, agent_info in AGENTS.items():
                if not agent_info.base_path.exists():
                    continue
                extractor = SESSION_EXTRACTORS.get(name)
                if not extractor:
                    continue
                for s in extractor(agent_info):
                    all_sessions.append((name, s))
            
            if not all_sessions:
                console.print("[yellow]No sessions found[/yellow]")
                return
            
            # Sort by mtime
            all_sessions.sort(key=lambda x: x[1].mtime or x[1].ctime or datetime.min, reverse=not reverse)
            
            if formatter:
                sessions = [s for _, s in all_sessions]
                print(formatter.format_sessions(sessions))
            else:
                rfields = fields if fields is not None else LONG_FIELDS
                body_rows = []
                for agent_name, s in all_sessions:
                    values = _session_values(s, rfields)
                    values['id'] = f"{agent_name}/{s.id}"
                    body_rows.append((True, values))
                _render_table(body_rows, rfields)
                print(f"\n  {len(body_rows)} session(s)")
        else:
            pass  # handled above


if __name__ == "__main__":
    app()
