import json
import sqlite3
import pytest
import typer
from pathlib import Path
from typer.testing import CliRunner
from ctools.ccopy import (
    app, parse_args, extract_concepts_from_messages,
    concepts_to_messages, read_concepts_from_file, write_concepts_to_file,
)
from ctools.lib import Message, AGENTS

runner = CliRunner()


# --- Argument parsing tests ---

def test_parse_args_sessions_only():
    sessions, files = parse_args(["@opencode/ses_abc", "@claude/ses_def"])
    assert sessions == ["opencode/ses_abc", "claude/ses_def"]
    assert files == []


def test_parse_args_files_only():
    sessions, files = parse_args(["a.json", "b.json"])
    assert sessions == []
    assert files == ["a.json", "b.json"]


def test_parse_args_mixed():
    sessions, files = parse_args(["@opencode/ses_abc", "concepts.json"])
    assert sessions == ["opencode/ses_abc"]
    assert files == ["concepts.json"]


def test_parse_args_strips_leading_slash():
    sessions, files = parse_args(["@/opencode/ses_abc"])
    assert sessions == ["/opencode/ses_abc"]


# --- Concept extraction tests ---

def test_extract_concepts_basic():
    messages = [
        Message(role="system", content="Use the following constraint: Use C17 standard"),
        Message(role="user", content="Hello"),
        Message(role="assistant", content="Use the following preference: prefer snake_case"),
    ]
    concepts = extract_concepts_from_messages(messages)
    assert len(concepts) == 2
    assert concepts[0]["type"] == "constraint"
    assert "C17" in concepts[0]["short"]
    assert concepts[1]["type"] == "preference"
    assert "snake_case" in concepts[1]["short"]


def test_extract_concepts_no_match():
    messages = [
        Message(role="user", content="Hello"),
        Message(role="assistant", content="Hi there"),
    ]
    concepts = extract_concepts_from_messages(messages)
    assert len(concepts) == 0


def test_extract_concepts_dedup():
    messages = [
        Message(role="system", content="Use the following constraint: Use C17\nUse the following constraint: Use C17"),
    ]
    concepts = extract_concepts_from_messages(messages)
    assert len(concepts) == 1


def test_extract_concepts_case_insensitive():
    messages = [
        Message(role="system", content="Use the following CONSTRAINT: Use C17"),
    ]
    concepts = extract_concepts_from_messages(messages)
    assert len(concepts) == 1
    assert concepts[0]["type"] == "constraint"


def test_extract_concepts_multiple_per_message():
    messages = [
        Message(role="system", content=(
            "Use the following constraint: Use C17\n"
            "Use the following preference: prefer snake_case\n"
            "Use the following goal: Finish the project"
        )),
    ]
    concepts = extract_concepts_from_messages(messages)
    assert len(concepts) == 3


# --- Concept to messages tests ---

def test_concepts_to_messages():
    concepts = [
        {"type": "constraint", "short": "Use C17"},
        {"type": "preference", "short": "prefer snake_case"},
    ]
    messages = concepts_to_messages(concepts)
    assert len(messages) == 1
    assert messages[0].role == "system"
    assert "Use the following constraint: Use C17" in messages[0].content
    assert "Use the following preference: prefer snake_case" in messages[0].content


def test_concepts_to_messages_empty():
    messages = concepts_to_messages([])
    assert len(messages) == 0


# --- File I/O tests ---

def test_write_read_concepts(tmp_path):
    path = str(tmp_path / "concepts.json")
    concepts = [
        {"type": "constraint", "short": "Use C17"},
        {"type": "preference", "short": "prefer snake_case"},
    ]
    write_concepts_to_file(concepts, path)
    loaded = read_concepts_from_file(path)
    assert len(loaded) == 2
    assert loaded[0]["type"] == "constraint"
    assert loaded[1]["type"] == "preference"


def test_read_concepts_not_found():
    with pytest.raises(typer.Exit):
        read_concepts_from_file("/nonexistent/path.json")


# --- CLI tests ---

def test_cli_no_args():
    result = runner.invoke(app, [])
    assert result.exit_code != 0


def test_cli_extract_and_inject(tmp_path):
    # Create a test opencode DB with concept messages
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("""
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
    cursor.execute("""
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT
        )
    """)

    # Insert a session with concept messages
    cursor.execute(
        "INSERT INTO session (id, title, time_created, time_updated, tokens_input, tokens_output, directory) VALUES (?, 'Test', 1700000000000, 1700000060000, 100, 200, '/tmp')",
        ("ses_test123",),
    )

    concept_content = "Use the following constraint: Use C17\nUse the following preference: prefer snake_case"
    msg_data = json.dumps({"role": "system"})
    cursor.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        ("msg_sys", "ses_test123", 1700000000000, 1700000000000, msg_data),
    )
    part_data = json.dumps({"type": "text", "text": concept_content})
    cursor.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
        ("part_sys", "msg_sys", "ses_test123", 1700000000000, 1700000000000, part_data),
    )

    conn.commit()
    conn.close()

    # Monkeypatch the agent path
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        # Extract concepts from session to file
        concepts_file = str(tmp_path / "extracted.json")
        result = runner.invoke(app, ["@opencode/ses_test123", concepts_file])
        assert result.exit_code == 0
        assert "Extracted" in result.stdout

        # Verify extracted concepts
        with open(concepts_file) as f:
            concepts = json.load(f)
        assert len(concepts) == 2
        assert concepts[0]["type"] == "constraint"
        assert concepts[1]["type"] == "preference"
    finally:
        AGENTS["opencode"].base_path = original
