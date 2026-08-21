#!/usr/bin/env python3
"""
agents - one class per LLM agent backend.

Every ctools command talks to an agent through the interface defined on
:class:`Agent`:

    discovery   sessions()
    reading     messages()   raw_messages()   lines()
    mutation    inject_system()   inject_toolcall()   remove_messages()

Storage shape lives in the intermediate classes (:class:`JsonAgent`,
:class:`JsonlAgent`, :class:`SqliteAgent`); a concrete agent overrides only
what is genuinely peculiar to it.  Supporting a new agent is one subclass
plus one line in ``REGISTRY`` -- no command needs to change.
"""

import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple


# --- Domain model ---

@dataclass
class Session:
    """A conversation session from any agent."""
    id: str
    name: str
    ctime: Optional[datetime]
    mtime: Optional[datetime]
    size: int  # tokens where the agent tracks them, bytes otherwise
    path: Optional[str] = None
    model: Optional[str] = None
    message_count: Optional[int] = None
    parent_id: Optional[str] = None


@dataclass
class Message:
    """A single conversation message."""
    role: str  # 'user', 'assistant', 'system', 'tool'
    content: str


@dataclass
class Match:
    """A single grep match within a session."""
    session_id: str
    agent: str
    line_num: int
    line: str
    context_before: Optional[List[str]] = None
    context_after: Optional[List[str]] = None


# --- Errors ---

class AgentError(Exception):
    """Base class for agent-layer failures."""


class SessionNotFound(AgentError):
    """The requested session does not exist for this agent."""

    def __init__(self, agent: str, session_id: str):
        self.agent = agent
        self.session_id = session_id
        super().__init__(f"Session not found: {agent}/{session_id}")


class UnsupportedOperation(AgentError):
    """This agent's storage does not support the requested operation."""

    def __init__(self, agent: str, operation: str, detail: str = ''):
        self.agent = agent
        self.operation = operation
        msg = f"{agent} does not support {operation}"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


# --- Shared parsing helpers ---

CONVERSATION_ROLES = ('user', 'assistant')

_NANOS = re.compile(r'^(.*\.\d{6})\d+(.*)$')


def text_of(content) -> str:
    """Flatten a message content value to plain text.

    Accepts a bare string, a single block, or a list of blocks.  Understands
    the ``{"type": "text", "text": ...}`` blocks used by Claude, pi and
    current goose, the ``{"type": "input_text"|"output_text"}`` blocks used
    by codex, and goose's legacy externally-tagged ``{"Text": {...}}`` form.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ''
    parts = []
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get('type') in ('text', 'input_text', 'output_text'):
            text = block.get('text', '')
        elif 'Text' in block:
            inner = block['Text']
            text = inner.get('text', '') if isinstance(inner, dict) else ''
        else:
            continue
        if text:
            parts.append(text)
    return '\n'.join(parts)


def parse_timestamp(ts) -> Optional[datetime]:
    """Parse an ISO-8601 / RFC-3339 timestamp into a naive local datetime.

    Tolerates a trailing ``Z`` and sub-microsecond precision (goose writes
    nanoseconds, which ``datetime.fromisoformat`` rejects).
    """
    if not ts:
        return None
    try:
        s = str(ts).replace('Z', '+00:00')
        trimmed = _NANOS.match(s)
        if trimmed:
            s = trimmed.group(1) + trimmed.group(2)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def epoch_ms(ms) -> Optional[datetime]:
    """Convert milliseconds-since-epoch to a naive local datetime."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000)
    except (ValueError, TypeError, OSError):
        return None


def file_metadata(path: Path) -> Tuple[datetime, datetime, int]:
    """Return (ctime, mtime, size) for a file."""
    st = path.stat()
    return (datetime.fromtimestamp(st.st_ctime),
            datetime.fromtimestamp(st.st_mtime),
            st.st_size)


def read_json_lines(path: Path) -> Iterator[dict]:
    """Yield each parseable JSON object from a JSONL file, skipping junk."""
    try:
        with open(path, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


# --- Base agent ---

class Agent:
    """Interface every ctools command programs against.

    Subclasses declare their identity as class attributes and implement
    :meth:`sessions` and :meth:`messages`.  Everything else has a working
    default: :meth:`raw_messages` falls back to the conversation,
    :meth:`lines` is derived from it, and the mutators refuse politely
    rather than corrupting a store they do not understand.
    """

    name: str = ''
    description: str = ''
    display_name: Optional[str] = None
    storage_format: str = ''      # 'json' | 'jsonl' | 'sqlite' -- descriptive only
    session_pattern: Optional[str] = None
    files_read: Optional[str] = None   # what we actually read, relative to base_path

    def __init__(self, base_path=None):
        self.base_path = Path(base_path) if base_path is not None else self.default_base_path()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_path={str(self.base_path)!r})"

    @classmethod
    def default_base_path(cls) -> Path:
        raise NotImplementedError

    # --- location ---

    @property
    def label(self) -> str:
        """Human-facing name."""
        return self.display_name or self.name

    @property
    def source(self) -> Path:
        """The path we actually read sessions out of."""
        return self.base_path / self.files_read if self.files_read else self.base_path

    def exists(self) -> bool:
        return self.base_path.exists()

    # --- discovery ---

    def sessions(self) -> List[Session]:
        """All sessions this agent has stored."""
        raise NotImplementedError

    def session(self, session_id: str) -> Optional[Session]:
        """One session by id, or None."""
        for s in self.sessions():
            if s.id == session_id:
                return s
        return None

    # --- reading ---

    def messages(self, session_id: str) -> List[Message]:
        """The conversation: user and assistant turns, in order."""
        raise NotImplementedError

    def raw_messages(self, session_id: str) -> List[Message]:
        """Every message including system and tool turns.

        Defaults to the conversation for agents that keep nothing else.
        """
        return self.messages(session_id)

    def lines(self, session_id: str) -> List[Tuple[int, str]]:
        """Searchable ``(line_num, "role: text")`` pairs for grep."""
        out = []
        for message in self.messages(session_id):
            for line in message.content.split('\n'):
                if line.strip():
                    out.append((len(out) + 1, f"{message.role}: {line}"))
        return out

    def token_usage(self, session_id: str) -> Optional[Dict[str, int]]:
        """Token counts the agent recorded itself, or None if it keeps none.

        Callers fall back to estimating from message text.
        """
        return None

    # --- mutation ---

    def inject_system(self, session_id: str, content: str) -> None:
        """Replace (or add) this session's system message."""
        raise UnsupportedOperation(self.name, 'system message injection')

    def inject_toolcall(self, session_id: str, content: str,
                        tool_name: str = 'context_from_source') -> None:
        """Replace (or add) a synthetic tool message carrying `content`."""
        raise UnsupportedOperation(self.name, 'toolcall injection')

    def remove_messages(self, session_id: str, indices: List[int]) -> int:
        """Delete the messages at `indices` (as numbered by `raw_messages`).

        Returns how many were removed.
        """
        raise UnsupportedOperation(self.name, 'message removal')


# --- Storage shapes ---

class FileAgent(Agent):
    """An agent that keeps one file per session under `session_pattern`."""

    def session_files(self) -> List[Path]:
        if not self.base_path.exists() or not self.session_pattern:
            return []
        return sorted(self.base_path.glob(self.session_pattern))

    def session_file(self, session_id: str) -> Optional[Path]:
        """Locate a session's file. Matched on filename stem by default."""
        for path in self.session_files():
            if path.stem == session_id:
                return path
        return None

    def require_file(self, session_id: str) -> Path:
        path = self.session_file(session_id)
        if path is None:
            raise SessionNotFound(self.name, session_id)
        return path


class JsonAgent(FileAgent):
    """One JSON document per session: a message list, or ``{"messages": [...]}``."""

    storage_format = 'json'

    def load(self, path: Path) -> Optional[dict]:
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def message_list(data) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get('messages', []) or []
        return []

    def messages(self, session_id: str) -> List[Message]:
        data = self.load(self.require_file(session_id))
        out = []
        for raw in self.message_list(data):
            if not isinstance(raw, dict):
                continue
            content = text_of(raw.get('content', ''))
            if content:
                out.append(Message(role=raw.get('role', ''), content=content))
        return out

    def _rewrite(self, session_id: str, mutate) -> None:
        """Load, hand the message list to `mutate`, write back in place."""
        path = self.require_file(session_id)
        data = self.load(path)
        if data is None:
            raise SessionNotFound(self.name, session_id)
        messages = self.message_list(data)
        mutate(messages)
        if isinstance(data, list):
            out = messages
        else:
            data['messages'] = messages
            out = data
        with open(path, 'w') as f:
            json.dump(out, f, indent=2)
            f.write('\n')

    def inject_system(self, session_id: str, content: str) -> None:
        def mutate(messages):
            for msg in messages:
                if isinstance(msg, dict) and msg.get('role') == 'system':
                    msg['content'] = content
                    return
            messages.insert(0, {'role': 'system', 'content': content})
        self._rewrite(session_id, mutate)

    def inject_toolcall(self, session_id: str, content: str,
                        tool_name: str = 'context_from_source') -> None:
        def mutate(messages):
            for msg in messages:
                if isinstance(msg, dict) and msg.get('name') == tool_name:
                    msg['content'] = content
                    return
            messages.append({'role': 'tool', 'name': tool_name, 'content': content})
        self._rewrite(session_id, mutate)

    def remove_messages(self, session_id: str, indices: List[int]) -> int:
        drop = set(indices)
        removed = 0

        def mutate(messages):
            nonlocal removed
            kept = [m for i, m in enumerate(messages) if i not in drop]
            removed = len(messages) - len(kept)
            messages[:] = kept
        self._rewrite(session_id, mutate)
        return removed


class JsonlAgent(FileAgent):
    """One JSONL file per session: a stream of typed entries, one per line.

    Entries carrying conversation are recognised by :meth:`entry_message`;
    everything else (headers, tool events, metadata) is passed through
    untouched by the mutators, so rewriting a session never disturbs the
    parts we do not model.
    """

    storage_format = 'jsonl'

    def entry_message(self, entry: dict) -> Optional[Message]:
        """Return the Message an entry carries, or None if it carries none."""
        message = entry.get('message')
        if not isinstance(message, dict):
            return None
        content = text_of(message.get('content', ''))
        if not content:
            return None
        role = message.get('role') or entry.get('type', '')
        return Message(role=role, content=content)

    def raw_messages(self, session_id: str) -> List[Message]:
        path = self.require_file(session_id)
        out = []
        for entry in read_json_lines(path):
            message = self.entry_message(entry)
            if message is not None:
                out.append(message)
        return out

    def messages(self, session_id: str) -> List[Message]:
        return [m for m in self.raw_messages(session_id) if m.role in CONVERSATION_ROLES]

    def _rewrite(self, session_id: str, mutate) -> None:
        """Hand `mutate` the file's lines as parsed entries; write back."""
        path = self.require_file(session_id)
        lines = path.read_text().splitlines()
        out = mutate(lines)
        path.write_text('\n'.join(out) + '\n' if out else '')  # empty file, not a bare newline

    def _inject_matching(self, session_id: str, content: str, matches, build,
                         at_head: bool = False) -> None:
        """Replace the first line satisfying `matches`, else insert `build()`.

        `at_head` decides where a new entry goes when nothing matched: a
        system prompt belongs at the top of a transcript, a tool result at
        the bottom.
        """
        def mutate(lines):
            new_lines = []
            replaced = False
            for line in lines:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    new_lines.append(line)
                    continue
                if not replaced and isinstance(entry, dict) and matches(entry):
                    new_lines.append(json.dumps(self._set_content(entry, content)))
                    replaced = True
                else:
                    new_lines.append(line)
            if not replaced:
                built = json.dumps(build(content))
                if at_head:
                    new_lines.insert(0, built)
                else:
                    new_lines.append(built)
            return new_lines
        self._rewrite(session_id, mutate)

    @staticmethod
    def _set_content(entry: dict, content: str) -> dict:
        """Write `content` back into whichever slot the entry keeps it in."""
        message = entry.get('message')
        if isinstance(message, dict):
            message['content'] = content
        else:
            entry['content'] = content
        return entry

    @staticmethod
    def _is_system(entry: dict) -> bool:
        message = entry.get('message')
        role = message.get('role') if isinstance(message, dict) else None
        return entry.get('type') == 'system' or role == 'system'

    def inject_system(self, session_id: str, content: str) -> None:
        self._inject_matching(
            session_id, content,
            matches=self._is_system,
            build=lambda text: {'type': 'system',
                                'message': {'role': 'system', 'content': text}},
            at_head=True,
        )

    def inject_toolcall(self, session_id: str, content: str,
                        tool_name: str = 'context_from_source') -> None:
        self._inject_matching(
            session_id, content,
            matches=lambda entry: entry.get('name') == tool_name,
            build=lambda text: {'role': 'tool', 'name': tool_name, 'content': text},
        )

    def remove_messages(self, session_id: str, indices: List[int]) -> int:
        """Drop the Nth *message* entries -- not the Nth lines.

        Non-message lines (session headers, tool events) are not numbered and
        never removed, so the indices line up with :meth:`raw_messages`.
        """
        drop = set(indices)
        removed = 0

        def mutate(lines):
            nonlocal removed
            new_lines = []
            seen = 0
            for line in lines:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    new_lines.append(line)
                    continue
                if isinstance(entry, dict) and self.entry_message(entry) is not None:
                    if seen in drop:
                        seen += 1
                        removed += 1
                        continue
                    seen += 1
                new_lines.append(line)
            return new_lines
        self._rewrite(session_id, mutate)
        return removed


class SqliteAgent(Agent):
    """An agent backed by a SQLite database.

    Deliberately says nothing about schema -- ``storage_format == 'sqlite'``
    is not a contract, it is a coincidence between opencode and goose whose
    tables have nothing in common.
    """

    storage_format = 'sqlite'
    db_name: str = ''

    @property
    def db_path(self) -> Path:
        return self.base_path / self.db_name

    def connect(self) -> Optional[sqlite3.Connection]:
        if not self.db_path.exists():
            return None
        try:
            return sqlite3.connect(str(self.db_path))
        except sqlite3.Error:
            return None


# --- Concrete agents ---

class ClaudeDesktopAgent(JsonAgent):
    """Claude Desktop: one JSON file per local-agent-mode session."""

    name = 'claude'
    description = 'Claude Desktop'
    display_name = 'Claude'
    session_pattern = 'local-agent-mode-sessions/**/*.json'
    files_read = 'conversations/'

    # Config documents that live alongside sessions but are not sessions.
    SKIP_FILES = {'manifest.json', 'plugin.json', 'scheduled-tasks.json',
                  'remote-session-spaces.json', 'ant-device-registry.json'}

    @classmethod
    def default_base_path(cls) -> Path:
        if sys.platform == 'darwin':
            return Path.home() / 'Library/Application Support/Claude'
        if sys.platform == 'win32':
            return Path(os.environ.get('APPDATA', '')) / 'Claude'
        return Path.home() / '.config/Claude'

    def session_files(self) -> List[Path]:
        return [p for p in super().session_files()
                if p.name not in self.SKIP_FILES and '.claude-plugin' not in p.parts]

    def sessions(self) -> List[Session]:
        sessions = []
        for path in self.session_files():
            try:
                ctime, mtime, size = file_metadata(path)
            except OSError:
                continue
            data = self.load(path)
            if data is None:
                continue
            session_id = path.stem
            name = data.get('name') if isinstance(data, dict) else None
            sessions.append(Session(
                id=session_id,
                name=name or session_id[:8],
                ctime=ctime,
                mtime=mtime,
                size=size,
                path=str(path),
            ))
        return sessions


class ClaudeCodeAgent(JsonlAgent):
    """Claude Code CLI: one JSONL transcript per session under projects/."""

    name = 'claude-code'
    description = 'Claude Code CLI'
    display_name = 'Claude Code'
    session_pattern = 'projects/**/*.jsonl'
    files_read = 'projects/'

    # Entry types that carry a conversation turn. 'human' is the historical
    # spelling; current versions write 'user'.
    ROLE_TYPES = {'human': 'user', 'user': 'user', 'assistant': 'assistant',
                  'system': 'system'}

    @classmethod
    def default_base_path(cls) -> Path:
        return Path.home() / '.claude'

    def entry_message(self, entry: dict) -> Optional[Message]:
        role = self.ROLE_TYPES.get(entry.get('type', ''))
        if role is None:
            return None
        message = entry.get('message')
        content = text_of(message.get('content', '')) if isinstance(message, dict) else ''
        if not content:
            return None
        return Message(role=role, content=content)

    def sessions(self) -> List[Session]:
        sessions = []
        for path in self.session_files():
            try:
                ctime, mtime, size = file_metadata(path)
            except OSError:
                continue
            session_id = path.stem
            title = None
            first_user = None
            model = None
            line_count = 0
            for entry in read_json_lines(path):
                line_count += 1
                etype = entry.get('type')
                if etype == 'ai-title':
                    title = entry.get('aiTitle') or title
                elif etype in ('user', 'human') and first_user is None:
                    message = entry.get('message')
                    if isinstance(message, dict):
                        first_user = text_of(message.get('content', ''))
                elif etype == 'assistant':
                    message = entry.get('message')
                    if isinstance(message, dict):
                        model = message.get('model') or model
            name = title or (first_user or '').strip().replace('\n', ' ')[:50] or session_id[:8]
            sessions.append(Session(
                id=session_id,
                name=name,
                ctime=ctime,
                mtime=mtime,
                size=size,
                path=str(path),
                model=model,
                message_count=line_count,
            ))
        return sessions


class CodexAgent(JsonlAgent):
    """OpenAI Codex CLI: JSONL rollout files, optionally indexed in SQLite."""

    name = 'codex'
    description = 'OpenAI Codex CLI'
    display_name = 'Codex'
    session_pattern = 'sessions/**/*.jsonl'
    files_read = 'sessions/'
    index_name = 'state_5.sqlite'

    @classmethod
    def default_base_path(cls) -> Path:
        return Path.home() / '.codex'

    @property
    def index_path(self) -> Path:
        return self.base_path / self.index_name

    def session_file(self, session_id: str) -> Optional[Path]:
        """Match on filename stem, then on the rollout header's session id.

        Codex names rollout files ``rollout-<timestamp>-<uuid>.jsonl`` while
        the index and the header both use the bare uuid.
        """
        for path in self.session_files():
            if path.stem == session_id or session_id in path.name:
                return path
        for path in self.session_files():
            for entry in read_json_lines(path):
                meta = entry.get('session_meta')
                if isinstance(meta, dict) and meta.get('id') == session_id:
                    return path
                break
        return None

    def entry_message(self, entry: dict) -> Optional[Message]:
        """Unwrap a rollout entry.

        Conversation turns arrive as ``{"type": "response_item", "payload":
        {"type": "message", "role": ..., "content": [...]}}``; older rollouts
        inline the same shape at the top level.
        """
        payload = entry.get('payload') if isinstance(entry.get('payload'), dict) else entry
        if payload.get('type') not in (None, 'message'):
            return None
        role = payload.get('role')
        if not role:
            return None
        content = text_of(payload.get('content', ''))
        if not content:
            return None
        return Message(role=role, content=content)

    def _index_sessions(self) -> List[Session]:
        if not self.index_path.exists():
            return []
        try:
            conn = sqlite3.connect(str(self.index_path))
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, title, cwd, model, created_at, updated_at
                FROM sessions
                ORDER BY updated_at DESC
            ''')
            rows = cursor.fetchall()
            conn.close()
        except sqlite3.Error:
            return []
        return [Session(
            id=session_id,
            name=title or session_id[:8],
            ctime=parse_timestamp(created_at),
            mtime=parse_timestamp(updated_at),
            size=0,  # the index does not track size; rollout files do
            path=cwd or str(self.index_path),
            model=model,
        ) for session_id, title, cwd, model, created_at, updated_at in rows]

    def _rollout_sessions(self) -> List[Session]:
        sessions = []
        for path in self.session_files():
            try:
                ctime, mtime, size = file_metadata(path)
            except OSError:
                continue
            session_id = path.stem
            name = session_id[:8]
            for entry in read_json_lines(path):
                meta = entry.get('session_meta')
                if isinstance(meta, dict):
                    session_id = meta.get('id', session_id)
                    name = meta.get('title', name)
                break
            sessions.append(Session(
                id=session_id,
                name=name,
                ctime=ctime,
                mtime=mtime,
                size=size,
                path=str(path),
            ))
        return sessions

    def sessions(self) -> List[Session]:
        if not self.base_path.exists():
            return []
        return self._index_sessions() or self._rollout_sessions()


class PiAgent(JsonlAgent):
    """Pi Coding Agent: a JSONL *tree* per session, not a flat transcript.

    Every entry has an id and a parentId; branches are created by rewinding
    the conversation.  Reading a session means walking the newest leaf back
    to the root, so the flat JSONL readers do not apply.
    """

    name = 'pi'
    description = 'Pi Coding Agent'
    display_name = 'Pi'
    session_pattern = 'sessions/**/*.jsonl'
    files_read = 'sessions/'

    @classmethod
    def default_base_path(cls) -> Path:
        return Path.home() / '.pi' / 'agent'

    def session_file(self, session_id: str) -> Optional[Path]:
        """Locate a session by filename suffix or by header uuid.

        Pi names files ``<timestamp>_<uuid>.jsonl``, so the stem never equals
        the session id.
        """
        if not self.base_path.exists():
            return None
        for path in sorted(self.base_path.glob('**/*.jsonl')):
            if session_id in path.name:
                return path
            for entry in read_json_lines(path):
                if entry.get('type') == 'session' and entry.get('id') == session_id:
                    return path
                break
        return None

    @staticmethod
    def _session_uuid(path_str) -> Optional[str]:
        """Extract a session uuid from a parentSession path or bare uuid."""
        if not path_str:
            return None
        stem = Path(str(path_str)).stem
        return stem.split('_', 1)[1] if '_' in stem else stem

    def raw_messages(self, session_id: str) -> List[Message]:
        """Walk the active branch: newest leaf back to the root."""
        path = self.require_file(session_id)

        by_id = {}
        parents = {}
        children = {}
        last_id = None
        for entry in read_json_lines(path):
            eid = entry.get('id')
            if not eid:
                continue
            by_id[eid] = entry
            parents[eid] = entry.get('parentId')
            children.setdefault(entry.get('parentId'), []).append(eid)
            last_id = eid

        if not by_id:
            return []

        leaves = [eid for eid in by_id if not children.get(eid)]
        if leaves:
            leaves.sort(key=lambda eid: by_id[eid].get('timestamp') or '')
            node = leaves[-1]
        else:
            node = last_id

        branch = []
        seen = set()
        while node and node in by_id and node not in seen:
            seen.add(node)
            branch.append(by_id[node])
            node = parents.get(node)
        branch.reverse()

        out = []
        for entry in branch:
            if entry.get('type') != 'message':
                continue
            message = entry.get('message') or {}
            content = text_of(message.get('content', ''))
            if content:
                out.append(Message(role=message.get('role', ''), content=content))
        return out

    def sessions(self) -> List[Session]:
        sessions = []
        for path in self.session_files():
            header = None
            header_ts = last_ts = None
            name = cwd = model = parent_session = None
            first_user_text = None
            message_count = 0
            total_tokens = 0

            for entry in read_json_lines(path):
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
                    name = name or entry.get('name')
                elif etype == 'model_change':
                    model = entry.get('modelId') or model
                elif etype == 'message':
                    message_count += 1
                    message = entry.get('message') or {}
                    if message.get('role') == 'user' and first_user_text is None:
                        first_user_text = text_of(message.get('content', ''))
                    model = message.get('model') or model
                    total_tokens += (message.get('usage') or {}).get('totalTokens', 0) or 0
                elif etype in ('compaction', 'branch_summary'):
                    total_tokens += (entry.get('usage') or {}).get('totalTokens', 0) or 0

            if not header:
                continue

            session_id = header.get('id') or path.stem
            if not name:
                name = (first_user_text or '').strip()
                if len(name) > 80:
                    name = name[:80] + '...'
                name = name or session_id[:8]
            ctime = parse_timestamp(header_ts)
            try:
                fallback_size = path.stat().st_size
            except OSError:
                fallback_size = 0
            sessions.append(Session(
                id=session_id,
                name=name,
                ctime=ctime,
                mtime=parse_timestamp(last_ts) or ctime,
                size=total_tokens or fallback_size,
                path=cwd or str(path),
                model=model,
                message_count=message_count,
                parent_id=self._session_uuid(parent_session),
            ))
        return sessions

    def _unsupported(self, operation: str):
        return UnsupportedOperation(
            self.name, operation,
            'sessions are branch trees; a rewrite would have to re-parent the graph')

    def inject_system(self, session_id: str, content: str) -> None:
        raise self._unsupported('system message injection')

    def inject_toolcall(self, session_id: str, content: str,
                        tool_name: str = 'context_from_source') -> None:
        raise self._unsupported('toolcall injection')

    def remove_messages(self, session_id: str, indices: List[int]) -> int:
        raise self._unsupported('message removal')


class OpencodeAgent(SqliteAgent):
    """Opencode: one SQLite database with session / message / part tables.

    Message text lives in `part` rows, so every read is a join and every
    write has to keep the two tables in step.
    """

    name = 'opencode'
    description = 'Opencode CLI'
    display_name = 'Opencode'
    db_name = 'opencode.db'
    files_read = 'opencode.db'

    @classmethod
    def default_base_path(cls) -> Path:
        return Path.home() / '.local/share/opencode'

    def sessions(self) -> List[Session]:
        conn = self.connect()
        if conn is None:
            return []
        sessions = []
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, title, time_created, time_updated,
                       tokens_input, tokens_output, model, directory, parent_id
                FROM session
                ORDER BY time_updated DESC
            ''')
            for row in cursor.fetchall():
                (session_id, title, created, updated,
                 tokens_in, tokens_out, model, directory, parent_id) = row
                try:
                    cursor.execute('SELECT COUNT(*) FROM message WHERE session_id = ?',
                                   (session_id,))
                    msg_count = cursor.fetchone()[0]
                except sqlite3.Error:
                    msg_count = None
                sessions.append(Session(
                    id=session_id,
                    name=title or session_id[:8],
                    ctime=epoch_ms(created),
                    mtime=epoch_ms(updated),
                    size=(tokens_in or 0) + (tokens_out or 0),
                    path=directory or str(self.db_path),
                    model=model,
                    message_count=msg_count,
                    parent_id=parent_id,
                ))
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return sessions

    def token_usage(self, session_id: str) -> Optional[Dict[str, int]]:
        conn = self.connect()
        if conn is None:
            return None
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT tokens_input, tokens_output FROM session WHERE id = ?',
                           (session_id,))
            row = cursor.fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if not row:
            return None
        tokens_input, tokens_output = row
        return {
            'total': (tokens_input or 0) + (tokens_output or 0),
            'input': tokens_input or 0,
            'output': tokens_output or 0,
        }

    def _rows(self, session_id: str) -> List[Tuple[str, str, str]]:
        """Return (message_id, role, text) for a session, in order."""
        conn = self.connect()
        if conn is None:
            raise SessionNotFound(self.name, session_id)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, data FROM message
                WHERE session_id = ?
                ORDER BY time_created
            ''', (session_id,))
            message_rows = cursor.fetchall()
            if not message_rows:
                cursor.execute('SELECT 1 FROM session WHERE id = ?', (session_id,))
                if cursor.fetchone() is None:
                    raise SessionNotFound(self.name, session_id)
            rows = []
            for msg_id, msg_data in message_rows:
                try:
                    role = json.loads(msg_data).get('role', '')
                except (json.JSONDecodeError, TypeError):
                    role = ''
                cursor.execute('''
                    SELECT data FROM part
                    WHERE message_id = ?
                    ORDER BY time_created
                ''', (msg_id,))
                parts = []
                for (part_data,) in cursor.fetchall():
                    try:
                        part = json.loads(part_data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if part.get('type') == 'text' and part.get('text'):
                        parts.append(part['text'])
                rows.append((msg_id, role, '\n'.join(parts)))
            return rows
        except sqlite3.Error:
            raise SessionNotFound(self.name, session_id)
        finally:
            conn.close()

    def raw_messages(self, session_id: str) -> List[Message]:
        return [Message(role=role, content=text)
                for _, role, text in self._rows(session_id) if text]

    def messages(self, session_id: str) -> List[Message]:
        return [m for m in self.raw_messages(session_id) if m.role in CONVERSATION_ROLES]

    def _upsert(self, session_id: str, content: str, find_sql: str,
                find_arg: str, data: dict, id_prefix: str) -> None:
        """Replace the text of the matching message, or insert a new one."""
        conn = self.connect()
        if conn is None:
            raise SessionNotFound(self.name, session_id)
        try:
            cursor = conn.cursor()
            cursor.execute(find_sql, (session_id, find_arg))
            row = cursor.fetchone()
            now_ms = int(time.time() * 1000)
            part = json.dumps({'type': 'text', 'text': content})
            payload = json.dumps(data)
            if row:
                msg_id = row[0]
                cursor.execute('UPDATE message SET data = ?, time_updated = ? WHERE id = ?',
                               (payload, now_ms, msg_id))
                cursor.execute('UPDATE part SET data = ? WHERE message_id = ?',
                               (part, msg_id))
            else:
                msg_id = f"{id_prefix}_{now_ms}"
                cursor.execute(
                    'INSERT INTO message (id, session_id, time_created, time_updated, data)'
                    ' VALUES (?, ?, ?, ?, ?)',
                    (msg_id, session_id, now_ms, now_ms, payload))
                cursor.execute(
                    'INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)'
                    ' VALUES (?, ?, ?, ?, ?, ?)',
                    (f"part_{msg_id}", msg_id, session_id, now_ms, now_ms, part))
            conn.commit()
        finally:
            conn.close()

    def inject_system(self, session_id: str, content: str) -> None:
        self._upsert(
            session_id, content,
            find_sql='SELECT id FROM message WHERE session_id = ? AND data LIKE ?',
            find_arg='%"role": "system"%',
            data={'role': 'system', 'content': content},
            id_prefix='ccopy',
        )

    def inject_toolcall(self, session_id: str, content: str,
                        tool_name: str = 'context_from_source') -> None:
        self._upsert(
            session_id, content,
            find_sql='SELECT id FROM message WHERE session_id = ? AND data LIKE ?',
            find_arg=f'%"name": "{tool_name}"%',
            data={'role': 'tool', 'name': tool_name, 'content': content},
            id_prefix='cconnect',
        )

    def remove_messages(self, session_id: str, indices: List[int]) -> int:
        msg_ids = [msg_id for msg_id, _, _ in self._rows(session_id)]
        targets = [msg_ids[i] for i in indices if 0 <= i < len(msg_ids)]
        if not targets:
            return 0
        conn = self.connect()
        if conn is None:
            raise SessionNotFound(self.name, session_id)
        try:
            cursor = conn.cursor()
            for msg_id in targets:
                cursor.execute('DELETE FROM part WHERE message_id = ?', (msg_id,))
                cursor.execute('DELETE FROM message WHERE id = ?', (msg_id,))
            conn.commit()
        finally:
            conn.close()
        return len(targets)


class GooseAgent(SqliteAgent):
    """Goose: a SQLite session index, with a legacy one-JSONL-per-session mode.

    Note this is *not* opencode's schema -- shared `storage_format` says
    nothing about how the rows are laid out.
    """

    name = 'goose'
    description = 'Goose AI agent'
    display_name = 'Goose'
    db_name = 'sessions/sessions.db'
    session_pattern = 'sessions/*.jsonl'
    files_read = 'sessions/sessions.db'

    @classmethod
    def default_base_path(cls) -> Path:
        """Goose's own (etcetera XDG) path resolution."""
        root = os.environ.get('GOOSE_PATH_ROOT')
        if root and os.path.isabs(root):
            return Path(root) / 'data'
        if sys.platform == 'win32':
            return Path(os.environ.get('APPDATA', '')) / 'Block' / 'goose' / 'data'
        xdg_data = os.environ.get('XDG_DATA_HOME')
        base = Path(xdg_data) if xdg_data and os.path.isabs(xdg_data) else Path.home() / '.local/share'
        return base / 'goose'

    def legacy_file(self, session_id: str) -> Path:
        return self.base_path / 'sessions' / f'{session_id}.jsonl'

    def _db_sessions(self) -> Optional[List[Session]]:
        conn = self.connect()
        if conn is None:
            return None
        sessions = []
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, name, description, working_dir, created_at, updated_at,
                       total_tokens, provider_name, model_config_json, parent_session_id
                FROM sessions
                ORDER BY updated_at DESC
            ''')
            for row in cursor.fetchall():
                (session_id, name, description, working_dir, created_at, updated_at,
                 total_tokens, provider_name, model_config_json, parent_id) = row
                model = None
                if model_config_json:
                    try:
                        model = json.loads(model_config_json).get('model_name')
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        pass
                try:
                    cursor.execute('SELECT COUNT(*) FROM messages WHERE session_id = ?',
                                   (session_id,))
                    msg_count = cursor.fetchone()[0]
                except sqlite3.Error:
                    msg_count = None
                sessions.append(Session(
                    id=session_id,
                    name=name or description or session_id[:8],
                    ctime=parse_timestamp(created_at),
                    mtime=parse_timestamp(updated_at),
                    size=total_tokens or 0,
                    path=working_dir or str(self.db_path),
                    model=model or provider_name,
                    message_count=msg_count,
                    parent_id=parent_id,
                ))
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        return sessions

    def _legacy_sessions(self) -> List[Session]:
        """Older installs: first line is metadata, the rest are messages."""
        if not self.base_path.exists():
            return []
        sessions = []
        for path in sorted(self.base_path.glob(self.session_pattern)):
            try:
                ctime, mtime, size = file_metadata(path)
                with open(path, 'r') as f:
                    header_line = f.readline()
                    if not header_line.strip():
                        continue
                    message_count = sum(1 for _ in f)
            except OSError:
                continue
            try:
                meta = json.loads(header_line)
            except json.JSONDecodeError:
                meta = {}
            session_id = meta.get('id') or path.stem
            sessions.append(Session(
                id=session_id,
                name=meta.get('description') or session_id[:8],
                ctime=parse_timestamp(meta.get('created_at')) or ctime,
                mtime=parse_timestamp(meta.get('updated_at')) or mtime,
                size=meta.get('total_tokens') or size,
                path=str(path),
                message_count=message_count,
            ))
        return sessions

    def sessions(self) -> List[Session]:
        db_sessions = self._db_sessions()
        return self._legacy_sessions() if db_sessions is None else db_sessions

    def messages(self, session_id: str) -> List[Message]:
        conn = self.connect()
        if conn is not None:
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT role, content_json FROM messages
                    WHERE session_id = ?
                    ORDER BY created_timestamp, id
                ''', (session_id,))
                rows = cursor.fetchall()
                if not rows:
                    cursor.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,))
                    if cursor.fetchone() is None:
                        raise SessionNotFound(self.name, session_id)
                out = []
                for role, content_json in rows:
                    try:
                        text = text_of(json.loads(content_json))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if role in CONVERSATION_ROLES and text:
                        out.append(Message(role=role, content=text))
                return out
            except sqlite3.Error:
                pass
            finally:
                conn.close()

        path = self.legacy_file(session_id)
        if not path.exists():
            raise SessionNotFound(self.name, session_id)
        out = []
        for i, entry in enumerate(read_json_lines(path)):
            if i == 0:
                continue  # session metadata header
            text = text_of(entry.get('content'))
            if entry.get('role') in CONVERSATION_ROLES and text:
                out.append(Message(role=entry['role'], content=text))
        return out


# --- Registry ---

AGENT_CLASSES = (
    ClaudeDesktopAgent,
    ClaudeCodeAgent,
    OpencodeAgent,
    CodexAgent,
    PiAgent,
    GooseAgent,
)

REGISTRY: Dict[str, Agent] = {cls.name: cls() for cls in AGENT_CLASSES}


def get_agent(name: str) -> Optional[Agent]:
    """Look up an agent by name."""
    return REGISTRY.get(name)


def agent_names() -> List[str]:
    return list(REGISTRY)


def installed() -> List[Agent]:
    """Agents whose storage is actually present on this machine."""
    return [a for a in REGISTRY.values() if a.exists()]
