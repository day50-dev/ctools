import json
import sqlite3
import pytest
from pathlib import Path
from typer.testing import CliRunner
from ctools.cdu import app, count_tokens, format_tokens, get_session_tokens
from ctools.lib import AGENTS

runner = CliRunner()


# --- Token counting tests ---

def test_count_tokens_basic():
    tokens = count_tokens("hello world")
    assert tokens > 0
    assert tokens < 10


def test_count_tokens_empty():
    tokens = count_tokens("")
    assert tokens == 0


def test_count_tokens_code():
    code = "def hello():\n    print('world')"
    tokens = count_tokens(code)
    assert tokens > 0


# --- Format tokens tests ---

def test_format_tokens_small():
    assert format_tokens(42) == "42"


def test_format_tokens_thousands():
    assert format_tokens(1500) == "1.5k"


def test_format_tokens_millions():
    assert format_tokens(2_500_000) == "2.5M"


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
        VALUES (?, 'Test Session', 1700000000000, 1700000060000, 1000, 500, '/tmp')
    ''', (session_id,))

    if messages is None:
        messages = [
            ("user", "Hello world"),
            ("assistant", "Hi there! How can I help?"),
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


@pytest.fixture
def patched_opencode(tmp_path):
    create_test_opencode_db(tmp_path)
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    yield tmp_path
    AGENTS["opencode"].base_path = original


# --- get_session_tokens tests ---

def test_get_session_tokens_opencode(patched_opencode):
    tokens = get_session_tokens("opencode", "ses_test123")
    assert tokens["total"] == 1500
    assert tokens["input"] == 1000
    assert tokens["output"] == 500
    assert tokens["estimated"] is False


def test_get_session_tokens_unknown_agent():
    tokens = get_session_tokens("nonexistent", "ses_123")
    assert tokens == {}


def test_get_session_tokens_not_found(patched_opencode):
    tokens = get_session_tokens("opencode", "ses_nonexistent")
    assert tokens == {}


# --- CLI tests ---

def test_cli_no_args():
    result = runner.invoke(app, [])
    assert result.exit_code == 0


def test_cli_all_agents(patched_opencode):
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "opencode" in result.stdout


def test_cli_agent_sessions(patched_opencode):
    result = runner.invoke(app, ["opencode/"])
    assert result.exit_code == 0
    assert "ses_test123" in result.stdout


def test_cli_session_detail(patched_opencode):
    result = runner.invoke(app, ["opencode/ses_test123"])
    assert result.exit_code == 0
    assert "1.5k" in result.stdout


def test_cli_unknown_agent():
    result = runner.invoke(app, ["nonexistent/"])
    assert result.exit_code == 1
    assert "Unknown agent" in result.stdout


def test_cli_json_output(patched_opencode):
    result = runner.invoke(app, ["--json", "opencode/ses_test123"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["total"] == 1500


def test_cli_json_agent_sessions(patched_opencode):
    result = runner.invoke(app, ["--json", "opencode/"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert data[0]["tokens"] == 1500
