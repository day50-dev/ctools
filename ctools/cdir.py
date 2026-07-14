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
    
    for session_file in agent.base_path.glob(agent.session_pattern):
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
                   tokens_input, tokens_output, model, directory
            FROM session 
            ORDER BY time_updated DESC
        ''')
        
        for row in cursor.fetchall():
            session_id, title, time_created, time_updated, tokens_input, tokens_output, model, directory = row
            
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
                path=str(db_path),
                model=model,
                message_count=msg_count
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
                    path=str(sqlite_path),
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


# Session extractors for each agent
SESSION_EXTRACTORS = {
    'claude': get_claude_sessions,
    'claude-code': get_claude_code_sessions,
    'opencode': get_opencode_sessions,
    'codex': get_codex_sessions,
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
}


def _print_sessions(sessions, agent_name, by_time, by_size, reverse, formatter=None):
    """Print sessions in aligned columns. agent_name shown if provided."""
    if not sessions:
        console.print(f"[yellow]No sessions found[/yellow]")
        return
    
    if by_time:
        sessions.sort(key=lambda s: s.mtime or s.ctime or datetime.min, reverse=not reverse)
    elif by_size:
        sessions.sort(key=lambda s: s.size, reverse=not reverse)
    else:
        sessions.sort(key=lambda s: s.mtime or s.ctime or datetime.min, reverse=not reverse)
    
    if formatter:
        print(formatter.format_sessions(sessions, agent_name))
        return
    
    rows = []
    for s in sessions:
        ctime = format_datetime(s.ctime)
        mtime = format_datetime(s.mtime)
        size = format_size(s.size)
        msgs = str(s.message_count) if s.message_count else "-"
        rows.append((s.id, s.name, ctime, mtime, size, msgs))
    
    w_id = max(len(r[0]) for r in rows)
    w_name = max(len(r[1]) for r in rows)
    w_ctime = max(len(r[2]) for r in rows)
    w_mtime = max(len(r[3]) for r in rows)
    w_size = max(len(r[4]) for r in rows)
    w_msgs = max(len(r[5]) for r in rows)
    
    for id, name, ctime, mtime, size, msgs in rows:
        print(f"  {id:<{w_id}}  {name:<{w_name}}  {ctime:<{w_ctime}}  {mtime:<{w_mtime}}  {size:>{w_size}}  {msgs:>{w_msgs}}")
    
    print(f"\n  {len(rows)} session(s)")


@app.command()
def main(
    path: Optional[str] = typer.Argument(None, help="Agent or agent/session_id"),
    agents: bool = typer.Option(False, "--agents", "-a", help="List supported agents"),
    by_time: bool = typer.Option(False, "--time", "-t", help="Sort by modification time"),
    by_size: bool = typer.Option(False, "--size", "-s", help="Sort by size"),
    reverse: bool = typer.Option(False, "--reverse", "-r", help="Reverse sort order"),
    recursive: bool = typer.Option(False, "--recursive", "-R", help="Show agent name, recurse all agents if no path given"),
    fmt: str = typer.Option("default", "--format", "-f", help="Output format: json, xml, md, or default")
):
    """
    List agents and their conversation sessions.
    
    With --agents, lists all known agents.
    With an agent name, lists sessions for that agent.
    With agent/session_id, exports that session.
    With -R, shows agent name and recurse all agents if no path given.
    """
    # Get formatter if specified
    formatter = None
    if fmt != "default":
        try:
            formatter = get_formatter(fmt)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    
    if agents or (path is None and not recursive):
        # List all agents with aligned columns
        if formatter:
            # For agents list, use JSON format as base
            data = []
            for name, agent_info in AGENTS.items():
                data.append({
                    'name': name,
                    'description': agent_info.description,
                    'path': str(agent_info.base_path),
                    'format': agent_info.storage_format,
                    'exists': agent_info.base_path.exists(),
                })
            print(json.dumps(data, indent=2))
        else:
            found = []
            missing = []
            for name, agent_info in AGENTS.items():
                display = agent_info.display_name or name
                entry = (display, agent_info.description, str(agent_info.base_path), agent_info.storage_format)
                if agent_info.base_path.exists():
                    found.append(entry)
                else:
                    missing.append(entry)

            all_entries = found + missing
            if all_entries:
                w_name = max(len(r[0]) for r in all_entries)
                w_desc = max(len(r[1]) for r in all_entries)

            if found:
                print("Found:")
                for name, desc, path, fmt in found:
                    print(f"  {name:<{w_name}}  {desc:<{w_desc}}  [{fmt}]")

            if missing:
                if found:
                    print()
                print("Not Found:")
                for name, desc, path, fmt in missing:
                    print(f"  {name:<{w_name}}  {desc:<{w_desc}}  {path}")
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
            
            _print_sessions(sessions, agent_name if recursive else None, by_time, by_size, reverse, formatter)
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
                rows = []
                for agent_name, s in all_sessions:
                    ctime = format_datetime(s.ctime)
                    mtime = format_datetime(s.mtime)
                    size = format_size(s.size)
                    msgs = str(s.message_count) if s.message_count else "-"
                    rows.append((f"{agent_name}/{s.id}", s.name, ctime, mtime, size, msgs))
                
                w_id = max(len(r[0]) for r in rows)
                w_name = max(len(r[1]) for r in rows)
                w_ctime = max(len(r[2]) for r in rows)
                w_mtime = max(len(r[3]) for r in rows)
                w_size = max(len(r[4]) for r in rows)
                w_msgs = max(len(r[5]) for r in rows)
                
                for id, name, ctime, mtime, size, msgs in rows:
                    print(f"  {id:<{w_id}}  {name:<{w_name}}  {ctime:<{w_ctime}}  {mtime:<{w_mtime}}  {size:>{w_size}}  {msgs:>{w_msgs}}")
                
                print(f"\n  {len(rows)} session(s)")
        else:
            pass  # handled above


if __name__ == "__main__":
    app()
