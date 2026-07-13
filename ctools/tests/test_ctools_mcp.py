import json
import sqlite3
import pytest
from pathlib import Path
from ctools.ctools_mcp import (
    list_agents, list_sessions, search_sessions,
    export_session, extract_concepts, copy_concepts,
    get_session_concepts,
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
    result = export_session("opencode", "ses_test123")
    assert "ses_test123" in result
    assert "Hello world" in result


def test_export_session_unknown_agent():
    result = export_session("nonexistent", "ses_123")
    assert "Unknown agent" in result


def test_export_session_not_found(patched_opencode):
    result = export_session("opencode", "ses_nonexistent")
    assert "Session not found" in result


def test_export_session_max_messages(patched_opencode):
    result = export_session("opencode", "ses_test123", max_messages=1)
    assert "3 more messages" in result


# --- extract_concepts ---

def test_extract_concepts(patched_concepts):
    result = extract_concepts("opencode", "ses_concepts")
    assert "constraint" in result
    assert "preference" in result


def test_extract_concepts_none(patched_opencode):
    result = extract_concepts("opencode", "ses_test123")
    assert "No concepts found" in result


def test_extract_concepts_unknown_agent():
    result = extract_concepts("nonexistent", "ses_123")
    assert "Unknown agent" in result


# --- get_session_concepts ---

def test_get_session_concepts(patched_concepts):
    result = get_session_concepts("opencode", "ses_concepts")
    assert "constraint" in result
    assert "preference" in result


def test_get_session_concepts_filter_type(patched_concepts):
    result = get_session_concepts("opencode", "ses_concepts", concept_type="constraint")
    assert "constraint" in result
    assert "preference" not in result


def test_get_session_concepts_no_match(patched_opencode):
    result = get_session_concepts("opencode", "ses_test123")
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
    assert "Invalid session reference" in result


def test_copy_concepts_invalid_dest_session_ref(patched_opencode, tmp_path):
    concepts = [{"type": "constraint", "short": "test"}]
    src_file = tmp_path / "concepts.json"
    src_file.write_text(json.dumps(concepts))
    result = copy_concepts(str(src_file), "@bad_agent/ses_123")
    assert "Invalid session reference" in result
