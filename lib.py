#!/usr/bin/env python3
"""
lib - Shared types and formatters for cdir and cgrep.

Provides dataclasses, agent registry, and output formatters
(json, xml, md) for both tools.
"""

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from xml.dom import minidom


# --- Dataclasses ---

@dataclass
class Session:
    """Represents a conversation session from any agent."""
    id: str
    name: str
    ctime: Optional[datetime]
    mtime: Optional[datetime]
    size: int  # in bytes or message count
    path: Optional[str] = None
    model: Optional[str] = None
    message_count: Optional[int] = None


@dataclass
class Agent:
    """Represents an LLM agent with its storage configuration."""
    name: str
    description: str
    base_path: Path
    storage_format: str  # 'json', 'sqlite', 'jsonl'
    session_pattern: Optional[str] = None  # glob pattern for session files


@dataclass
class Match:
    """A single grep match."""
    session_id: str
    agent: str
    line_num: int
    line: str
    context_before: Optional[List[str]] = None
    context_after: Optional[List[str]] = None


@dataclass
class Message:
    """A single conversation message."""
    role: str  # 'user', 'assistant', 'system'
    content: str


# --- Agent Registry ---

AGENTS: Dict[str, Agent] = {
    'claude': Agent(
        name='claude',
        description='Claude Desktop (Anthropic)',
        base_path=Path.home() / 'Library/Application Support/Claude-3p',
        storage_format='json',
        session_pattern='local-agent-mode-sessions/**/*.json'
    ),
    'claude-code': Agent(
        name='claude-code',
        description='Claude Code CLI',
        base_path=Path.home() / '.claude',
        storage_format='jsonl',
        session_pattern='projects/**/*.jsonl'
    ),
    'opencode': Agent(
        name='opencode',
        description='opencode CLI',
        base_path=Path.home() / '.local/share/opencode',
        storage_format='sqlite'
    ),
    'codex': Agent(
        name='codex',
        description='OpenAI Codex CLI',
        base_path=Path.home() / '.codex',
        storage_format='jsonl',
        session_pattern='sessions/**/*.jsonl'
    ),
}


# --- Helper Functions ---

def format_size(size: int) -> str:
    """Format size in human-readable format."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"


def format_datetime(dt: Optional[datetime]) -> str:
    """Format datetime for display."""
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M")


def _escape_xml(text: str) -> str:
    """Escape text for XML content."""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;').replace("'", '&apos;')


# --- Output Formatters ---

class OutputFormatter:
    """Base class for output formatters."""
    
    def format_sessions(self, sessions: List[Session], agent_name: Optional[str] = None) -> str:
        """Format a list of sessions."""
        raise NotImplementedError
    
    def format_session_export(self, messages: List[Message], session_id: str, agent_name: str) -> str:
        """Format exported session messages."""
        raise NotImplementedError
    
    def format_matches(self, matches: List[Match]) -> str:
        """Format grep matches."""
        raise NotImplementedError
    
    def format_match_files(self, files: List[str], has_matches: bool = True) -> str:
        """Format file list (with or without matches)."""
        raise NotImplementedError
    
    def format_match_counts(self, counts: Dict[str, int]) -> str:
        """Format match counts per file."""
        raise NotImplementedError


class JsonFormatter(OutputFormatter):
    """JSON output format."""
    
    def format_sessions(self, sessions: List[Session], agent_name: Optional[str] = None) -> str:
        data = []
        for s in sessions:
            data.append({
                'id': s.id,
                'name': s.name,
                'ctime': s.ctime.isoformat() if s.ctime else None,
                'mtime': s.mtime.isoformat() if s.mtime else None,
                'size': s.size,
                'path': s.path,
                'model': s.model,
                'message_count': s.message_count,
            })
        return json.dumps(data, indent=2)
    
    def format_session_export(self, messages: List[Message], session_id: str, agent_name: str) -> str:
        data = [{'role': m.role, 'content': m.content} for m in messages]
        return json.dumps(data, indent=2)
    
    def format_matches(self, matches: List[Match]) -> str:
        data = []
        for m in matches:
            item = {
                'session': f"{m.agent}/{m.session_id}",
                'line_num': m.line_num,
                'line': m.line,
            }
            if m.context_before:
                item['context_before'] = m.context_before
            if m.context_after:
                item['context_after'] = m.context_after
            data.append(item)
        return json.dumps(data, indent=2)
    
    def format_match_files(self, files: List[str], has_matches: bool = True) -> str:
        return json.dumps(files, indent=2)
    
    def format_match_counts(self, counts: Dict[str, int]) -> str:
        return json.dumps(counts, indent=2)


class XmlFormatter(OutputFormatter):
    """XML output format (conversation-centric)."""
    
    def format_sessions(self, sessions: List[Session], agent_name: Optional[str] = None) -> str:
        root = ET.Element('sessions')
        if agent_name:
            root.set('agent', agent_name)
        
        for s in sessions:
            elem = ET.SubElement(root, 'session')
            elem.set('id', s.id)
            elem.set('name', s.name)
            if s.ctime:
                elem.set('ctime', s.ctime.isoformat())
            if s.mtime:
                elem.set('mtime', s.mtime.isoformat())
            elem.set('size', str(s.size))
            if s.model:
                elem.set('model', s.model)
            if s.message_count is not None:
                elem.set('messages', str(s.message_count))
        
        return minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
    
    def format_session_export(self, messages: List[Message], session_id: str, agent_name: str) -> str:
        root = ET.Element('session')
        root.set('id', session_id)
        root.set('agent', agent_name)
        
        for m in messages:
            elem = ET.SubElement(root, 'message')
            elem.set('role', m.role)
            elem.text = m.content
        
        return minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
    
    def format_matches(self, matches: List[Match]) -> str:
        root = ET.Element('matches')
        
        current_session = None
        session_elem = None
        
        for m in matches:
            session_key = f"{m.agent}/{m.session_id}"
            if session_key != current_session:
                session_elem = ET.SubElement(root, 'session')
                session_elem.set('id', m.session_id)
                session_elem.set('agent', m.agent)
                current_session = session_key
            
            match_elem = ET.SubElement(session_elem, 'match')
            match_elem.set('line', str(m.line_num))
            
            if m.context_before:
                ctx_before = ET.SubElement(match_elem, 'context_before')
                for line in m.context_before:
                    ctx_line = ET.SubElement(ctx_before, 'line')
                    ctx_line.text = line
            
            msg_elem = ET.SubElement(match_elem, 'message')
            msg_elem.text = m.line
            
            if m.context_after:
                ctx_after = ET.SubElement(match_elem, 'context_after')
                for line in m.context_after:
                    ctx_line = ET.SubElement(ctx_after, 'line')
                    ctx_line.text = line
        
        return minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
    
    def format_match_files(self, files: List[str], has_matches: bool = True) -> str:
        root = ET.Element('files')
        root.set('match', 'true' if has_matches else 'false')
        
        for f in files:
            elem = ET.SubElement(root, 'file')
            # Split "agent/session_id"
            parts = f.split('/', 1)
            elem.set('agent', parts[0] if parts else '')
            elem.set('session', parts[1] if len(parts) > 1 else '')
            elem.text = f
        
        return minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
    
    def format_match_counts(self, counts: Dict[str, int]) -> str:
        root = ET.Element('counts')
        
        for path, count in counts.items():
            elem = ET.SubElement(root, 'count')
            parts = path.split('/', 1)
            elem.set('agent', parts[0] if parts else '')
            elem.set('session', parts[1] if len(parts) > 1 else '')
            elem.set('matches', str(count))
            elem.text = path
        
        return minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")


class MarkdownFormatter(OutputFormatter):
    """Markdown output format (conversation-style)."""
    
    def format_sessions(self, sessions: List[Session], agent_name: Optional[str] = None) -> str:
        lines = []
        
        if agent_name:
            lines.append(f"# Sessions — {agent_name}")
        else:
            lines.append("# Sessions")
        lines.append("")
        
        if not sessions:
            lines.append("*No sessions found*")
            return '\n'.join(lines)
        
        # Table header
        lines.append("| ID | Name | Modified | Size | Messages |")
        lines.append("|---|---|---|---|---|")
        
        for s in sessions:
            mtime = format_datetime(s.mtime)
            size = format_size(s.size)
            msgs = str(s.message_count) if s.message_count else "-"
            name = s.name[:40] + "..." if len(s.name) > 40 else s.name
            lines.append(f"| `{s.id}` | {name} | {mtime} | {size} | {msgs} |")
        
        lines.append("")
        lines.append(f"*{len(sessions)} session(s)*")
        
        return '\n'.join(lines)
    
    def format_session_export(self, messages: List[Message], session_id: str, agent_name: str) -> str:
        lines = []
        lines.append(f"# Session — {agent_name}/{session_id}")
        lines.append("")
        
        for m in messages:
            if m.role == 'user':
                lines.append(f"## You")
            elif m.role == 'assistant':
                lines.append(f"## Assistant")
            else:
                lines.append(f"## {m.role.title()}")
            lines.append("")
            lines.append(m.content)
            lines.append("")
        
        return '\n'.join(lines)
    
    def format_matches(self, matches: List[Match]) -> str:
        lines = []
        
        current_session = None
        for m in matches:
            session_key = f"{m.agent}/{m.session_id}"
            if session_key != current_session:
                if current_session is not None:
                    lines.append("")
                lines.append(f"### {session_key}")
                lines.append("")
                current_session = session_key
            
            if m.context_before:
                for line in m.context_before:
                    lines.append(f"  {line}")
            
            lines.append(f"**{m.line_num}:** {m.line}")
            
            if m.context_after:
                for line in m.context_after:
                    lines.append(f"  {line}")
            
            lines.append("")
        
        return '\n'.join(lines)
    
    def format_match_files(self, files: List[str], has_matches: bool = True) -> str:
        lines = []
        label = "Files with matches" if has_matches else "Files without matches"
        lines.append(f"# {label}")
        lines.append("")
        
        for f in files:
            lines.append(f"- `{f}`")
        
        return '\n'.join(lines)
    
    def format_match_counts(self, counts: Dict[str, int]) -> str:
        lines = []
        lines.append("# Match Counts")
        lines.append("")
        
        for path, count in sorted(counts.items()):
            lines.append(f"- `{path}`: {count}")
        
        return '\n'.join(lines)


# --- Formatter Factory ---

def get_formatter(fmt: str) -> OutputFormatter:
    """Get output formatter by name."""
    formatters = {
        'json': JsonFormatter,
        'xml': XmlFormatter,
        'md': MarkdownFormatter,
    }
    
    if fmt not in formatters:
        raise ValueError(f"Unknown format: {fmt}. Available: {', '.join(formatters.keys())}")
    
    return formatters[fmt]()
