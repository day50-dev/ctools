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
import uuid
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
    size: int  # real stored content size in bytes (always)
    path: Optional[str] = None
    model: Optional[str] = None
    message_count: Optional[int] = None
    parent_id: Optional[str] = None
    tokens: Optional[int] = None  # token count where the agent records it


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


def epoch_seconds(sec) -> Optional[datetime]:
    """Convert seconds-since-epoch to a naive local datetime."""
    if not sec:
        return None
    try:
        return datetime.fromtimestamp(float(sec))
    except (ValueError, TypeError, OSError):
        return None


def _truncated(text: str, limit: int = 80) -> str:
    """Collapse `text` to a single line and cap it at `limit` characters."""
    line = ' '.join(text.split())
    return line[:limit]


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


def scan_jsonl_head(path: Path, max_bytes: int = 262144) -> Tuple[List[dict], int]:
    """Parse only the leading lines of a JSONL file.

    Returns ``(entries, record_count)`` where ``entries`` is the list of
    parsed dict entries read from the leading ``max_bytes`` and
    ``record_count`` is the total number of records in the file.

    Listing a session does not need the whole file: the metadata that
    populates a row (cwd, title, first user turn, model) lives in the first
    dozen-or-so lines, but the message count needs the whole thing. Counting
    newlines over raw bytes is ~10x cheaper than JSON-parsing every line, so
    we parse the head (enough for the metadata) and count the rest. If the
    file is smaller than ``max_bytes`` the whole file is parsed.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0
    if size == 0:
        return [], 0
    try:
        with open(path, 'rb') as f:
            head = f.read(max_bytes)
    except OSError:
        return [], 0
    # Record count = newline count, plus one if the file does not end in a
    # newline (JSONL records are newline-delimited; real transcripts have no
    # blank lines). Count in chunks so the whole file is scanned, cheaply.
    total = head.count(b'\n')
    last = head[-1:]
    if size > len(head):
        try:
            with open(path, 'rb') as f:
                f.seek(len(head))
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    total += chunk.count(b'\n')
                    last = chunk[-1:]
        except OSError:
            pass
    if last and last != b'\n':
        total += 1
    entries: List[dict] = []
    for line in head.split(b'\n'):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line.decode('utf-8', 'replace'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries, total


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

    @property
    def storage_path(self) -> Path:
        """The file (or directory) holding this agent's storage, under
        `base_path`. For directory agents that is the sessions tree; for
        sqlite agents the database."""
        if self.files_read:
            return self.base_path / self.files_read.rstrip('/')
        if getattr(type(self), 'db_name', None):
            return self.base_path / type(self).db_name
        return self.base_path

    def storage_relative(self) -> str:
        """The agent's storage path, relative to $HOME, in the default
        install location (posix style).

        This is what cross-host ccopy pulls/pushes: e.g.
        ``.local/share/opencode/opencode.db`` or ``.pi/agent/sessions``.
        """
        root = self.home_relative_storage()
        if self.files_read:
            return f"{root}/{self.files_read.rstrip('/')}"
        if getattr(type(self), 'db_name', None):
            return f"{root}/{type(self).db_name}"
        return root

    def home_relative_storage(self) -> str:
        """The agent's base path as a path relative to $HOME, in the default
        install location (e.g. ``.local/share/opencode``).

        Cross-host ccopy anchors the remote tar at this, since storage is
        always rooted at the user's home on the remote side.
        """
        import os as _os
        home = _os.path.expanduser('~')
        return _os.path.relpath(str(self.default_base_path()), home)

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

    def raw_records(self, session_id: str) -> List[dict]:
        """The agent's verbatim storage record for each message, in order.

        This is the lossless payload a copy needs to replay a session into the
        *same* agent without flattening: pi's JSONL entries, opencode's
        message+part rows, and so on. Agents whose storage we cannot introspect
        return an empty list, in which case consumers fall back to the portable
        {role, content} conversation and accept the loss.

        The list is parallel to :meth:`raw_messages` when the agent can produce
        it; an empty list is the honest "we can't" answer.
        """
        return []

    def session_info(self, session_id: str) -> Optional[Dict[str, object]]:
        """Session-level metadata (model, timestamps) for one session, or None.

        Used by ccat --raw to annotate the envelope. Returns a plain dict with
        any subset of the keys the agent tracks; no requirement to fill all.
        """
        return None

    # --- common interchange format ---
    #
    # The common format is the ccat --raw envelope: a dict with
    #   { "context": [{role, content}, ...],   # portable spine (llcat/OpenAI)
    #     "source": "agent/session_id",
    #     "model": ..., "created": ..., "modified": ...,   # session facts
    #     "raw": [agent's verbatim records] }              # lossless, optional
    # Every agent that can be seeded gets to_common (export) and from_common
    # (import); agents that can't do either refuse with UnsupportedOperation.
    # The point of the 2N design: prove to_common and from_common per agent,
    # and A->B is just the composition to_common(A) -> from_common(B).

    def to_common(self, session_id: str) -> Dict[str, object]:
        """Export `session_id` to the common interchange format."""
        context = [{"role": m.role, "content": m.content}
                   for m in self.messages(session_id)]
        doc: Dict[str, object] = {'context': context,
                                  'source': f'{self.name}/{session_id}'}
        info = self.session_info(session_id)
        if info:
            for key in ('model', 'created', 'modified'):
                if info.get(key):
                    doc[key] = info[key]
        raw = self.raw_records(session_id)
        if raw:
            doc['raw'] = raw
        return doc

    def from_common(self, doc: Dict[str, object]) -> str:
        """Import a common-format envelope into a NEW session; return its id."""
        context = doc.get('context') or []
        if not isinstance(context, list):
            raise AgentError(f'{self.name}: common format missing a context list')
        # If the envelope carries verbatim records *for this agent* (a same-
        # agent round trip), replay them; otherwise project the portable spine.
        messages = self._from_common_messages(doc, context)
        return self.create_session(messages)

    def _from_common_messages(self, doc: Dict[str, object],
                              context: List[dict]) -> List[Message]:
        """Build the Message list a common import seeds from.

        Default: project the portable {role, content} spine. Agents that can
        replay their own verbatim records override this for lossless same-agent
        round trips.
        """
        out: List[Message] = []
        for rec in context:
            if isinstance(rec, dict) and rec.get('content'):
                out.append(Message(role=rec.get('role', 'user'),
                                   content=rec.get('content', '')))
        return out

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

    def create_session(self, messages: List[Message]) -> str:
        """Create a brand-new session holding `messages`; return its id.

        Used to seed a conversation into an agent that has none yet (e.g.
        migrating a transcript from another agent).  Agents whose storage
        cannot be written in a way the tool itself understands refuse with
        :class:`UnsupportedOperation`.
        """
        raise UnsupportedOperation(self.name, 'session creation')

    def supports_create(self) -> bool:
        """Whether this agent can seed a new session from a conversation."""
        fn = type(self).create_session
        return fn is not Agent.create_session


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

    def _session_dir(self) -> Path:
        """Where new session files are written (overridable)."""
        return self.base_path

    def _new_entry(self, message: Message, session_id: str, index: int) -> dict:
        """Build one native entry for `message` in a fresh session."""
        raise UnsupportedOperation(self.name, 'session creation')

    def _new_header(self, session_id: str) -> Optional[dict]:
        """An optional file header record, or None if the agent has none."""
        return None

    def create_session(self, messages: List[Message]) -> str:
        session_id = str(uuid.uuid4())
        lines = []
        header = self._new_header(session_id)
        if header is not None:
            lines.append(json.dumps(header))
        lines += [json.dumps(self._new_entry(m, session_id, i))
                  for i, m in enumerate(messages)]
        path = self._session_dir() / f'{session_id}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n' if lines else '')
        return session_id

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

    @staticmethod
    def _content_bytes(cursor, tables_cols, session_id: str) -> int:
        """Real stored size (bytes) of a session's message content.

        `tables_cols` is a list of (table, content_column) pairs; the lengths
        of the content columns are summed. Any table/column that does not
        exist is skipped, so this degrades gracefully across schemas. Returns
        0 when nothing can be measured.
        """
        total = 0
        for table, col in tables_cols:
            try:
                cursor.execute(
                    f'SELECT COALESCE(SUM(LENGTH({col})), 0) FROM {table} '
                    'WHERE session_id = ?', (session_id,))
                row = cursor.fetchone()
                if row and row[0]:
                    total += int(row[0])
            except sqlite3.Error:
                continue
        return total


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

    def _new_entry(self, message: Message, session_id: str, index: int) -> dict:
        etype = 'user' if message.role in ('user', 'system') else 'assistant'
        entry = {'type': etype, 'uuid': str(uuid.uuid4()),
                 'sessionId': session_id, 'timestamp': time.time(),
                 'message': {'role': message.role, 'content': message.content}}
        if etype == 'assistant':
            entry['message']['model'] = 'claude-sonnet-4-5'
        return entry

    def _session_dir(self) -> Path:
        return self.base_path / 'projects' / '-ctools'

    def _session_meta(self, entries: List[dict]) -> Tuple[Optional[str], Optional[str],
                                                          Optional[str], Optional[str]]:
        """Pull (title, first_user, model, cwd) from leading JSONL entries.

        Mirrors the full-file scan semantics on the (small) head list: the
        first non-None cwd, the first user/human turn, the first assistant
        model, and the last non-empty ai-title (later titles overwrite).
        """
        title = None
        first_user = None
        model = None
        cwd = None
        for entry in entries:
            etype = entry.get('type')
            if cwd is None:
                cwd = entry.get('cwd')
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
        return title, first_user, model, cwd

    def sessions(self) -> List[Session]:
        sessions = []
        for path in self.session_files():
            try:
                ctime, mtime, size = file_metadata(path)
            except OSError:
                continue
            session_id = path.stem
            entries, message_count = scan_jsonl_head(path)
            title, first_user, model, cwd = self._session_meta(entries)
            name = title or (first_user or '').strip().replace('\n', ' ')[:50] or session_id[:8]
            sessions.append(Session(
                id=session_id,
                name=name,
                ctime=ctime,
                mtime=mtime,
                size=size,
                path=cwd or str(path),
                model=model,
                message_count=message_count,
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

    def _new_header(self, session_id: str) -> dict:
        return {'session_meta': {'id': session_id,
                                 'timestamp': int(time.time()),
                                 'cwd': str(self.base_path)}}

    def _new_entry(self, message: Message, session_id: str, index: int) -> dict:
        payload = {'type': 'message', 'role': message.role,
                   'content': [{'type': 'input_text' if message.role == 'user'
                                else 'output_text', 'text': message.content}]}
        return {'type': 'response_item', 'payload': payload}

    def _session_dir(self) -> Path:
        return self.base_path / 'sessions'

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

    def raw_records(self, session_id: str) -> List[dict]:
        """The active-branch message entries, verbatim (lossless replay form)."""
        path = self.require_file(session_id)
        by_id, parents = {}, {}
        children, last_id = {}, None
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
        branch, seen = [], set()
        while node and node in by_id and node not in seen:
            seen.add(node)
            branch.append(by_id[node])
            node = parents.get(node)
        branch.reverse()
        return [e for e in branch if e.get('type') == 'message']

    def session_info(self, session_id: str) -> Optional[Dict[str, object]]:
        path = self.session_file(session_id)
        if path is None:
            return None
        header, model, last_ts = None, None, None
        for entry in read_json_lines(path):
            etype, ts = entry.get('type'), entry.get('timestamp')
            if ts:
                last_ts = ts
            if etype == 'session':
                header = entry
            elif etype == 'model_change':
                model = entry.get('modelId') or model
            elif etype == 'message':
                model = (entry.get('message') or {}).get('model') or model
        if header is None:
            return None
        info: Dict[str, object] = {'created': header.get('timestamp'),
                                   'modified': last_ts or header.get('timestamp')}
        if model:
            info['model'] = model
        return info

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
                file_size = path.stat().st_size
            except OSError:
                file_size = 0
            sessions.append(Session(
                id=session_id,
                name=name,
                ctime=ctime,
                mtime=parse_timestamp(last_ts) or ctime,
                size=file_size,
                path=cwd or str(path),
                model=model,
                message_count=message_count,
                tokens=total_tokens or None,
                parent_id=self._session_uuid(parent_session),
            ))
        return sessions

    def _pi_usage(self) -> Dict[str, object]:
        """A zeroed pi token-usage block.

        The pi TUI reads ``message.usage.input`` when rendering the footer's
        session stats; an assistant entry written without a ``usage`` object
        crashes ``pi --session`` with ``Cannot read properties of undefined
        (reading 'input')``. Seed every assistant message with a full (zero)
        usage block so imported sessions load cleanly.
        """
        return {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0,
                'totalTokens': 0,
                'cost': {'input': 0, 'output': 0, 'cacheRead': 0,
                         'cacheWrite': 0, 'total': 0}}

    def create_session(self, messages: List[Message]) -> str:
        session_id = str(uuid.uuid4())
        now_iso = datetime.now().isoformat()
        first_user = next((m.content for m in messages if m.role == 'user'), '')
        # Pi only discovers sessions under per-cwd subdirectories of
        # sessions/ (named `--<cwd-without-leading-slash>--`) and requires a
        # version-3 header; a session dropped at the top level of sessions/
        # is invisible to `pi --session` and `pi --resume`.
        cwd = os.getcwd()
        safe = f"--{cwd.lstrip('/').replace('/', '-')}--"
        session_dir = self.base_path / 'sessions' / safe
        session_dir.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({'type': 'session', 'version': 3, 'id': session_id,
                             'timestamp': now_iso, 'cwd': cwd,
                             'name': _truncated(first_user)})]
        prev: Optional[str] = None
        for i, message in enumerate(messages):
            # Write real-shaped pi messages so the TUI loads them. Content is a
            # list of text blocks; assistant entries carry a usage block (the
            # TUI's footer reads usage.input) plus a stop reason.
            content = [{'type': 'text', 'text': message.content}]
            body: Dict[str, object] = {'role': message.role, 'content': content,
                                       'timestamp': int(time.time() * 1000)}
            if message.role == 'assistant':
                body['usage'] = self._pi_usage()
                body['stopReason'] = 'stop'
            entry = {'type': 'message', 'id': uuid.uuid4().hex[:8],
                     'parentId': prev, 'timestamp': now_iso, 'message': body}
            lines.append(json.dumps(entry))
            prev = entry['id']
        path = session_dir / f'{int(time.time())}_{session_id}.jsonl'
        path.write_text('\n'.join(lines) + '\n')
        return session_id

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

    @staticmethod
    def _content_bytes(cursor, session_id: str) -> int:
        """Real stored size (bytes) of a session's message + part data.

        opencode does not always record token counts (some sessions have 0
        tokens despite real content), so the byte size of the actual stored
        rows is the honest size to report.
        """
        return SqliteAgent._content_bytes(cursor, [('message', 'data'),
                                                  ('part', 'data')], session_id)


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
                tokens = (tokens_in or 0) + (tokens_out or 0)
                # size is the real stored content bytes (always accurate); the
                # token count is carried separately where opencode records it.
                size = self._content_bytes(cursor, session_id)
                sessions.append(Session(
                    id=session_id,
                    name=title or session_id[:8],
                    ctime=epoch_ms(created),
                    mtime=epoch_ms(updated),
                    size=size,
                    path=directory or str(self.db_path),
                    model=model,
                    message_count=msg_count,
                    parent_id=parent_id,
                    tokens=tokens or None,
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

    def raw_records(self, session_id: str) -> List[dict]:
        """Each message's verbatim `message.data` plus its `part` rows, in order."""
        conn = self.connect()
        if conn is None:
            raise SessionNotFound(self.name, session_id)
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM session WHERE id = ?', (session_id,))
            if cursor.fetchone() is None:
                raise SessionNotFound(self.name, session_id)
            cursor.execute('''
                SELECT id, data FROM message
                WHERE session_id = ?
                ORDER BY time_created
            ''', (session_id,))
            out = []
            for msg_id, msg_data in cursor.fetchall():
                try:
                    message = json.loads(msg_data)
                except (json.JSONDecodeError, TypeError):
                    continue
                cursor.execute('''
                    SELECT data FROM part WHERE message_id = ? ORDER BY time_created
                ''', (msg_id,))
                parts = []
                for (part_data,) in cursor.fetchall():
                    try:
                        parts.append(json.loads(part_data))
                    except (json.JSONDecodeError, TypeError):
                        continue
                out.append({'message': message, 'parts': parts})
            return out
        except sqlite3.Error:
            raise SessionNotFound(self.name, session_id)
        finally:
            conn.close()

    def session_info(self, session_id: str) -> Optional[Dict[str, object]]:
        conn = self.connect()
        if conn is None:
            return None
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT model, time_created, time_updated FROM session WHERE id = ?
            ''', (session_id,))
            row = cursor.fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if row is None:
            return None
        model, created, updated = row
        info: Dict[str, object] = {
            'created': epoch_ms(created).isoformat() if created else None,
            'modified': epoch_ms(updated).isoformat() if updated else None,
        }
        if model:
            info['model'] = model
        return info

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
            id_prefix='cextract',
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

    def create_session(self, messages: List[Message]) -> str:
        """Seed a real, loadable opencode session.

        The runtime (opencode -s / the TUI) reads richer message data than our
        portable {role, content} spine: user messages carry time.created + agent;
        assistant messages additionally carry model/provider, a parent link, and a
        completion time; every part links back to its message/session by id. We
        write that shape so the created session actually loads, not just parses.
        """
        session_id = f"ses_{uuid.uuid4().hex[:24]}"
        now_ms = int(time.time() * 1000)
        first_user = next((m.content for m in messages if m.role == 'user'), '')
        cwd = os.getcwd()
        try:
            conn = sqlite3.connect(str(self.db_path))
        except sqlite3.Error as exc:
            raise AgentError(f"{self.name}: cannot open {self.db_path} ({exc})")
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session'")
            if cursor.fetchone() is None:
                raise AgentError(f"{self.name}: no session table in {self.db_path}")
            project_id = f"prt_{uuid.uuid4().hex[:12]}"
            cursor.execute('''
                INSERT INTO session (id, project_id, parent_id, slug, directory, title,
                    version, time_created, time_updated, cost,
                    tokens_input, tokens_output, tokens_reasoning,
                    tokens_cache_read, tokens_cache_write)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (session_id, project_id, None, session_id, str(self.base_path),
                  _truncated(first_user or 'ctools session'), '0',
                  now_ms, now_ms, 0.0, 0, 0, 0, 0, 0))
            prev_id: Optional[str] = None
            for i, message in enumerate(messages):
                msg_id = f"msg_{session_id}_{i}"
                m_created = now_ms + i
                m_data: Dict[str, object] = {
                    'role': message.role,
                    'time': {'created': m_created},
                    'agent': 'build',
                    'path': {'cwd': cwd, 'root': cwd},
                }
                if message.role == 'assistant':
                    m_data['model'] = {'providerID': 'ctools', 'modelID': 'imported'}
                    m_data['modelID'] = 'imported'
                    m_data['providerID'] = 'ctools'
                    m_data['mode'] = 'build'
                    if prev_id:
                        m_data['parentID'] = prev_id
                    m_data['time'] = {'created': m_created,
                                      'completed': m_created + 1}
                    m_data['finish'] = 'stop'
                    m_data['tokens'] = {'input': 0, 'output': 0, 'reasoning': 0,
                                        'cache': {'read': 0, 'write': 0}}
                else:
                    m_data['summary'] = {'title': '' , 'diffs': []}
                part_id = f"part_{msg_id}"
                part_data = {'type': 'text', 'text': message.content,
                             'id': part_id, 'sessionID': session_id,
                             'messageID': msg_id}
                cursor.execute(
                    'INSERT INTO message (id, session_id, time_created, time_updated, data)'
                    ' VALUES (?, ?, ?, ?, ?)',
                    (msg_id, session_id, m_created, m_created,
                     json.dumps(m_data)))
                cursor.execute(
                    'INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)'
                    ' VALUES (?, ?, ?, ?, ?, ?)',
                    (part_id, msg_id, session_id, m_created, m_created,
                     json.dumps(part_data)))
                prev_id = msg_id
            conn.commit()
        finally:
            conn.close()
        return session_id


class KiloAgent(OpencodeAgent):
    """Kilo: an Opencode fork that keeps the same SQLite schema.

    Only the database location and identity differ -- the ``session`` /
    ``message`` / ``part`` tables are byte-for-byte opencode's, so every read
    and write is inherited unchanged.
    """

    name = 'kilo'
    description = 'Kilo CLI'
    display_name = 'Kilo'
    db_name = 'kilo.db'
    files_read = 'kilo.db'

    @classmethod
    def default_base_path(cls) -> Path:
        return Path.home() / '.local/share/kilo'


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
                    size=self._content_bytes(cursor, [('messages', 'content_json')],
                                              session_id),
                    path=working_dir or str(self.db_path),
                    model=model or provider_name,
                    message_count=msg_count,
                    parent_id=parent_id,
                    tokens=total_tokens or None,
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
                size=size,
                path=str(path),
                message_count=message_count,
                tokens=int(meta.get('total_tokens') or 0) or None,
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

    def create_session(self, messages: List[Message]) -> str:
        """Seed a real, loadable Goose session.

        The sessions row carries the columns goose's readers sort and list on
        (working_dir NOT NULL, RFC-3339 timestamps like goose's own), and each
        message is a real content block (``[{\"type\": \"text\", ...}]``) with a
        monotonically increasing created_timestamp, the order goose reads by.
        """
        conn = self.connect()
        if conn is None:
            raise AgentError(f"{self.name}: cannot open {self.db_path}")
        base_id = f"{datetime.now():%Y%m%d_%H%M%S}"
        session_id = base_id
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
            n = 1
            while cursor.fetchone() is not None:
                session_id = f"{base_id}_{n}"
                n += 1
                cursor.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
        except sqlite3.Error:
            session_id = f"{base_id}_{uuid.uuid4().hex[:6]}"
        stamp = datetime.now().astimezone().isoformat()
        first_user = next((m.content for m in messages if m.role == 'user'), '')
        try:
            cursor.execute('''
                INSERT INTO sessions (id, name, description, working_dir,
                    created_at, updated_at)
                VALUES (?, '', ?, ?, ?, ?)
            ''', (session_id, _truncated(first_user or 'ctools session'),
                  os.getcwd(), stamp, stamp))
            cursor.executemany(
                'INSERT INTO messages (session_id, role, content_json,'
                ' created_timestamp) VALUES (?, ?, ?, ?)',
                [(session_id, m.role,
                  json.dumps([{'type': 'text', 'text': m.content}]),
                  int(time.time()) + i)
                 for i, m in enumerate(messages)])
            conn.commit()
        except sqlite3.Error as exc:
            raise AgentError(f"{self.name}: cannot write session ({exc})")
        finally:
            conn.close()
        return session_id


class HermesAgent(SqliteAgent):
    """Hermes: one SQLite database (``state.db``) with sessions / messages tables.

    Message text lives directly in ``messages.content`` -- either a plain
    string or a ``\\x00json:``-prefixed JSON blob for multimodal turns.  Only
    the primary home (``HERMES_HOME`` or ``~/.hermes``) is read; per-profile
    databases under ``profiles/`` are a separate store.
    """

    name = 'hermes'
    description = 'Hermes agent'
    display_name = 'Hermes'
    db_name = 'state.db'
    files_read = 'state.db'
    _CONTENT_JSON_PREFIX = '\x00json:'

    @classmethod
    def default_base_path(cls) -> Path:
        root = os.environ.get('HERMES_HOME')
        if root and os.path.isabs(root):
            return Path(root)
        return Path.home() / '.hermes'

    def _decode_content(self, content) -> str:
        if content is None:
            return ''
        if isinstance(content, str) and content.startswith(self._CONTENT_JSON_PREFIX):
            payload = content[len(self._CONTENT_JSON_PREFIX):]
            try:
                return text_of(json.loads(payload))
            except (json.JSONDecodeError, TypeError):
                return payload
        if isinstance(content, str):
            return content
        return text_of(content)

    def sessions(self) -> List[Session]:
        conn = self.connect()
        if conn is None:
            return []
        sessions = []
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, title, display_name, started_at, last_activity_at,
                       ended_at, input_tokens, output_tokens, model, cwd,
                       parent_session_id, message_count
                FROM sessions
                WHERE COALESCE(hidden, 0) = 0
                ORDER BY COALESCE(last_activity_at, ended_at, started_at) DESC
            ''')
            for row in cursor.fetchall():
                (session_id, title, display_name, created, activity, ended,
                 tokens_in, tokens_out, model, cwd, parent_id, msg_count) = row
                sessions.append(Session(
                    id=session_id,
                    name=title or display_name or session_id[:8],
                    ctime=epoch_seconds(created),
                    mtime=epoch_seconds(activity or ended),
                    size=self._content_bytes(cursor, [('messages', 'content')],
                                             session_id),
                    path=cwd or str(self.db_path),
                    model=model,
                    message_count=msg_count,
                    parent_id=parent_id,
                    tokens=(tokens_in or 0) + (tokens_out or 0) or None,
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
            cursor.execute(
                'SELECT input_tokens, output_tokens, reasoning_tokens FROM sessions WHERE id = ?',
                (session_id,))
            row = cursor.fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if not row:
            return None
        tokens_input, tokens_output, tokens_reasoning = row
        return {
            'total': (tokens_input or 0) + (tokens_output or 0) + (tokens_reasoning or 0),
            'input': tokens_input or 0,
            'output': tokens_output or 0,
        }

    def _rows(self, session_id: str) -> List[Tuple[str, str]]:
        """Return (role, text) for a session's live messages, in order."""
        conn = self.connect()
        if conn is None:
            raise SessionNotFound(self.name, session_id)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT role, content FROM messages
                WHERE session_id = ? AND COALESCE(active, 1) = 1
                ORDER BY timestamp, id
            ''', (session_id,))
            rows = [(role, self._decode_content(content))
                    for role, content in cursor.fetchall()]
            rows = [(role, text) for role, text in rows if text]
            if not rows:
                cursor.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,))
                if cursor.fetchone() is None:
                    raise SessionNotFound(self.name, session_id)
            return rows
        except sqlite3.Error:
            raise SessionNotFound(self.name, session_id)
        finally:
            conn.close()

    def raw_messages(self, session_id: str) -> List[Message]:
        return [Message(role=role, content=text) for role, text in self._rows(session_id)]

    def messages(self, session_id: str) -> List[Message]:
        return [m for m in self.raw_messages(session_id) if m.role in CONVERSATION_ROLES]

    def create_session(self, messages: List[Message]) -> str:
        """Seed a real, loadable Hermes session.

        Mirrors hermes's own foreign-history import (hermes_state_portability):
        the id is minted like an import id (``YYYYMMDD_HHMMSS_<hex12>``), the
        sessions row carries source/title/cwd/timestamps so the session lists,
        sorts and stays resumable (``ended_at`` NULL), and each message is the
        minimal real row (role, content, timestamp, active) that hermes's read
        path loads in id order; hermes's insert triggers complete the rest
        (message_uid, FTS, display order).
        """
        conn = self.connect()
        if conn is None:
            raise AgentError(f"{self.name}: cannot open {self.db_path}")
        session_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:12]}"
        now = time.time()
        first_user = next((m.content for m in messages if m.role == 'user'), '')
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'")
            if cursor.fetchone() is None:
                raise AgentError(f"{self.name}: no sessions table in {self.db_path}")
            cursor.execute('''
                INSERT INTO sessions (id, source, title, model, cwd,
                    started_at, last_activity_at, message_count, hidden)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 0)
            ''', (session_id, 'import',
                  _truncated(first_user or 'ctools session'),
                  os.getcwd(), now, now, len(messages)))
            cursor.executemany(
                'INSERT INTO messages (session_id, role, content, timestamp, active)'
                ' VALUES (?, ?, ?, ?, 1)',
                [(session_id, m.role, m.content, now + i)
                 for i, m in enumerate(messages)])
            conn.commit()
        except sqlite3.Error as exc:
            raise AgentError(f"{self.name}: cannot write session ({exc})")
        finally:
            conn.close()
        return session_id


class ClineAgent(Agent):
    """Cline (cline.bot): one directory per task under ``~/.cline/data/tasks/``.

    The conversation lives in ``api_conversation_history.json`` as a list of
    ``{role, content}`` pairs, where each ``content`` is a JSON-encoded list of
    content blocks.  Newer installs (and the Roo Code fork) also keep a
    ``history_item.json`` with a title and token counts, so that is preferred
    when present.
    """

    name = 'cline'
    description = 'Cline (cline.bot)'
    display_name = 'Cline'
    storage_format = 'json'
    files_read = 'data/tasks/'

    @classmethod
    def default_base_path(cls) -> Path:
        return Path.home() / '.cline'

    def tasks_root(self) -> Path:
        return self.base_path / 'data' / 'tasks'

    def task_dirs(self) -> List[Path]:
        root = self.tasks_root()
        if not root.exists():
            return []
        return sorted(p for p in root.iterdir() if p.is_dir())

    def _task_dir(self, session_id: str) -> Path:
        return self.tasks_root() / session_id

    def _read_history(self, task_dir: Path) -> List[Message]:
        path = task_dir / 'api_conversation_history.json'
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = item.get('role', '')
            content = item.get('content', '')
            if isinstance(content, str) and content:
                try:
                    blocks = json.loads(content)
                except json.JSONDecodeError:
                    blocks = content
                text = text_of(blocks)
            else:
                text = text_of(content)
            if text:
                out.append(Message(role=role, content=text))
        return out

    def _read_meta(self, task_dir: Path) -> Dict[str, object]:
        meta: Dict[str, object] = {'name': None, 'model': None, 'tokens': 0,
                                   'path': None, 'ts': None}
        hist = task_dir / 'history_item.json'
        if hist.exists():
            try:
                h = json.loads(hist.read_text())
                meta['name'] = h.get('task')
                meta['model'] = h.get('model')
                meta['tokens'] = (h.get('tokensIn') or 0) + (h.get('tokensOut') or 0)
                meta['path'] = h.get('workspace')
                meta['ts'] = h.get('ts')
            except (OSError, json.JSONDecodeError):
                pass
        tm = task_dir / 'task_metadata.json'
        if tm.exists():
            try:
                m = json.loads(tm.read_text())
                usage = m.get('model_usage') or []
                if usage and isinstance(usage, list) and isinstance(usage[0], dict):
                    if not meta['model']:
                        meta['model'] = usage[0].get('model_id')
                if m.get('files_in_context'):
                    pass
            except (OSError, json.JSONDecodeError):
                pass
        ui = task_dir / 'ui_messages.json'
        if ui.exists():
            try:
                u = json.loads(ui.read_text())
                if isinstance(u, list):
                    stamps = [x.get('ts') for x in u
                              if isinstance(x, dict) and isinstance(x.get('ts'), (int, float))]
                    if stamps:
                        if not meta['ts']:
                            meta['ts'] = min(stamps)
                        if not meta['path']:
                            for x in u:
                                if isinstance(x, dict):
                                    cwd = x.get('cwd') or (x.get('modelInfo') or {}).get('cwd')
                                    if cwd:
                                        meta['path'] = cwd
                                        break
            except (OSError, json.JSONDecodeError):
                pass
        return meta

    def sessions(self) -> List[Session]:
        sessions = []
        for task_dir in self.task_dirs():
            session_id = task_dir.name
            messages = self._read_history(task_dir)
            meta = self._read_meta(task_dir)
            first_user = next((m.content for m in messages if m.role == 'user'), '')
            name = str(meta['name']) if meta['name'] else _truncated(first_user or session_id[:8])
            try:
                ctime, mtime, _hist_size = file_metadata(task_dir / 'api_conversation_history.json')
            except OSError:
                ctime = mtime = None
                _hist_size = 0
            ts = meta['ts']
            if ts:
                created = epoch_ms(ts)
                if created:
                    ctime = mtime = created
            sessions.append(Session(
                id=session_id,
                name=str(name)[:80],
                ctime=ctime,
                mtime=mtime,
                size=int(_hist_size),
                path=meta['path'] or str(task_dir),
                model=meta['model'],
                message_count=len(messages),
                parent_id=None,
                tokens=int(meta['tokens'] or 0) or None,
            ))
        return sessions

    def messages(self, session_id: str) -> List[Message]:
        task_dir = self._task_dir(session_id)
        if not task_dir.is_dir():
            raise SessionNotFound(self.name, session_id)
        return [m for m in self._read_history(task_dir) if m.role in CONVERSATION_ROLES]

    def raw_messages(self, session_id: str) -> List[Message]:
        task_dir = self._task_dir(session_id)
        if not task_dir.is_dir():
            raise SessionNotFound(self.name, session_id)
        return self._read_history(task_dir)


class OmpAgent(Agent):
    """omp (oh-my-pi): JSONL session journals under ``~/.omp/agent/sessions/``.

    Each file is a version-3 journal: an optional fixed-width ``title`` record,
    a ``session`` header, then tree entries keyed by ``id`` / ``parentId``.
    The conversation is the ancestor chain from the leaf (the last entry in
    file order) back to the root.
    """

    name = 'omp'
    description = 'omp (oh-my-pi)'
    display_name = 'omp'
    storage_format = 'jsonl'
    session_pattern = 'agent/sessions/**/*.jsonl'
    files_read = 'agent/sessions/'

    @classmethod
    def default_base_path(cls) -> Path:
        root = os.environ.get('OMP_HOME')
        if root and os.path.isabs(root):
            return Path(root)
        return Path.home() / '.omp'

    def session_files(self) -> List[Path]:
        root = self.base_path / 'agent' / 'sessions'
        if not root.exists():
            return []
        return sorted(root.rglob('*.jsonl'))

    def _entry(self, obj: dict) -> Optional[Dict[str, object]]:
        entry_id = obj.get('id')
        parent = obj.get('parentId')
        if isinstance(entry_id, str) and entry_id and \
                (parent is None or isinstance(parent, str)):
            return {'id': entry_id, 'parentId': parent,
                    'type': obj.get('type', ''), 'message': obj.get('message')}
        return None

    def _parse(self, path: Path) -> Dict[str, object]:
        title = None
        header: Dict[str, object] = {}
        entries: List[Dict[str, object]] = []
        for line in read_json_lines(path):
            t = line.get('type')
            if t == 'title':
                if line.get('v') == 1 and isinstance(line.get('title'), str):
                    title = line['title']
                continue
            if t == 'session':
                header = line
                continue
            entry = self._entry(line)
            if entry is not None:
                entries.append(entry)
        return {'title': title, 'header': header, 'entries': entries}

    def _main_path(self, entries: List[Dict[str, object]]) -> List[Dict[str, object]]:
        by_id: Dict[str, Dict[str, object]] = {}
        for entry in entries:
            by_id.setdefault(str(entry['id']), entry)
        if not entries:
            return []
        path: List[Dict[str, object]] = []
        current: Optional[Dict[str, object]] = entries[-1]
        seen = set()
        while current is not None:
            eid = str(current['id'])
            if eid in seen:
                break
            seen.add(eid)
            path.append(current)
            parent = current.get('parentId')
            current = by_id.get(str(parent)) if parent else None
        path.reverse()
        return path

    @staticmethod
    def _message_role(entry: Dict[str, object]) -> Optional[str]:
        message = entry.get('message')
        if not isinstance(message, dict):
            return None
        role = message.get('role')
        return role if isinstance(role, str) else None

    @staticmethod
    def _message_text(entry: Dict[str, object]) -> str:
        message = entry.get('message')
        if not isinstance(message, dict):
            return ''
        content = message.get('content')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return text_of(content)
        return ''

    @staticmethod
    def _message_total_tokens(entry: Dict[str, object]) -> int:
        message = entry.get('message')
        if not isinstance(message, dict):
            return 0
        usage = message.get('usage')
        if isinstance(usage, dict) and isinstance(usage.get('totalTokens'), (int, float)):
            return int(usage['totalTokens'])
        return 0

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def sessions(self) -> List[Session]:
        sessions = []
        for path in self.session_files():
            try:
                parsed = self._parse(path)
            except OSError:
                continue
            header = parsed['header']
            if not header:
                continue
            entry_id = header.get('id') or path.stem
            entries = parsed['entries']
            conversation = [e for e in self._main_path(entries)
                            if e.get('type') == 'message']
            first_user = next((self._message_text(e) for e in conversation
                               if self._message_role(e) == 'user'), '')
            model = None
            for e in conversation:
                if self._message_role(e) == 'assistant':
                    msg = e.get('message')
                    if isinstance(msg, dict) and isinstance(msg.get('model'), str):
                        model = msg['model']
            sessions.append(Session(
                id=str(entry_id),
                name=_truncated(str(parsed['title'] or header.get('title') or first_user)),
                ctime=parse_timestamp(header.get('timestamp')),
                mtime=parse_timestamp(header.get('timestamp')),
                size=self._file_size(path),
                path=header.get('cwd') or str(path),
                model=model,
                message_count=len(conversation),
                parent_id=None,
                tokens=sum(self._message_total_tokens(e) for e in conversation) or None,
            ))
        return sessions

    def _conversation(self, path: Path) -> List[Message]:
        entries = self._parse(path)['entries']
        out = []
        for e in self._main_path(entries):
            if e.get('type') != 'message':
                continue
            role = self._message_role(e)
            text = self._message_text(e)
            if role and text:
                out.append(Message(role=role, content=text))
        return out

    def raw_messages(self, session_id: str) -> List[Message]:
        for path in self.session_files():
            header = self._parse(path)['header']
            if header and str(header.get('id')) == session_id:
                return self._conversation(path)
        raise SessionNotFound(self.name, session_id)

    def messages(self, session_id: str) -> List[Message]:
        return [m for m in self.raw_messages(session_id) if m.role in CONVERSATION_ROLES]


class FreebuffAgent(Agent):
    """Freebuff / Codebuff (freebuff.com): one directory per chat under
    ``~/.config/<codebuff>/projects/<project>/chats/<chat>/``.

    Each chat holds ``chat-meta.json`` (title, counts) and
    ``chat-messages.json`` (a list of ``{variant, content, blocks}``).  The
    config directory is discovered by name first, then by the
    ``codebuff-metadata.json`` marker so rebranded installs are still found.
    """

    name = 'freebuff'
    description = 'Freebuff (Codebuff)'
    display_name = 'Freebuff'
    storage_format = 'json'
    files_read = 'projects/'
    _MARKER = 'codebuff-metadata.json'

    @classmethod
    def default_base_path(cls) -> Path:
        override = os.environ.get('FREEBUFF_CONFIG_DIR')
        if override and os.path.isabs(override):
            return Path(override)
        config_root = Path.home() / '.config'
        for name in ('freebuff', 'codebuff'):
            candidate = config_root / name
            if (candidate / cls._MARKER).exists() or (candidate / 'projects').is_dir():
                return candidate
        try:
            entries = list(config_root.iterdir())
        except OSError:
            entries = []
        for candidate in sorted(entries):
            try:
                if (candidate / cls._MARKER).is_file():
                    return candidate
            except OSError:
                continue
        return config_root / 'freebuff'

    def chat_dirs(self) -> List[Path]:
        root = self.base_path / 'projects'
        if not root.exists():
            return []
        return sorted(root.glob('*/chats/*'))

    def _chat_dir(self, session_id: str) -> Optional[Path]:
        for chat_dir in self.chat_dirs():
            if chat_dir.name == session_id:
                return chat_dir
        return None

    @staticmethod
    def _block_text(blocks) -> str:
        parts: List[str] = []
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text' and block.get('textType') != 'reasoning':
                content = block.get('content')
                if isinstance(content, str) and content.strip():
                    parts.append(content)
            elif block.get('type') == 'agent':
                nested = FreebuffAgent._block_text(block.get('blocks'))
                if nested:
                    parts.append(nested)
        return '\n'.join(parts)

    def _read_messages(self, chat_dir: Path) -> List[Message]:
        path = chat_dir / 'chat-messages.json'
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            variant = item.get('variant')
            if variant == 'ai':
                role = 'assistant'
            elif variant == 'user':
                role = 'user'
            else:
                continue
            content = item.get('content')
            text = content if isinstance(content, str) else ''
            if not text.strip():
                text = self._block_text(item.get('blocks'))
            if text.strip():
                out.append(Message(role=role, content=text))
        return out

    def sessions(self) -> List[Session]:
        sessions = []
        for chat_dir in self.chat_dirs():
            session_id = chat_dir.name
            meta: Dict[str, object] = {}
            meta_path = chat_dir / 'chat-meta.json'
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except (OSError, json.JSONDecodeError):
                    meta = {}
            messages = self._read_messages(chat_dir)
            title = meta.get('firstPrompt') if isinstance(meta, dict) else None
            first_user = next((m.content for m in messages if m.role == 'user'), '')
            name = title or first_user or session_id[:8]
            ctime, mtime, _size = file_metadata(chat_dir)
            mtime_ms = meta.get('messagesMtimeMs') if isinstance(meta, dict) else None
            if mtime_ms:
                parsed_mtime = epoch_ms(mtime_ms)
                if parsed_mtime:
                    mtime = parsed_mtime
            message_count = meta.get('messageCount') if isinstance(meta, dict) else None
            size = meta.get('messagesSize') if isinstance(meta, dict) else None
            sessions.append(Session(
                id=session_id,
                name=_truncated(str(name)),
                ctime=ctime,
                mtime=mtime,
                size=int(size) if isinstance(size, (int, float)) else 0,
                path=str(chat_dir),
                model=None,
                message_count=int(message_count) if isinstance(message_count, (int, float))
                else len(messages),
                parent_id=None,
            ))
        return sessions

    def raw_messages(self, session_id: str) -> List[Message]:
        chat_dir = self._chat_dir(session_id)
        if chat_dir is None:
            raise SessionNotFound(self.name, session_id)
        return self._read_messages(chat_dir)

    def messages(self, session_id: str) -> List[Message]:
        return [m for m in self.raw_messages(session_id) if m.role in CONVERSATION_ROLES]


# --- Registry ---

AGENT_CLASSES = (
    ClaudeDesktopAgent,
    ClaudeCodeAgent,
    OpencodeAgent,
    KiloAgent,
    CodexAgent,
    PiAgent,
    GooseAgent,
    HermesAgent,
    ClineAgent,
    OmpAgent,
    FreebuffAgent,
)

REGISTRY: Dict[str, Agent] = {cls.name: cls() for cls in AGENT_CLASSES}


def normalize_name(name: str) -> str:
    """Fold a human spelling of an agent name onto its registry key.

    Display names carry spaces and capitals ("Claude Code") that make poor
    command-line arguments, so anything that differs from the key only by
    case or by space/underscore separators resolves to the same agent.
    """
    return re.sub(r'[\s_]+', '-', name.strip()).lower()


_ALIASES: Dict[str, str] = {}
for _agent in REGISTRY.values():
    for _spelling in (_agent.name, _agent.display_name or _agent.name):
        _ALIASES.setdefault(normalize_name(_spelling), _agent.name)


def get_agent(name: str) -> Optional[Agent]:
    """Look up an agent by registry key, display name, or a spelling variant."""
    if name in REGISTRY:
        return REGISTRY[name]
    key = _ALIASES.get(normalize_name(name))
    return REGISTRY.get(key) if key else None


def agent_names() -> List[str]:
    return list(REGISTRY)


def installed() -> List[Agent]:
    """Agents whose storage is actually present on this machine."""
    return [a for a in REGISTRY.values() if a.exists()]


# --- Resume commands ---
#
# Each value is a template with a ``{id}`` placeholder filled with the session
# id.  These are the flags the tools actually document; override per-machine in
# ~/.config/ctools/resume-commands.json ({"agent": "command {id}"}).

RESUME_COMMANDS: Dict[str, str] = {
    'claude-code': 'claude --resume {id}',
    'claude': 'claude --resume {id}',
    'opencode': 'opencode -s {id}',
    'kilo': 'kilo -s {id}',
    'codex': 'codex resume {id}',
    'pi': 'pi --session {id}',
    'goose': 'goose session resume {id}',
    'hermes': 'hermes resume {id}',
    'cline': 'cline --resume {id}',
    'omp': 'omp resume {id}',
    'freebuff': 'freebuff --resume {id}',
}


def _resume_override() -> Dict[str, str]:
    path = Path.home() / '.config' / 'ctools' / 'resume-commands.json'
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return {k: str(v) for k, v in data.items()}
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def get_resume_command(agent_name: str, session_id: str) -> Optional[str]:
    """The command to resume `session_id` under `agent_name`, or None.

    A per-machine override (see :data:`RESUME_COMMANDS`) wins over the built-in
    table.
    """
    template = _resume_override().get(agent_name) or RESUME_COMMANDS.get(agent_name)
    if not template:
        return None
    return template.format(id=session_id)
