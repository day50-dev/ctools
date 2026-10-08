"""Tests for the Kilo (opencode fork) and Hermes agents."""

import json
import sqlite3

import pytest

from ctools.agents import (
    HermesAgent,
    KiloAgent,
    SessionNotFound,
    get_agent,
)


# --- Registry / lookup ---

def test_kilo_registered():
    agent = get_agent('kilo')
    assert agent is not None
    assert agent.name == 'kilo'
    assert agent.label == 'Kilo'
    assert agent.storage_format == 'sqlite'


def test_hermes_registered():
    agent = get_agent('hermes')
    assert agent is not None
    assert agent.name == 'hermes'
    assert agent.label == 'Hermes'
    assert agent.storage_format == 'sqlite'


def test_kilo_resolves_spellings():
    for spelling in ('kilo', 'Kilo', 'KILO'):
        assert get_agent(spelling).name == 'kilo'


def test_hermes_resolves_spellings():
    for spelling in ('hermes', 'Hermes', 'HERMES'):
        assert get_agent(spelling).name == 'hermes'


# --- Kilo (opencode schema) ---

def _make_kilo_db(tmp_path, session_id='kil_ses_1', messages=None):
    db_path = tmp_path / 'kilo.db'
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT,
            directory TEXT, title TEXT, version TEXT, share_url TEXT,
            time_created INTEGER, time_updated INTEGER, model TEXT,
            tokens_input INTEGER, tokens_output INTEGER
        )
    ''')
    cur.execute('''
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT
        )
    ''')
    cur.execute(
        'INSERT INTO session (id, title, time_created, time_updated, model, tokens_input, tokens_output, directory)'
        ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (session_id, 'Kilo Session', 1700000000000, 1700000060000, 'kilo-model', 100, 200, '/tmp'),
    )
    if messages is None:
        messages = [
            ('user', 'Hello kilo'),
            ('assistant', 'Hi from kilo!'),
        ]
    for i, (role, content) in enumerate(messages):
        msg_id = f'msg_{i}'
        cur.execute(
            'INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)',
            (msg_id, session_id, 1700000000000 + i, 1700000000000 + i, json.dumps({'role': role})),
        )
        cur.execute(
            'INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)',
            (f'prt_{i}', msg_id, session_id, 1700000000000 + i, 1700000000000 + i,
             json.dumps({'type': 'text', 'text': content})),
        )
    conn.commit()
    conn.close()
    return db_path


def test_kilo_lists_sessions(tmp_path):
    _make_kilo_db(tmp_path)
    agent = KiloAgent(tmp_path)
    sessions = agent.sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == 'kil_ses_1'
    assert s.name == 'Kilo Session'
    assert s.model == 'kilo-model'
    assert s.size > 0  # stored message + part content bytes
    assert s.tokens == 300  # 100 + 200
    assert s.message_count == 2
    assert s.path == '/tmp'


def test_kilo_reads_messages(tmp_path):
    _make_kilo_db(tmp_path)
    agent = KiloAgent(tmp_path)
    msgs = agent.messages('kil_ses_1')
    assert [(m.role, m.content) for m in msgs] == [
        ('user', 'Hello kilo'),
        ('assistant', 'Hi from kilo!'),
    ]


def test_kilo_token_usage(tmp_path):
    _make_kilo_db(tmp_path)
    agent = KiloAgent(tmp_path)
    usage = agent.token_usage('kil_ses_1')
    assert usage == {'total': 300, 'input': 100, 'output': 200}


def test_kilo_lines_are_greppable(tmp_path):
    _make_kilo_db(tmp_path, messages=[('user', 'find the needle')])
    agent = KiloAgent(tmp_path)
    lines = agent.lines('kil_ses_1')
    assert any('needle' in text for _, text in lines)


def test_kilo_missing_db_yields_no_sessions(tmp_path):
    assert KiloAgent(tmp_path).sessions() == []


def test_kilo_unknown_session_raises(tmp_path):
    _make_kilo_db(tmp_path)
    agent = KiloAgent(tmp_path)
    with pytest.raises(SessionNotFound):
        agent.messages('does-not-exist')


# --- Hermes (state.db) ---

def _make_hermes_db(tmp_path, session_id='her_ses_1', rows=None, session_kwargs=None):
    db_path = tmp_path / 'state.db'
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, display_name TEXT,
            title TEXT, model TEXT, started_at REAL NOT NULL, ended_at REAL,
            last_activity_at REAL, input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0,
            cwd TEXT, parent_session_id TEXT, message_count INTEGER DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0
        )
    ''')
    cur.execute('''
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
    ''')
    kwargs = session_kwargs or {}
    cur.execute('''
        INSERT INTO sessions (id, source, display_name, model, started_at, ended_at,
            last_activity_at, input_tokens, output_tokens, reasoning_tokens, cwd,
            parent_session_id, message_count, hidden)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        session_id, kwargs.get('source', 'cli'), kwargs.get('display_name'),
        kwargs.get('model', 'hermes-model'), kwargs.get('started_at', 1700000000.0),
        kwargs.get('ended_at'), kwargs.get('last_activity_at', 1700000060.0),
        kwargs.get('input_tokens', 10), kwargs.get('output_tokens', 20),
        kwargs.get('reasoning_tokens', 5), kwargs.get('cwd', '/tmp'),
        kwargs.get('parent_session_id'), kwargs.get('message_count'),
        kwargs.get('hidden', 0),
    ))
    if rows is None:
        rows = [
            ('user', 'hello hermes', 1700000001.0, 1),
            ('assistant', 'hi there', 1700000002.0, 1),
            ('tool', 'tool output should be excluded from messages()', 1700000003.0, 1),
        ]
    for role, content, ts, active in rows:
        cur.execute(
            'INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, ?, ?, ?, ?)',
            (session_id, role, content, ts, active),
        )
    conn.commit()
    conn.close()
    return db_path


def test_hermes_lists_sessions(tmp_path):
    _make_hermes_db(tmp_path)
    agent = HermesAgent(tmp_path)
    sessions = agent.sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == 'her_ses_1'
    assert s.model == 'hermes-model'
    assert s.size > 0  # stored message content bytes
    assert s.tokens == 30  # 10 + 20
    assert s.path == '/tmp'
    assert s.ctime is not None
    assert s.mtime is not None


def test_hermes_uses_title_or_display_name(tmp_path):
    _make_hermes_db(tmp_path, session_kwargs={'display_name': 'My Chat'})
    s = HermesAgent(tmp_path).sessions()[0]
    assert s.name == 'My Chat'


def test_hermes_excludes_hidden_sessions(tmp_path):
    _make_hermes_db(tmp_path, session_id='hidden_one', session_kwargs={'hidden': 1})
    assert HermesAgent(tmp_path).sessions() == []


def test_hermes_reads_conversation_only(tmp_path):
    _make_hermes_db(tmp_path)
    agent = HermesAgent(tmp_path)
    msgs = agent.messages('her_ses_1')
    assert [(m.role, m.content) for m in msgs] == [
        ('user', 'hello hermes'),
        ('assistant', 'hi there'),
    ]


def test_hermes_raw_includes_tool(tmp_path):
    _make_hermes_db(tmp_path)
    agent = HermesAgent(tmp_path)
    raw = agent.raw_messages('her_ses_1')
    assert any(m.role == 'tool' for m in raw)


def test_hermes_skips_inactive_messages(tmp_path):
    _make_hermes_db(tmp_path, rows=[
        ('user', 'visible', 1700000001.0, 1),
        ('user', 'inactive hidden', 1700000002.0, 0),
    ])
    agent = HermesAgent(tmp_path)
    contents = [m.content for m in agent.messages('her_ses_1')]
    assert 'visible' in contents
    assert 'inactive hidden' not in contents


def test_hermes_decodes_json_prefixed_multimodal(tmp_path):
    blob = '\x00json:' + json.dumps([{'type': 'text', 'text': 'multi part text'}])
    _make_hermes_db(tmp_path, rows=[('user', blob, 1700000001.0, 1)])
    agent = HermesAgent(tmp_path)
    contents = [m.content for m in agent.messages('her_ses_1')]
    assert 'multi part text' in contents


def test_hermes_token_usage_includes_reasoning(tmp_path):
    _make_hermes_db(tmp_path)
    agent = HermesAgent(tmp_path)
    usage = agent.token_usage('her_ses_1')
    assert usage == {'total': 35, 'input': 10, 'output': 20}


def test_hermes_missing_db_yields_no_sessions(tmp_path):
    assert HermesAgent(tmp_path).sessions() == []


def test_hermes_unknown_session_raises(tmp_path):
    _make_hermes_db(tmp_path)
    agent = HermesAgent(tmp_path)
    with pytest.raises(SessionNotFound):
        agent.messages('does-not-exist')
