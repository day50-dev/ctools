"""OpenClaw agent: reads per-agent SQLite stores under ~/.openclaw/agents/.

OpenClaw is a Gateway-owned store, so ctools is read/export-only: sessions(),
messages(), raw_records(), session_info() and to_common() are verified here.
create_session() is intentionally refused (UnsupportedOperation) because a raw
insert cannot satisfy the store's canonical-index + integrity validation.

The fixture mirrors the real schema (src/state/openclaw-agent-schema.sql):
  session_nodes(session_key, current_session_id, entry_json, created_at, ...)
  session_windows(session_id, model, created_at, ...)
  transcript_events(session_id, seq, event_json, created_at)
"""

import json
import sqlite3

import pytest

from ctools.agents import OpenclawAgent, SessionNotFound, UnsupportedOperation, REGISTRY


# --- fixture -------------------------------------------------------------

_NOW = 1_750_000_000_000          # ms
_CWD = '/home/chris/day50'

_HEADER = {"type": "session", "id": "h1", "timestamp": "2025-06-15T00:00:00Z",
           "cwd": _CWD, "version": 3}


def _msg(role, content, usage=None, ts=None):
    """A transcript_events entry for a message, in the real stored shape."""
    message = {"role": role, "content": content,
               "timestamp": ts or _NOW + 1000}
    if role == "assistant":
        message["api"] = "openai-responses"
        message["provider"] = "openai"
        message["model"] = "openai/gpt-5"
        message["stopReason"] = "stop"
        message["usage"] = usage or {"input": 10, "output": 20, "cacheRead": 0,
                                     "cacheWrite": 0, "totalTokens": 30,
                                     "cost": {"input": 0.0, "output": 0.0,
                                              "cacheRead": 0.0, "cacheWrite": 0.0,
                                              "total": 0.0}}
    return {"type": "message", "id": f"m-{role}", "parentId": None,
            "timestamp": "2025-06-15T00:00:01Z", "message": message}


def _tool_result():
    return {"type": "message", "id": "m-tr", "parentId": None,
            "timestamp": "2025-06-15T00:00:02Z",
            "message": {"role": "toolResult", "toolCallId": "c1",
                        "toolName": "run", "isError": False,
                        "content": [{"type": "text", "text": "OK, 0"}],
                        "timestamp": _NOW + 2000}}


def _entry_db(agent_id, db_path, window="w1", key="agent:main:main",
              model="openai/gpt-5", cwd=_CWD, events=None,
              node_created=_NOW, node_updated=_NOW + 5000,
              archived_at=None, display_name=None):
    """Create one agent's openclaw-agent.sqlite with a single live session."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE session_nodes (
            session_key TEXT PRIMARY KEY,
            current_session_id TEXT NOT NULL,
            entry_json TEXT NOT NULL,
            created_at INTEGER,
            updated_at INTEGER,
            display_name TEXT,
            label TEXT,
            archived_at INTEGER
        )
    ''')
    cur.execute('''
        CREATE TABLE session_windows (
            session_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            model TEXT,
            created_at INTEGER,
            updated_at INTEGER
        )
    ''')
    cur.execute('''
        CREATE TABLE transcript_events (
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_json TEXT,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (session_id, seq)
        )
    ''')
    cur.execute(
        'INSERT INTO session_nodes (session_key, current_session_id, entry_json,'
        ' created_at, updated_at, display_name, label, archived_at)'
        ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (key, window,
         json.dumps({"sessionId": window, "updatedAt": node_updated}),
         node_created, node_updated, display_name, None, archived_at))
    cur.execute(
        'INSERT INTO session_windows (session_id, session_key, model,'
        ' created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
        (window, key, model, node_created, node_updated))
    if events is None:
        events = [
            _HEADER,
            _msg("user", "make a simple snake game in python"),
            _msg("assistant", "Here is a snake game in Python:\nimport curses"),
        ]
    for seq, event in enumerate(events):
        cur.execute(
            'INSERT INTO transcript_events (session_id, seq, event_json, created_at)'
            ' VALUES (?, ?, ?, ?)',
            (window, seq, json.dumps(event), _NOW + seq * 1000))
    conn.commit()
    conn.close()


def _base(tmp_path):
    """OpenclawAgent's base_path is the agents/ tree (like the real layout)."""
    return tmp_path / 'agents'


def _make(tmp_path, agent_id='main', key=None, **kw):
    base = _base(tmp_path)
    db = base / agent_id / 'agent' / 'openclaw-agent.sqlite'
    db.parent.mkdir(parents=True, exist_ok=True)
    _entry_db(agent_id, db, key=key or f'agent:{agent_id}:main', **kw)
    return base


# --- discovery -----------------------------------------------------------

def test_registered_and_named():
    assert 'openclaw' in REGISTRY
    assert REGISTRY['openclaw'].name == 'openclaw'
    assert not REGISTRY['openclaw'].supports_create()


def test_empty_when_no_agents_dir(tmp_path):
    assert OpenclawAgent(tmp_path).sessions() == []


def test_lists_live_session_and_skips_archived(tmp_path):
    base = _make(tmp_path)
    db = base / 'main' / 'agent' / 'openclaw-agent.sqlite'
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO session_nodes (session_key, current_session_id, entry_json,"
        " created_at, updated_at, archived_at) VALUES (?,?,?,?,?,?)",
        ('agent:main:scratch', 'w1',
         json.dumps({"sessionId": "w1"}), _NOW, _NOW + 99999, _NOW))
    conn.commit()
    conn.close()

    ids = [s.id for s in OpenclawAgent(base).sessions()]
    assert 'agent:main:main' in ids
    assert 'agent:main:scratch' not in ids          # archived is hidden


def test_multiple_agents_aggregate(tmp_path):
    base = _base(tmp_path)
    db1 = base / 'main' / 'agent' / 'openclaw-agent.sqlite'
    db2 = base / 'work' / 'agent' / 'openclaw-agent.sqlite'
    db1.parent.mkdir(parents=True)
    db2.parent.mkdir(parents=True)
    _entry_db('main', db1)
    _entry_db('work', db2, key='agent:work:main', display_name='Work')

    sessions = {s.id: s for s in OpenclawAgent(base).sessions()}
    assert set(sessions) == {'agent:main:main', 'agent:work:main'}
    assert sessions['agent:work:main'].name == 'Work'      # display_name used


def test_session_row_fields(tmp_path):
    base = _make(tmp_path, model='openai/gpt-5', cwd=_CWD)
    s = OpenclawAgent(base).sessions()[0]
    assert s.id == 'agent:main:main'
    assert s.name == 'agent:main:main'       # no display_name -> key
    assert s.model == 'openai/gpt-5'
    assert s.path == _CWD
    assert s.message_count == 2              # header is not a message
    assert s.size > 0                        # real content bytes
    assert s.ctime is not None and s.mtime is not None


# --- reading -------------------------------------------------------------

def test_messages_project_text(tmp_path):
    base = _make(tmp_path, events=[
        _HEADER,
        _msg("user", "make a simple snake game in python"),
        _msg("assistant", "Here is a snake game in Python:\nimport curses"),
        _msg("assistant", [
            {"type": "thinking", "thinking": "let me write it"},
            {"type": "text", "text": "and the rest"},
        ]),
        _tool_result(),
    ])
    agent = OpenclawAgent(base)
    msgs = agent.messages('agent:main:main')
    roles = [m.role for m in msgs]
    assert roles == ['user', 'assistant', 'assistant']     # tool excluded
    joined = ' '.join(m.content for m in msgs)
    assert 'thinking' not in joined                          # reasoning hidden
    assert 'let me write it' not in joined
    assert msgs[0].content == 'make a simple snake game in python'


def test_raw_records_is_verbatim(tmp_path):
    base = _make(tmp_path)
    raw = OpenclawAgent(base).raw_records('agent:main:main')
    assert len(raw) == 2
    assert all(r['type'] == 'message' for r in raw)
    # verbatim: the stored usage object survives untouched
    usage = raw[1]['message']['usage']
    assert usage['totalTokens'] == 30
    assert usage['cost']['total'] == 0.0


def test_token_usage_sums_assistant_usage(tmp_path):
    base = _make(tmp_path, events=[
        _HEADER,
        _msg("user", "hi"),
        _msg("assistant", "hello",
             usage={"input": 5, "output": 7, "cacheRead": 1, "cacheWrite": 0,
                    "totalTokens": 12, "cost": {"total": 0.1}}),
        _msg("assistant", "again",
             usage={"input": 2, "output": 3, "cacheRead": 0, "cacheWrite": 0,
                    "totalTokens": 5, "cost": {"total": 0.01}}),
    ])
    usage = OpenclawAgent(base).token_usage('agent:main:main')
    assert usage == {'total': 17, 'input': 7, 'output': 10}


def test_session_info_model_and_timestamps(tmp_path):
    base = _make(tmp_path, model='anthropic/claude-sonnet-4-6')
    info = OpenclawAgent(base).session_info('agent:main:main')
    assert info['model'] == 'anthropic/claude-sonnet-4-6'
    assert info['created'] is not None
    assert info['modified'] is not None


def test_unknown_session_raises(tmp_path):
    base = _make(tmp_path)
    with pytest.raises(SessionNotFound):
        OpenclawAgent(base).messages('agent:main:nope')


def test_missing_base_is_empty_not_error(tmp_path):
    agent = OpenclawAgent(tmp_path / 'does-not-exist')
    assert agent.sessions() == []
    with pytest.raises(SessionNotFound):
        agent.messages('agent:main:main')


# --- common format (export-only) ----------------------------------------

def test_to_common_envelope(tmp_path):
    base = _make(tmp_path)
    doc = OpenclawAgent(base).to_common('agent:main:main')
    assert doc['source'] == 'openclaw/agent:main:main'
    assert [m['role'] for m in doc['context']] == ['user', 'assistant']
    assert doc['context'][0]['content'] == 'make a simple snake game in python'
    assert doc.get('model') == 'openai/gpt-5'
    assert 'raw' in doc and len(doc['raw']) == 2


def test_from_common_refuses(tmp_path):
    base = _make(tmp_path)
    doc = OpenclawAgent(base).to_common('agent:main:main')
    agent = OpenclawAgent(base)
    with pytest.raises(UnsupportedOperation):
        agent.from_common(doc)


# --- env override --------------------------------------------------------

def test_env_state_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLAW_STATE_DIR', str(tmp_path))
    db = tmp_path / 'agents' / 'main' / 'agent' / 'openclaw-agent.sqlite'
    db.parent.mkdir(parents=True)
    _entry_db('main', db)
    agent = OpenclawAgent()           # no base_path -> honours the env var
    try:
        assert [s.id for s in agent.sessions()] == ['agent:main:main']
    finally:
        agent.base_path = agent.default_base_path()
