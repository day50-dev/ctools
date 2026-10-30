import json
import sqlite3
import pytest
from pathlib import Path
from ctools.ctools_mcp import (
    list_agents, list_sessions, search_sessions,
    export_session, summarize_session, extract_concepts, copy_concepts,
    get_session_concepts, _resolve_session, _is_session_ref,
)
from ctools.lib import AGENTS


# --- Helper to create test opencode DB ---

def create_test_opencode_db(tmp_path, session_id="ses_test123", messages=None):
    db_path = tmp_path / 'opencode.db'
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()

    c.execute('''
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT,
            directory TEXT, title TEXT, version TEXT, share_url TEXT,
            summary_additions INTEGER, summary_deletions INTEGER,
            summary_files INTEGER, summary_diffs TEXT, revert TEXT,
            permission TEXT, time_created INTEGER, time_updated INTEGER,
            time_compacting INTEGER, time_archived INTEGER, workspace_id TEXT,
            path TEXT, agent TEXT, model TEXT, cost REAL,
            tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
            tokens_cache_read INTEGER, tokens_cache_write INTEGER, metadata TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT
        )
    ''')

    c.execute('''
        INSERT INTO session (id, title, time_created, time_updated, tokens_input, tokens_output, directory)
        VALUES (?, 'Test Session', 1700000000000, 1700000060000, 100, 200, '/tmp')
    ''', (session_id,))

    if messages is None:
        messages = [
            ("user", "Hello world"),
            ("assistant", "Hi there! How can I help?"),
            ("user", "Write some python code"),
            ("assistant", "Here is some python code:\n```python\nprint('hello')\n```"),
        ]

    for i, (role, content) in enumerate(messages):
        msg_id = f"msg_{i}"
        c.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
            (msg_id, session_id, 1700000000000 + i * 1000, 1700000000000 + i * 1000,
             json.dumps({"role": role})),
        )
        c.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (f"prt_{i}", msg_id, session_id, 1700000000000 + i * 1000, 1700000000000 + i * 1000,
             json.dumps({"type": "text", "text": content})),
        )

    conn.commit()
    conn.close()
    return db_path


def create_concept_session(tmp_path, session_id="ses_concepts"):
    """Create a session with concept messages."""
    db_path = tmp_path / 'opencode.db'
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()

    c.execute('''
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT,
            directory TEXT, title TEXT, version TEXT, share_url TEXT,
            summary_additions INTEGER, summary_deletions INTEGER,
            summary_files INTEGER, summary_diffs TEXT, revert TEXT,
            permission TEXT, time_created INTEGER, time_updated INTEGER,
            time_compacting INTEGER, time_archived INTEGER, workspace_id TEXT,
            path TEXT, agent TEXT, model TEXT, cost REAL,
            tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
            tokens_cache_read INTEGER, tokens_cache_write INTEGER, metadata TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT
        )
    ''')

    c.execute('''
        INSERT INTO session (id, title, time_created, time_updated, tokens_input, tokens_output, directory)
        VALUES (?, 'Concept Session', 1700000000000, 1700000060000, 100, 200, '/tmp')
    ''', (session_id,))

    msgs = [
        ("system", "Use the following constraint: Use C17 standard"),
        ("user", "Hello"),
        ("assistant", "Use the following preference: prefer snake_case"),
    ]
    for i, (role, content) in enumerate(msgs):
        msg_id = f"msg_{i}"
        c.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
            (msg_id, session_id, 1700000000000 + i * 1000, 1700000000000 + i * 1000,
             json.dumps({"role": role})),
        )
        c.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (f"prt_{i}", msg_id, session_id, 1700000000000 + i * 1000, 1700000000000 + i * 1000,
             json.dumps({"type": "text", "text": content})),
        )

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def patched_opencode(tmp_path):
    """Monkeypatch opencode agent path for the duration of the test."""
    create_test_opencode_db(tmp_path)
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    yield tmp_path
    AGENTS["opencode"].base_path = original


@pytest.fixture
def patched_concepts(tmp_path):
    """Monkeypatch opencode with concept messages."""
    create_concept_session(tmp_path)
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    yield tmp_path
    AGENTS["opencode"].base_path = original


# --- list_agents ---

def test_list_agents():
    result = list_agents()
    assert "opencode" in result
    assert "claude" in result
    assert "claude-code" in result
    assert "codex" in result


# --- list_sessions ---

def test_list_sessions(patched_opencode):
    result = list_sessions("opencode")
    assert "ses_test123" in result
    assert "Test Session" in result


def test_list_sessions_unknown_agent():
    result = list_sessions("nonexistent")
    assert "Unknown agent" in result


def test_list_sessions_no_sessions(tmp_path):
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = list_sessions("opencode")
        assert "No sessions found" in result
    finally:
        AGENTS["opencode"].base_path = original


def test_list_sessions_sort_size(patched_opencode):
    result = list_sessions("opencode", sort="size")
    assert "ses_test123" in result


# --- search_sessions ---

def test_search_sessions(patched_opencode):
    result = search_sessions("python")
    assert "python" in result.lower()


def test_search_sessions_no_match(patched_opencode):
    result = search_sessions("nonexistent_xyz_abc")
    assert "No matches found" in result


def test_search_sessions_invalid_pattern():
    result = search_sessions("[invalid")
    assert "Invalid pattern" in result


def test_search_sessions_unknown_agent():
    result = search_sessions("test", agents="nonexistent")
    assert "Unknown agent" in result


def test_search_sessions_case_insensitive(patched_opencode):
    result = search_sessions("HELLO", ignore_case=True)
    assert "Hello world" in result


def test_search_sessions_max_results(patched_opencode):
    result = search_sessions(".*", max_results=2)
    assert "capped at 2" in result


# --- export_session ---

def test_export_session(patched_opencode):
    result = export_session("opencode/ses_test123")
    assert "ses_test123" in result
    assert "Hello world" in result


def test_export_session_bare_id_with_agent(patched_opencode):
    result = export_session("ses_test123", agent="opencode")
    assert "Hello world" in result


def test_export_session_bare_id_without_agent(patched_opencode):
    result = export_session("ses_test123")
    assert "Cannot determine the agent" in result


def test_export_session_at_prefix(patched_opencode):
    result = export_session("@opencode/ses_test123")
    assert "Hello world" in result


def test_export_session_unknown_agent():
    result = export_session("nonexistent/ses_123")
    assert "Unknown agent" in result


def test_export_session_not_found(patched_opencode):
    result = export_session("opencode/ses_nonexistent")
    assert "Session not found" in result


def test_export_session_max_messages(patched_opencode):
    result = export_session("opencode/ses_test123", max_messages=1)
    assert "3 more messages" in result


# --- summarize_session ---

def test_summarize_session(patched_opencode):
    result = summarize_session("opencode/ses_test123")
    assert "Test Session" in result
    assert "First user message: Hello world" in result
    assert "Last assistant message: Here is some python code" in result


def test_summarize_session_not_found(patched_opencode):
    result = summarize_session("opencode/ses_nonexistent")
    assert "Session not found" in result


# --- extract_concepts ---

def test_extract_concepts(patched_concepts):
    result = extract_concepts("opencode/ses_concepts")
    assert "constraint" in result
    assert "preference" in result


def test_extract_concepts_none(patched_opencode):
    result = extract_concepts("opencode/ses_test123")
    assert "No explicit concept markers" in result
    assert "summarize_session" in result


def test_extract_concepts_unknown_agent():
    result = extract_concepts("nonexistent/ses_123")
    assert "Unknown agent" in result


# --- get_session_concepts ---

def test_get_session_concepts(patched_concepts):
    result = get_session_concepts("opencode/ses_concepts")
    assert "constraint" in result
    assert "preference" in result


def test_get_session_concepts_filter_type(patched_concepts):
    result = get_session_concepts("opencode/ses_concepts", concept_type="constraint")
    assert "constraint" in result
    assert "preference" not in result


def test_get_session_concepts_no_match(patched_opencode):
    result = get_session_concepts("opencode/ses_test123")
    assert "No concepts found" in result


# --- copy_concepts ---

def test_copy_concepts_session_to_file(patched_concepts, tmp_path):
    dest = str(tmp_path / "out.json")
    result = copy_concepts("@opencode/ses_concepts", dest)
    assert "2 concept" in result
    assert Path(dest).exists()
    with open(dest) as f:
        data = json.load(f)
    assert len(data) == 2
    assert data[0]["type"] == "constraint"


def test_copy_concepts_session_to_file_no_at(patched_concepts, tmp_path):
    """A plain 'agent/id' reference works without the @ prefix."""
    dest = str(tmp_path / "out.json")
    result = copy_concepts("opencode/ses_concepts", dest)
    assert "2 concept" in result


def test_is_session_ref():
    assert _is_session_ref("opencode/ses_1")
    assert _is_session_ref("@opencode/ses_1")
    assert not _is_session_ref("out.json")
    assert not _is_session_ref("no_slash")
    assert not _is_session_ref("unknown_agent/ses_1")


def test_resolve_session_unknown_agent(patched_opencode):
    agent, sid, error = _resolve_session("nonexistent/ses_1")
    assert agent is None
    assert "Unknown agent" in error


def test_copy_concepts_file_to_session(patched_opencode, tmp_path):
    concepts = [
        {"type": "constraint", "short": "Use C17"},
        {"type": "preference", "short": "prefer snake_case"},
    ]
    src_file = tmp_path / "concepts.json"
    src_file.write_text(json.dumps(concepts))
    result = copy_concepts(str(src_file), "@opencode/ses_test123")
    assert "2 concept" in result


def test_copy_concepts_invalid_source():
    result = copy_concepts("bad_source", "dest.json")
    assert "Unknown source type" in result


def test_copy_concepts_invalid_destination(patched_opencode, tmp_path):
    concepts = [{"type": "constraint", "short": "test"}]
    src_file = tmp_path / "concepts.json"
    src_file.write_text(json.dumps(concepts))
    result = copy_concepts(str(src_file), "bad_dest")
    assert "Unknown destination type" in result


def test_copy_concepts_source_not_found(patched_opencode):
    result = copy_concepts("@opencode/ses_nonexistent", str(patched_opencode / "out.json"))
    assert "Session not found" in result


def test_copy_concepts_invalid_session_ref():
    result = copy_concepts("@bad_agent/ses_123", "out.json")
    assert "bad_agent" in result


def test_copy_concepts_invalid_dest_session_ref(patched_opencode, tmp_path):
    concepts = [{"type": "constraint", "short": "test"}]
    src_file = tmp_path / "concepts.json"
    src_file.write_text(json.dumps(concepts))
    result = copy_concepts(str(src_file), "@bad_agent/ses_123")
    assert "bad_agent" in result


def test_list_sessions_nests_subagents(tmp_path, monkeypatch):
    """Subagent sessions (parent_id set) are listed nested under their
    parent, not as top-level entries."""
    from ctools.agents import Session
    from datetime import datetime

    class StubAgent:
        name = 'stub'
        base_path = tmp_path

        def exists(self):
            return True

        def sessions(self):
            return [
                Session(id='parent1', name='Parent session',
                        ctime=datetime(2026, 1, 1), mtime=datetime(2026, 1, 3), size=10),
                Session(id='child1', name='Subagent task',
                        ctime=datetime(2026, 1, 1), mtime=datetime(2026, 1, 2), size=5,
                        parent_id='parent1'),
                Session(id='orphan', name='Orphan subagent',
                        ctime=datetime(2026, 1, 1), mtime=datetime(2026, 1, 1), size=5,
                        parent_id='missing'),
            ]

    monkeypatch.setitem(AGENTS, 'stub', StubAgent())
    lines = list_sessions('stub').splitlines()
    parent_i = next(i for i, l in enumerate(lines) if 'parent1' in l)
    child_i = next(i for i, l in enumerate(lines) if 'child1' in l)
    assert child_i == parent_i + 1
    assert '└' in lines[child_i]
    # A subagent whose parent is not in the list is not silently dropped.
    assert 'orphan' in '\n'.join(lines)
