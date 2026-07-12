#!/usr/bin/env python3
"""
cgrep - grep for LLM context windows

Search through agent session content using PCRE patterns.

Usage:
    cgrep -r "pattern" "opencode/*"
    cgrep -l "pattern" "opencode/ses_abc123"
    cgrep -c "pattern" "opencode/*" "claude-code/*"
"""

import re
import sys
import json
import sqlite3
import fnmatch
import typer
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from rich.console import Console

from ctools.lib import (
    Match, Agent, AGENTS, Message,
    get_formatter, JsonFormatter, XmlFormatter, MarkdownFormatter
)

# Re-export for backward compatibility
__all__ = ['app', 'parse_path_pattern', 'get_sessions_for_pattern', 'grep_session', 
           'get_opencode_session_content', 'get_claude_code_session_content', 'get_claude_session_content']

app = typer.Typer()
console = Console()


def get_opencode_session_content(agent_path: Path, session_id: str) -> List[Tuple[int, str]]:
    """Extract message content from opencode session, returns (line_num, text) pairs."""
    db_path = agent_path / 'opencode.db'
    if not db_path.exists():
        return []
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # Get messages for this session
    cursor.execute('''
        SELECT m.id, m.data
        FROM message m
        WHERE m.session_id = ?
        ORDER BY m.time_created
    ''', (session_id,))
    
    lines = []
    line_num = 1
    for msg_id, msg_data in cursor.fetchall():
        data = json.loads(msg_data)
        role = data.get('role', '')
        
        # Get parts for this message
        cursor.execute('''
            SELECT data FROM part
            WHERE message_id = ?
            ORDER BY time_created
        ''', (msg_id,))
        
        for (part_data,) in cursor.fetchall():
            part = json.loads(part_data)
            if part.get('type') == 'text':
                text = part.get('text', '')
                if text:
                    for line in text.split('\n'):
                        if line.strip():
                            lines.append((line_num, f"{role}: {line}"))
                            line_num += 1
    
    conn.close()
    return lines


def get_claude_code_session_content(agent_path: Path, session_id: str) -> List[Tuple[int, str]]:
    """Extract message content from claude-code session."""
    for session_file in agent_path.glob('projects/**/*.jsonl'):
        if session_file.stem == session_id:
            lines = []
            line_num = 1
            with open(session_file, 'r') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        msg_type = data.get('type', '')
                        content = ''
                        if msg_type == 'human':
                            content = data.get('message', {}).get('content', '')
                            role = 'user'
                        elif msg_type == 'assistant':
                            content = data.get('message', {}).get('content', '')
                            role = 'assistant'
                        
                        if content:
                            for text_line in content.split('\n'):
                                if text_line.strip():
                                    lines.append((line_num, f"{role}: {text_line}"))
                                    line_num += 1
                    except json.JSONDecodeError:
                        continue
            return lines
    return []


def get_claude_session_content(agent_path: Path, session_id: str) -> List[Tuple[int, str]]:
    """Extract message content from claude-desktop session."""
    for session_file in agent_path.glob('local-agent-mode-sessions/**/*.json'):
        if session_file.stem == session_id:
            try:
                with open(session_file, 'r') as f:
                    data = json.load(f)
                
                messages = data if isinstance(data, list) else data.get('messages', [])
                lines = []
                line_num = 1
                for msg in messages:
                    role = msg.get('role', '')
                    content = msg.get('content', '')
                    if isinstance(content, str) and content:
                        for text_line in content.split('\n'):
                            if text_line.strip():
                                lines.append((line_num, f"{role}: {text_line}"))
                                line_num += 1
                return lines
            except (json.JSONDecodeError, KeyError):
                pass
    return []


CONTENT_EXTRACTORS = {
    'opencode': get_opencode_session_content,
    'claude-code': get_claude_code_session_content,
    'claude': get_claude_session_content,
}


def parse_path_pattern(pattern: str) -> List[Tuple[str, Optional[str]]]:
    """Parse path pattern like 'opencode/*' or 'opencode/ses_abc123'.
    
    Returns list of (agent_name, session_id_or_none) tuples.
    """
    results = []
    
    # Handle multiple patterns
    for pat in pattern.split():
        pat = pat.strip('/')
        parts = pat.split('/', 1)
        agent_name = parts[0]
        session_pat = parts[1] if len(parts) > 1 else '*'
        
        if agent_name not in AGENTS:
            console.print(f"[red]Unknown agent: {agent_name}[/red]")
            continue
        
        results.append((agent_name, session_pat))
    
    return results


def get_sessions_for_pattern(agent_name: str, session_pat: str) -> List[str]:
    """Get session IDs matching a pattern for an agent."""
    from ctools.cdir import SESSION_EXTRACTORS
    
    agent_info = AGENTS.get(agent_name)
    if not agent_info or not agent_info.base_path.exists():
        return []
    
    extractor = SESSION_EXTRACTORS.get(agent_name)
    if not extractor:
        return []
    
    sessions = extractor(agent_info)
    matched = []
    for s in sessions:
        if fnmatch.fnmatch(s.id, session_pat):
            matched.append(s.id)
    
    return matched


def grep_session(agent_name: str, session_id: str, pattern: re.Pattern,
                 invert: bool = False, before: int = 0, after: int = 0) -> List[Match]:
    """Search a session for pattern matches."""
    agent_info = AGENTS.get(agent_name)
    if not agent_info or not agent_info.base_path.exists():
        return []
    
    extractor = CONTENT_EXTRACTORS.get(agent_name)
    if not extractor:
        return []
    
    lines = extractor(agent_info.base_path, session_id)
    if not lines:
        return []
    
    matches = []
    for i, (line_num, line) in enumerate(lines):
        found = bool(pattern.search(line))
        if invert:
            found = not found
        
        if found:
            # Get context lines
            ctx_before = []
            ctx_after = []
            
            if before > 0:
                start = max(0, i - before)
                ctx_before = [lines[j][1] for j in range(start, i)]
            
            if after > 0:
                end = min(len(lines), i + after + 1)
                ctx_after = [lines[j][1] for j in range(i + 1, end)]
            
            matches.append(Match(
                session_id=session_id,
                agent=agent_name,
                line_num=line_num,
                line=line,
                context_before=ctx_before if before > 0 else None,
                context_after=ctx_after if after > 0 else None
            ))
    
    return matches


@app.command()
def main(
    pattern: str = typer.Argument(..., help="PCRE search pattern"),
    paths: List[str] = typer.Argument(..., help="Agent/session paths (e.g., opencode/*)"),
    list_files: bool = typer.Option(False, "--files-with-matches", "-l", help="Show only session IDs with matches"),
    list_files_neg: bool = typer.Option(False, "--files-without-match", "-L", help="Show only session IDs without matches"),
    count: bool = typer.Option(False, "--count", "-c", help="Show match count per session"),
    invert: bool = typer.Option(False, "--invert-match", "-v", help="Invert match"),
    before: int = typer.Option(0, "--before", "-B", help="Show N lines before match"),
    after: int = typer.Option(0, "--after", "-A", help="Show N lines after match"),
    context: int = typer.Option(0, "--context", "-C", help="Show N lines before and after match"),
    ignore_case: bool = typer.Option(False, "--ignore-case", "-i", help="Ignore case"),
    fmt: str = typer.Option("default", "--format", "-f", help="Output format: json, xml, md, or default"),
):
    """
    Search through agent session content.
    
    Patterns are PCRE. Paths specify agents and optionally session IDs.
    
    Examples:
        cgrep "error" "opencode/*"
        cgrep -l "TODO" "opencode/*" "claude-code/*"
        cgrep -c "import" "opencode/*"
        cgrep -B2 -A2 "FIXME" "opencode/ses_abc123"
    """
    # Build regex
    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        console.print(f"[red]Invalid pattern: {e}[/red]")
        raise typer.Exit(1)
    
    # Apply -C to both before and after
    if context > 0:
        before = context
        after = context
    
    # Get formatter if specified
    formatter = None
    if fmt != "default":
        try:
            formatter = get_formatter(fmt)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    
    # Parse path patterns
    path_list = parse_path_pattern(' '.join(paths))
    
    all_matches = []
    sessions_with_matches = set()
    sessions_without_matches = set()
    session_counts = {}
    
    for agent_name, session_pat in path_list:
        session_ids = get_sessions_for_pattern(agent_name, session_pat)
        
        for session_id in session_ids:
            matches = grep_session(agent_name, session_id, compiled,
                                   invert=invert, before=before, after=after)
            
            if matches:
                sessions_with_matches.add(f"{agent_name}/{session_id}")
                session_counts[f"{agent_name}/{session_id}"] = len(matches)
                all_matches.extend(matches)
            else:
                sessions_without_matches.add(f"{agent_name}/{session_id}")
    
    # Output based on flags
    if list_files:
        files = sorted(sessions_with_matches)
        if formatter:
            print(formatter.format_match_files(files, has_matches=True))
        else:
            for path in files:
                print(path)
    elif list_files_neg:
        files = sorted(sessions_without_matches)
        if formatter:
            print(formatter.format_match_files(files, has_matches=False))
        else:
            for path in files:
                print(path)
    elif count:
        if formatter:
            print(formatter.format_match_counts(session_counts))
        else:
            for path, cnt in sorted(session_counts.items()):
                print(f"{path}:{cnt}")
    else:
        # Print matches with context
        if formatter:
            print(formatter.format_matches(all_matches))
        else:
            current_session = None
            for m in all_matches:
                path = f"{m.agent}/{m.session_id}"
                
                if path != current_session:
                    if current_session is not None:
                        print("--")
                    current_session = path
                
                # Print context before
                if m.context_before:
                    for line in m.context_before:
                        print(f"  {line}")
                
                # Print the matching line
                print(f"{m.line_num}:{m.line}")
                
                # Print context after
                if m.context_after:
                    for line in m.context_after:
                        print(f"  {line}")
            
            if not all_matches:
                console.print("[dim]No matches found[/dim]")


if __name__ == "__main__":
    app()
