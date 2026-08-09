import json
import sqlite3
import pytest
from pathlib import Path
from typer.testing import CliRunner
from datetime import datetime
from ctools.cdir import (app, Agent, Session, AGENTS, get_opencode_sessions,
                         get_claude_code_sessions, get_pi_sessions, export_pi_session)

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
    assert sessions[0].path == '/home/user/project'  # session working directory
    
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
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Claude" in result.stdout
    assert "Opencode" in result.stdout
    assert "Codex" in result.stdout


def test_cli_no_args_shows_agents():
    """Test that no arguments shows agents."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Opencode" in result.stdout


def test_cli_list_agents_json():
    """Test listing agents in JSON format."""
    result = runner.invoke(app, ["--format", "json"])
    assert result.exit_code == 0
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


# --- -o field selection tests ---

def _make_opencode_db(tmp_path, session_id='ses_test123', title='Test Session'):
    """Create an opencode.db with one session and return the session path."""
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
        VALUES (?, ?, 1700000000000, 1700000060000, 100, 200, '/tmp')
    ''', (session_id, title))
    conn.commit()
    conn.close()
    return db_path


def _run_opencode_cli(tmp_path, args):
    """Run cdir CLI with opencode.base_path pointed at tmp_path."""
    from ctools.cdir import AGENTS
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        return runner.invoke(app, args)
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_output_help():
    """Test -o help documents all available fields."""
    result = runner.invoke(app, ["-o", "help"])
    assert result.exit_code == 0
    for field in ('id', 'name', 'ctime', 'mtime', 'size', 'msgs', 'model', 'path', 'parent'):
        assert field in result.stdout


def test_cli_output_invalid_field():
    """Test -o with an unknown field fails."""
    result = runner.invoke(app, ["-o", "bogus", "opencode/"])
    assert result.exit_code == 1
    assert "Unknown field: bogus" in result.stdout


def test_cli_default_has_header(tmp_path):
    """Test default output has a header row."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["opencode/"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    header = [l for l in lines if 'ID' in l and 'NAME' in l]
    assert header, f"no header row in output:\n{result.stdout}"
    assert 'ses_test123' in result.stdout


def test_cli_long_format_header_and_path(tmp_path):
    """Test -l shows header, only modified date, and path at end."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-l", "opencode/"])
    assert result.exit_code == 0
    assert "MODIFIED" in result.stdout
    assert "SIZE" in result.stdout
    assert "MSGS" in result.stdout
    assert "PATH" in result.stdout
    assert "CREATED" not in result.stdout
    # Path (working directory) at the very end of each data row
    for line in result.stdout.splitlines():
        if 'ses_test123' in line:
            assert line.rstrip().endswith('/tmp')


def test_cli_output_field_selection(tmp_path):
    """Test -o selects and orders specific columns."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-o", "id,name,path", "opencode/"])
    assert result.exit_code == 0
    assert "PATH" in result.stdout
    assert "MODIFIED" not in result.stdout
    assert "SIZE" not in result.stdout
    for line in result.stdout.splitlines():
        if 'ses_test123' in line:
            assert '/tmp' in line


def test_cli_output_field_suppresses_created(tmp_path):
    """Test -o with explicit fields never shows the created date."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-o", "id,name,mtime", "opencode/"])
    assert result.exit_code == 0
    assert "MODIFIED" in result.stdout
    assert "CREATED" not in result.stdout


def test_cli_recursive_has_header(tmp_path):
    """Test -R output has a header row."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-R"])
    assert result.exit_code == 0
    assert "MODIFIED" in result.stdout
    assert "opencode/ses_test123" in result.stdout


# --- Pi Coding Agent tests ---

def _make_pi_session(tmp_path, session_id='019fe37e-d6a2-7344-8a05-5b04d8d40161',
                     name='My Session', cwd='/home/user/project',
                     parent_session=None, extra_entries=None):
    """Create a pi session JSONL file and return its path."""
    dir_path = tmp_path / 'sessions' / '--home-user-project--'
    dir_path.mkdir(parents=True, exist_ok=True)
    session_file = dir_path / f'2026-08-08T22-29-28-354Z_{session_id}.jsonl'
    header = {
        "type": "session", "version": 3, "id": session_id,
        "timestamp": "2026-08-08T22:29:28.354Z", "cwd": cwd,
    }
    if parent_session:
        header["parentSession"] = parent_session
    lines = [
        header,
        {"type": "model_change", "provider": "openrouter",
         "modelId": "moonshotai/kimi-k2.6", "timestamp": "2026-08-08T22:29:28.500Z"},
        {"type": "session_info", "name": name, "timestamp": "2026-08-08T22:29:28.600Z"},
        {"type": "message", "id": "m1", "parentId": "session",
         "timestamp": "2026-08-08T22:29:29Z",
         "message": {"role": "user", "content": "hello pi"}},
        {"type": "message", "id": "m2", "parentId": "m1",
         "timestamp": "2026-08-08T22:30:00Z",
         "message": {"role": "assistant",
                      "content": [{"type": "text", "text": "hi there"}],
                      "model": "moonshotai/kimi-k2.6",
                      "usage": {"input": 10, "output": 5, "totalTokens": 15}}},
    ]
    if extra_entries:
        lines.extend(extra_entries)
    session_file.write_text('\n'.join(json.dumps(l) for l in lines) + '\n')
    return session_file


def _pi_agent(tmp_path):
    """Build a pi Agent pointing at tmp_path."""
    return Agent(
        name='pi', description='Pi Coding Agent',
        base_path=tmp_path, storage_format='jsonl',
        session_pattern='sessions/**/*.jsonl',
    )


def _run_pi_cli(tmp_path, args):
    """Run cdir CLI with pi.base_path pointed at tmp_path."""
    from ctools.cdir import AGENTS
    original = AGENTS['pi'].base_path
    AGENTS['pi'].base_path = tmp_path
    try:
        return runner.invoke(app, args)
    finally:
        AGENTS['pi'].base_path = original


def test_pi_agent_in_registry():
    """Test pi is a registered agent."""
    assert 'pi' in AGENTS
    assert AGENTS['pi'].storage_format == 'jsonl'


def test_get_pi_sessions_empty(tmp_path):
    """Test extracting sessions from an empty pi directory."""
    assert get_pi_sessions(_pi_agent(tmp_path)) == []


def test_get_pi_sessions_with_data(tmp_path):
    """Test extracting a session from a pi JSONL file."""
    session_id = '019fe37e-d6a2-7344-8a05-5b04d8d40161'
    _make_pi_session(tmp_path, session_id)
    sessions = get_pi_sessions(_pi_agent(tmp_path))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == session_id
    assert s.name == 'My Session'  # session_info name wins
    assert s.path == '/home/user/project'  # cwd
    assert s.model == 'moonshotai/kimi-k2.6'
    assert s.message_count == 2
    assert s.size == 15  # totalTokens from usage
    assert s.ctime and s.mtime
    assert s.parent_id is None


def test_get_pi_sessions_name_fallback(tmp_path):
    """Test name falls back to the first user message text."""
    _make_pi_session(tmp_path, name=None)
    sessions = get_pi_sessions(_pi_agent(tmp_path))
    assert sessions[0].name == 'hello pi'


def test_get_pi_sessions_parent_id(tmp_path):
    """Test parentSession header maps to a parent session uuid."""
    parent = '2026-08-01T00-00-00-000Z_aaaa-bbbb-cccc-dddd-eeee'
    _make_pi_session(tmp_path, '11111111-2222-3333-4444-555555555555',
                     name='Fork', parent_session=parent)
    sessions = get_pi_sessions(_pi_agent(tmp_path))
    assert sessions[0].parent_id == 'aaaa-bbbb-cccc-dddd-eeee'


def test_export_pi_session(tmp_path):
    """Test exporting the active branch as user/assistant messages."""
    session_id = '019fe37e-d6a2-7344-8a05-5b04d8d40161'
    _make_pi_session(tmp_path, session_id)
    messages = export_pi_session(_pi_agent(tmp_path), session_id)
    assert [m.role for m in messages] == ['user', 'assistant']
    assert messages[0].content == 'hello pi'
    assert messages[1].content == 'hi there'


def test_export_pi_session_skips_tool_messages(tmp_path):
    """Test tool result messages are excluded from export."""
    session_id = '019fe37e-d6a2-7344-8a05-5b04d8d40161'
    extra = [
        {"type": "message", "id": "m3", "parentId": "m2",
         "timestamp": "2026-08-08T22:30:30Z",
         "message": {"role": "toolResult",
                      "content": [{"type": "tool_result", "text": "ls output"}]}},
    ]
    _make_pi_session(tmp_path, session_id, extra_entries=extra)
    messages = export_pi_session(_pi_agent(tmp_path), session_id)
    assert [m.role for m in messages] == ['user', 'assistant']


def test_export_pi_session_uses_active_branch(tmp_path):
    """Test export follows the newest leaf when a session is forked."""
    session_id = '019fe37e-d6a2-7344-8a05-5b04d8d40161'
    extra = [
        # original branch continues after m2
        {"type": "message", "id": "m3", "parentId": "m2",
         "timestamp": "2026-08-08T22:30:30Z",
         "message": {"role": "user", "content": "keep going"}},
        # fork from m2 (newer) becomes the active branch
        {"type": "message", "id": "m4", "parentId": "m2",
         "timestamp": "2026-08-08T22:31:00Z",
         "message": {"role": "user", "content": "fork point"}},
        {"type": "message", "id": "m5", "parentId": "m4",
         "timestamp": "2026-08-08T22:31:30Z",
         "message": {"role": "assistant", "content": "forked reply"}},
    ]
    _make_pi_session(tmp_path, session_id, extra_entries=extra)
    messages = export_pi_session(_pi_agent(tmp_path), session_id)
    assert [m.content for m in messages] == ['hello pi', 'hi there', 'fork point', 'forked reply']


def test_cli_pi_lists(tmp_path):
    """Test cdir pi/ lists pi sessions with a header."""
    _make_pi_session(tmp_path)
    result = _run_pi_cli(tmp_path, ["pi/"])
    assert result.exit_code == 0
    assert '019fe37e' in result.stdout
    assert 'My Session' in result.stdout


def test_cli_pi_long_shows_cwd(tmp_path):
    """Test cdir -l pi/ shows the working directory as PATH."""
    _make_pi_session(tmp_path)
    result = _run_pi_cli(tmp_path, ["-l", "pi/"])
    assert result.exit_code == 0
    assert "MODIFIED" in result.stdout
    assert "/home/user/project" in result.stdout


def test_cli_pi_export(tmp_path):
    """Test cdir pi/<id> exports the session messages."""
    session_id = '019fe37e-d6a2-7344-8a05-5b04d8d40161'
    _make_pi_session(tmp_path, session_id)
    result = _run_pi_cli(tmp_path, [f"pi/{session_id}"])
    assert result.exit_code == 0
    assert '"role": "user"' in result.stdout
    assert 'hello pi' in result.stdout
