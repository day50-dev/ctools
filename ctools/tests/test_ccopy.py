import json
import sqlite3
import pytest
from typer.testing import CliRunner
from ctools.ccopy import app, copy_conversation
from ctools.lib import AGENTS

runner = CliRunner()


def _make_opencode_conversation_db(tmp_path, session_id="ses_src",
                                   messages=None):
    """A minimal opencode DB with a real user/assistant conversation."""
    if messages is None:
        messages = [
            ("user", "Write a fibonacci function"),
            ("assistant", "Here is a fibonacci function:\n```python\ndef fib(n):\n    return n if n < 2 else fib(n-1) + fib(n-2)\n```"),
        ]
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
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
    """)
    cur.execute("""
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT
        )
    """)
    cur.execute(
        "INSERT INTO session (id, title, time_created, time_updated, tokens_input, tokens_output, directory) VALUES (?, 'Conv', 1700000000000, 1700000060000, 100, 200, '/tmp')",
        (session_id,),
    )
    for i, (role, content) in enumerate(messages):
        msg_id = f"msg_{i}"
        cur.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
            (msg_id, session_id, 1700000000000 + i * 1000, 1700000000000 + i * 1000,
             json.dumps({"role": role, "content": content})))
        cur.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (f"prt_{i}", msg_id, session_id, 1700000000000 + i * 1000,
             1700000000000 + i * 1000, json.dumps({"type": "text", "text": content})))
    conn.commit()
    conn.close()
    return db_path


def test_copies_conversation_to_new_session(tmp_path):
    """ccopy seeds the whole conversation into a new session in the target agent."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = runner.invoke(app, ["opencode/ses_src", "opencode"])
        assert result.exit_code == 0, result.output
        assert "new opencode session" in result.stdout
        new_id = result.stdout.split("new opencode session:")[-1].strip().split()[0]
        assert new_id.startswith("ses_")
        msgs = AGENTS["opencode"].messages(new_id)
        roles = [m.role for m in msgs]
        assert roles == ["user", "assistant"]
        assert any("fib" in m.content for m in msgs)
    finally:
        AGENTS["opencode"].base_path = original


def test_does_not_touch_source(tmp_path):
    """ccopy is a copy: the source session still exists and is unchanged."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        before = [m.content for m in AGENTS["opencode"].messages("ses_src")]
        result = runner.invoke(app, ["opencode/ses_src", "opencode"])
        assert result.exit_code == 0
        after = [m.content for m in AGENTS["opencode"].messages("ses_src")]
        assert before == after
    finally:
        AGENTS["opencode"].base_path = original


def test_unknown_source_agent_is_error():
    result = runner.invoke(app, ["not-an-agent/ses_src", "opencode"])
    assert result.exit_code == 1
    assert "Unknown agent" in result.stdout


def test_missing_session_id_is_error():
    """A source with no session id is an error."""
    result = runner.invoke(app, ["opencode", "opencode"])
    assert result.exit_code == 1
    assert "No session ID" in result.stdout


def test_unknown_destination_agent_is_error(tmp_path):
    """A bare destination that isn't a known agent is an error."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = runner.invoke(app, ["opencode/ses_src", "not-an-agent"])
        assert result.exit_code == 1
        assert "Unknown agent" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


def test_unsupported_destination_is_error(tmp_path):
    """A known agent that can't seed a new session gives a clean error."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        from ctools.agents import get_agent
        dest = next((get_agent(n) for n in AGENTS
                     if get_agent(n) is not None and not get_agent(n).supports_create()), None)
        if dest is None:
            pytest.skip("no agent available that lacks create_session")
        result = runner.invoke(app, ["opencode/ses_src", dest.name])
        assert result.exit_code == 1
        assert "does not support" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


def test_dry_run_writes_nothing(tmp_path):
    """--dry-run reports the plan without creating a session."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        before = {s.id for s in AGENTS["opencode"].sessions()}
        result = runner.invoke(app, ["opencode/ses_src", "opencode", "--dry-run"])
        assert result.exit_code == 0
        assert "Would copy" in result.stdout
        after = {s.id for s in AGENTS["opencode"].sessions()}
        assert before == after
    finally:
        AGENTS["opencode"].base_path = original


def test_version():
    """--version wins over required arguments, GNU style."""
    from ctools import __version__
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == f"ccopy (ctxttools) {__version__}\n"
