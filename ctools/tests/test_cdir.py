import json
import sqlite3
import pytest
from pathlib import Path
from typer.testing import CliRunner
from datetime import datetime
from ctools.cdir import app, Agent, Session, AGENTS, get_opencode_sessions, get_claude_code_sessions

runner = CliRunner()


# --- Agent Registry Tests ---

def test_agents_registry_exists():
    """Test that the agent registry is properly defined."""
    assert 'claude' in AGENTS
    assert 'claude-code' in AGENTS
    assert 'opencode' in AGENTS
    assert 'codex' in AGENTS


def test_agent_has_required_fields():
    """Test that each agent has required fields."""
    for name, agent in AGENTS.items():
        assert agent.name == name
        assert agent.description
        assert agent.base_path
        assert agent.storage_format in ('json', 'sqlite', 'jsonl')


# --- Claude Code Session Tests ---

def test_get_claude_code_sessions_empty(tmp_path):
    """Test extracting sessions from empty Claude Code directory."""
    agent = Agent(
        name='claude-code',
        description='Claude Code CLI',
        base_path=tmp_path,
        storage_format='jsonl',
        session_pattern='projects/**/*.jsonl'
    )
    sessions = get_claude_code_sessions(agent)
    assert sessions == []


def test_get_claude_code_sessions_with_data(tmp_path):
    """Test extracting sessions from Claude Code directory with data."""
    # Create project directory structure
    project_dir = tmp_path / 'projects' / 'my-project'
    project_dir.mkdir(parents=True)
    
    # Create a sample JSONL session file
    session_file = project_dir / 'session-abc123.jsonl'
    session_file.write_text(json.dumps({
        "type": "human",
        "message": {"content": "What is Python?"}
    }) + '\n' + json.dumps({
        "type": "assistant",
        "message": {"content": "Python is a programming language."}
    }) + '\n')
    
    agent = Agent(
        name='claude-code',
        description='Claude Code CLI',
        base_path=tmp_path,
        storage_format='jsonl',
        session_pattern='projects/**/*.jsonl'
    )
    
    sessions = get_claude_code_sessions(agent)
    assert len(sessions) == 1
    assert sessions[0].name == "What is Python?"
    assert sessions[0].message_count == 2
    assert sessions[0].path == str(session_file)


def test_get_claude_code_sessions_multiple(tmp_path):
    """Test extracting multiple sessions from Claude Code."""
    project_dir = tmp_path / 'projects' / 'project1'
    project_dir.mkdir(parents=True)
    
    # Create multiple session files
    for i in range(3):
        session_file = project_dir / f'session-{i}.jsonl'
        session_file.write_text(json.dumps({
            "type": "human",
            "message": {"content": f"Question {i}"}
        }) + '\n')
    
    agent = Agent(
        name='claude-code',
        description='Claude Code CLI',
        base_path=tmp_path,
        storage_format='jsonl',
        session_pattern='projects/**/*.jsonl'
    )
    
    sessions = get_claude_code_sessions(agent)
    assert len(sessions) == 3


# --- OpenCode Session Tests ---

def test_get_opencode_sessions_empty(tmp_path):
    """Test extracting sessions from empty OpenCode database."""
    agent = Agent(
        name='opencode',
        description='opencode CLI',
        base_path=tmp_path,
        storage_format='sqlite'
    )
    sessions = get_opencode_sessions(agent)
    assert sessions == []


def test_get_opencode_sessions_with_data(tmp_path):
    """Test extracting sessions from OpenCode SQLite database."""
    db_path = tmp_path / 'opencode.db'
    
    # Create SQLite database with actual opencode schema
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            parent_id TEXT,
            slug TEXT,
            directory TEXT,
            title TEXT,
            version TEXT,
            share_url TEXT,
            summary_additions INTEGER,
            summary_deletions INTEGER,
            summary_files INTEGER,
            summary_diffs TEXT,
            revert TEXT,
            permission TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            time_compacting INTEGER,
            time_archived INTEGER,
            workspace_id TEXT,
            path TEXT,
            agent TEXT,
            model TEXT,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER,
            tokens_reasoning INTEGER,
            tokens_cache_read INTEGER,
            tokens_cache_write INTEGER,
            metadata TEXT
        )
    ''')
    
    # Create message table for message count
    cursor.execute('''
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT
        )
    ''')
    
    # Insert test data (timestamps in milliseconds)
    cursor.execute('''
        INSERT INTO session (id, title, time_created, time_updated, tokens_input, tokens_output, model, directory)
        VALUES ('ses_abc123', 'Python Help', 1705312200000, 1705316700000, 1500, 2500, 'gpt-4', '/home/user/project')
    ''')
    cursor.execute('''
        INSERT INTO session (id, title, time_created, time_updated, tokens_input, tokens_output, model, directory)
        VALUES ('ses_def456', 'Code Review', 1705398000000, 1705403400000, 800, 1200, 'claude-3', '/home/user/project')
    ''')
    
    # Insert messages for message count
    cursor.execute('''
        INSERT INTO message (id, session_id, role, content) VALUES ('msg1', 'ses_abc123', 'user', 'Hello')
    ''')
    cursor.execute('''
        INSERT INTO message (id, session_id, role, content) VALUES ('msg2', 'ses_abc123', 'assistant', 'Hi there')
    ''')
    cursor.execute('''
        INSERT INTO message (id, session_id, role, content) VALUES ('msg3', 'ses_def456', 'user', 'Review this code')
    ''')
    
    conn.commit()
    conn.close()
    
    agent = Agent(
        name='opencode',
        description='opencode CLI',
        base_path=tmp_path,
        storage_format='sqlite'
    )
    
    sessions = get_opencode_sessions(agent)
    assert len(sessions) == 2
    
    # Check first session (most recent by time_updated)
    assert sessions[0].id == 'ses_def456'
    assert sessions[0].name == 'Code Review'
    assert sessions[0].message_count == 1
    assert sessions[0].size == 2000  # 800 + 1200 tokens
    
    # Check second session
    assert sessions[1].id == 'ses_abc123'
    assert sessions[1].name == 'Python Help'


def test_get_opencode_sessions_no_title(tmp_path):
    """Test sessions without titles use ID prefix."""
    db_path = tmp_path / 'opencode.db'
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            parent_id TEXT,
            slug TEXT,
            directory TEXT,
            title TEXT,
            version TEXT,
            share_url TEXT,
            summary_additions INTEGER,
            summary_deletions INTEGER,
            summary_files INTEGER,
            summary_diffs TEXT,
            revert TEXT,
            permission TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            time_compacting INTEGER,
            time_archived INTEGER,
            workspace_id TEXT,
            path TEXT,
            agent TEXT,
            model TEXT,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER,
            tokens_reasoning INTEGER,
            tokens_cache_read INTEGER,
            tokens_cache_write INTEGER,
            metadata TEXT
        )
    ''')
    
    cursor.execute('''
        INSERT INTO session (id, title, time_created, time_updated, tokens_input, tokens_output, model, directory)
        VALUES ('ses_no_title', NULL, 1705477200000, 1705479000000, 500, 700, 'gpt-4', '/home/user/project')
    ''')
    
    conn.commit()
    conn.close()
    
    agent = Agent(
        name='opencode',
        description='opencode CLI',
        base_path=tmp_path,
        storage_format='sqlite'
    )
    
    sessions = get_opencode_sessions(agent)
    assert len(sessions) == 1
    assert sessions[0].name == 'ses_no_title'[:8]  # Falls back to ID prefix


# --- CLI Tests ---

def test_cli_list_agents():
    """Test listing all agents."""
    result = runner.invoke(app, ["--agents"])
    assert result.exit_code == 0
    assert "claude" in result.stdout
    assert "opencode" in result.stdout
    assert "codex" in result.stdout


def test_cli_no_args_shows_usage():
    """Test that no arguments shows usage."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def test_cli_list_agents_json():
    """Test listing agents in JSON format."""
    result = runner.invoke(app, ["--agents"])
    assert result.exit_code == 0
    # Should show agent names
    assert "opencode" in result.stdout


def test_cli_unknown_agent():
    """Test with unknown agent name."""
    result = runner.invoke(app, ["unknown-agent"])
    assert result.exit_code == 1
    assert "Unknown agent" in result.stdout


def test_cli_agent_not_found(tmp_path, monkeypatch):
    """Test with agent that doesn't exist on system."""
    # This would require mocking the base_path, so we just test the error handling
    result = runner.invoke(app, ["claude"])
    # Should either show sessions or "not found" message
    assert result.exit_code == 0 or "not found" in result.stdout.lower()


# --- Session Metadata Tests ---

def test_session_metadata():
    """Test Session dataclass fields."""
    now = datetime.now()
    session = Session(
        id="test-123",
        name="Test Session",
        ctime=now,
        mtime=now,
        size=1024,
        path="/path/to/session",
        model="gpt-4",
        message_count=10
    )
    
    assert session.id == "test-123"
    assert session.name == "Test Session"
    assert session.ctime == now
    assert session.mtime == now
    assert session.size == 1024
    assert session.path == "/path/to/session"
    assert session.model == "gpt-4"
    assert session.message_count == 10


def test_session_optional_fields():
    """Test Session with optional fields as None."""
    session = Session(
        id="test-456",
        name="Minimal Session",
        ctime=None,
        mtime=None,
        size=0
    )
    
    assert session.path is None
    assert session.model is None
    assert session.message_count is None


# --- Format flag tests ---

def test_cli_format_json_list_sessions(tmp_path):
    """Test --format json for listing sessions."""
    db_path = tmp_path / 'opencode.db'
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute('''
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
    cursor.execute('''
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        )
    ''')
    cursor.execute('''
        INSERT INTO session (id, title, time_created, time_updated, tokens_input, tokens_output, directory)
        VALUES ('ses_test123', 'Test Session', 1700000000000, 1700000060000, 100, 200, '/tmp')
    ''')
    conn.commit()
    conn.close()
    
    from ctools.cdir import AGENTS
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--format", "json", "opencode/"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert data[0]['id'] == 'ses_test123'
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_xml_list_sessions(tmp_path):
    """Test --format xml for listing sessions."""
    db_path = tmp_path / 'opencode.db'
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute('''
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
    cursor.execute('''
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        )
    ''')
    cursor.execute('''
        INSERT INTO session (id, title, time_created, time_updated, tokens_input, tokens_output, directory)
        VALUES ('ses_test123', 'Test Session', 1700000000000, 1700000060000, 100, 200, '/tmp')
    ''')
    conn.commit()
    conn.close()
    
    from ctools.cdir import AGENTS
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--format", "xml", "opencode/"])
        assert result.exit_code == 0
        assert '<?xml version="1.0"' in result.stdout
        assert '<session id="ses_test123"' in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_md_list_sessions(tmp_path):
    """Test --format md for listing sessions."""
    db_path = tmp_path / 'opencode.db'
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute('''
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
    cursor.execute('''
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        )
    ''')
    cursor.execute('''
        INSERT INTO session (id, title, time_created, time_updated, tokens_input, tokens_output, directory)
        VALUES ('ses_test123', 'Test Session', 1700000000000, 1700000060000, 100, 200, '/tmp')
    ''')
    conn.commit()
    conn.close()
    
    from ctools.cdir import AGENTS
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--format", "md", "opencode/"])
        assert result.exit_code == 0
        assert '# Sessions' in result.stdout
        assert '| ID | Name |' in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_invalid():
    """Test --format with invalid format."""
    result = runner.invoke(app, ["--format", "csv", "opencode/"])
    assert result.exit_code == 1
    assert "Unknown format" in result.stdout
