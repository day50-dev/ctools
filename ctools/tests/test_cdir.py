import json
import sqlite3
import pytest
import ctools.cdir as cdir
from ctools.testing import Runner
from datetime import datetime
from ctools.cdir import app
from ctools.agents import (Session, SessionNotFound, REGISTRY as AGENTS,
                           ClaudeCodeAgent, GooseAgent, OpencodeAgent, PiAgent)

runner = Runner()


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
    agent = ClaudeCodeAgent(tmp_path)
    sessions = agent.sessions()
    assert sessions == []


def test_get_claude_code_sessions_with_data(tmp_path):
    """Test extracting sessions from Claude Code directory with data."""
    # Create project directory structure
    project_dir = tmp_path / 'projects' / 'my-project'
    project_dir.mkdir(parents=True)
    
    # Create a sample JSONL session file
    session_file = project_dir / 'session-abc123.jsonl'
    session_file.write_text(json.dumps({
        "type": "user",
        "cwd": "/home/user/src/project",
        "message": {"content": "What is Python?"}
    }) + '\n' + json.dumps({
        "type": "assistant",
        "message": {"content": "Python is a programming language."}
    }) + '\n')
    
    agent = ClaudeCodeAgent(tmp_path)
    
    sessions = agent.sessions()
    assert len(sessions) == 1
    assert sessions[0].name == "What is Python?"
    assert sessions[0].message_count == 2
    assert sessions[0].path == "/home/user/src/project"  # session working directory


def test_get_claude_code_sessions_path_falls_back_to_file(tmp_path):
    """Test that a transcript without a cwd reports its storage path."""
    project_dir = tmp_path / 'projects' / 'my-project'
    project_dir.mkdir(parents=True)
    session_file = project_dir / 'session-abc123.jsonl'
    session_file.write_text(json.dumps({
        "type": "human",
        "message": {"content": "What is Python?"}
    }) + '\n')

    sessions = ClaudeCodeAgent(tmp_path).sessions()
    assert len(sessions) == 1
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
    
    agent = ClaudeCodeAgent(tmp_path)
    
    sessions = agent.sessions()
    assert len(sessions) == 3


# --- OpenCode Session Tests ---

def test_get_opencode_sessions_empty(tmp_path):
    """Test extracting sessions from empty OpenCode database."""
    agent = OpencodeAgent(tmp_path)
    sessions = agent.sessions()
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
    
    # Create message table for message count + content size (real opencode
    # stores message rows in a `data` JSON column).
    cursor.execute('''
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
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

    # Insert messages for message count (data column carries the content)
    cursor.execute('''
        INSERT INTO message (id, session_id, time_created, time_updated, data)
        VALUES ('msg1', 'ses_abc123', 1705312200000, 1705312200000, '{"role":"user","content":"Hello"}')
    ''')
    cursor.execute('''
        INSERT INTO message (id, session_id, time_created, time_updated, data)
        VALUES ('msg2', 'ses_abc123', 1705312201000, 1705312201000, '{"role":"assistant","content":"Hi there"}')
    ''')
    cursor.execute('''
        INSERT INTO message (id, session_id, time_created, time_updated, data)
        VALUES ('msg3', 'ses_def456', 1705398000000, 1705398000000, '{"role":"user","content":"Review this code"}')
    ''')
    
    conn.commit()
    conn.close()
    
    agent = OpencodeAgent(tmp_path)
    
    sessions = agent.sessions()
    assert len(sessions) == 2
    
    # Check first session (most recent by time_updated)
    assert sessions[0].id == 'ses_def456'
    assert sessions[0].name == 'Code Review'
    assert sessions[0].message_count == 1
    # size is now the stored content bytes; the token count is carried
    # separately (800 + 1200) so a session never reports 0.
    assert sessions[0].size >= len('{"role":"user","content":"Review this code"}')
    assert sessions[0].tokens == 2000
    assert sessions[0].path == '/home/user/project'  # session working directory
    
    # Check second session
    assert sessions[1].id == 'ses_abc123'
    assert sessions[1].name == 'Python Help'
    assert sessions[1].tokens == 4000  # 1500 + 2500


def test_opencode_zero_token_session_reports_content_size(tmp_path):
    """A zero-token session with real content must not report 0 bytes.

    Regression: opencode sometimes records 0 tokens for a session that clearly
    has a body (a 263-message session used to display as "0 B"). Size must be
    the stored content bytes so it is always accurate.
    """
    db_path = tmp_path / 'opencode.db'
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE session (
            id TEXT PRIMARY KEY, title TEXT, time_created INTEGER, time_updated INTEGER,
            tokens_input INTEGER, tokens_output INTEGER, directory TEXT,
            model TEXT, parent_id TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        )
    ''')
    cursor.execute('''
        INSERT INTO session (id, time_created, time_updated, tokens_input, tokens_output, directory)
        VALUES ('ses_zero', 1700000000000, 1700000060000, 0, 0, '/tmp')
    ''')
    # A real body even though tokens are 0.
    cursor.execute('''
        INSERT INTO message (id, session_id, time_created, time_updated, data)
        VALUES ('m1', 'ses_zero', 1700000000000, 1700000000000, ?)
    ''', ('{"role":"user","content":"' + ('x' * 500) + '"}',))
    conn.commit()
    conn.close()

    agent = OpencodeAgent(tmp_path)
    s = agent.sessions()[0]
    assert s.id == 'ses_zero'
    assert s.tokens is None  # no tokens recorded
    assert s.size > 0  # but the content size is real, never 0


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
    
    agent = OpencodeAgent(tmp_path)
    
    sessions = agent.sessions()
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


def test_cli_list_agents_shows_addressable_names():
    """The AGENT column must be copy-pasteable back into `cdir <agent>/`."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "claude-code" in result.stdout
    assert "Claude Code  " not in result.stdout


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


# --- remote refs (ssh://host) ------------------------------------------------

def test_cli_remote_lists_agents(monkeypatch):
    """`cdir ssh://host` reports which agents are installed on the host."""
    import ctools.cdir as cdir
    monkeypatch.setattr(cdir, "_remote_agents_installed",
                        lambda remote: ["opencode", "goose"])
    result = runner.invoke(app, ["ssh://_lorenz"])
    assert result.exit_code == 0
    assert "_lorenz:" in result.stdout
    assert "Found:" in result.stdout
    assert "opencode" in result.stdout
    assert "goose" in result.stdout
    assert "Not Found:" in result.stdout
    assert "pi" in result.stdout  # listed under Not Found


def test_cli_remote_single_agent_present(monkeypatch):
    import ctools.cdir as cdir
    monkeypatch.setattr(cdir, "_remote_agents_installed",
                        lambda remote: ["opencode"])
    result = runner.invoke(app, ["ssh://_lorenz/opencode"])
    assert result.exit_code == 0
    assert "opencode is installed" in result.stdout


def test_cli_remote_single_agent_absent(monkeypatch):
    import ctools.cdir as cdir
    monkeypatch.setattr(cdir, "_remote_agents_installed", lambda remote: [])
    result = runner.invoke(app, ["ssh://_lorenz/pi"])
    assert result.exit_code == 0
    assert "pi is NOT installed" in result.stdout


def test_cli_remote_deep_ref_points_to_ccat(monkeypatch):
    """`ssh://host/agent/session` is a session ref -- not a cdir listing."""
    import ctools.cdir as cdir
    monkeypatch.setattr(cdir, "_remote_agents_installed",
                        lambda remote: ["opencode"])
    result = runner.invoke(app, ["ssh://_lorenz/opencode/ses_abc"])
    assert result.exit_code == 0
    assert "ccat/ccopy job" in result.stdout


def test_cli_remote_unknown_agent(monkeypatch):
    import ctools.cdir as cdir
    monkeypatch.setattr(cdir, "_remote_agents_installed",
                        lambda remote: ["opencode"])
    result = runner.invoke(app, ["ssh://_lorenz/foobar"])
    assert result.exit_code == 1
    assert "Unknown agent" in result.stdout


def test_cli_remote_json(monkeypatch):
    import ctools.cdir as cdir
    monkeypatch.setattr(cdir, "_remote_agents_installed",
                        lambda remote: ["opencode"])
    result = runner.invoke(app, ["ssh://_lorenz", "-f", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    by_name = {row["name"]: row["installed"] for row in data}
    assert by_name["opencode"] is True
    assert by_name["pi"] is False


def test_cli_remote_probe_uses_ssh(monkeypatch):
    """_remote_agents_installed issues one ssh `test -e` per agent, anchored at
    the remote's $HOME."""
    import ctools.cdir as cdir
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "opencode\n"

    def fake_probe(remote, command):
        captured["command"] = command
        return _Proc()

    monkeypatch.setattr(cdir, "_remote_probe", fake_probe)
    from ctools.cli import Remote
    installed = cdir._remote_agents_installed(Remote(host="_lorenz"))
    assert installed == ["opencode"]
    cmd = captured["command"]
    assert "$HOME" in cmd
    assert "test -e" in cmd
    assert "opencode" in cmd


def test_cli_remote_probe_ssh_unavailable(monkeypatch):
    """No ssh binary -> _remote_probe returns None -> empty install list."""
    import ctools.cdir as cdir
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _p: None)
    from ctools.cli import Remote
    assert cdir._remote_agents_installed(Remote(host="_lorenz")) == []



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


def test_cli_resume_hint_shown_for_agent_listing(tmp_path):
    """A single-agent listing tells the user how to resume a session."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["opencode/"])
    assert result.exit_code == 0
    assert "Resume a session with:" in result.stdout
    assert "opencode -s <id>" in result.stdout


def test_cli_resume_hint_not_in_one_line(tmp_path):
    """-1 (machine/script output) carries no prose hint."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-1", "opencode/"])
    assert result.exit_code == 0
    assert "Resume a session with:" not in result.stdout


def test_cli_resume_hint_not_in_recursive(tmp_path):
    """-R (all agents) carries no per-agent hint."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-R"])
    assert result.exit_code == 0
    assert "Resume a session with:" not in result.stdout


def test_cli_resume_hint_respects_override(tmp_path, monkeypatch):
    """A per-machine override in resume-commands.json wins over the built-in
    table."""
    _make_opencode_db(tmp_path)
    cfg = tmp_path / 'resume-commands.json'
    cfg.write_text('{"opencode": "myoc --continue {id}"}')
    monkeypatch.setattr('ctools.agents.Path.home', lambda: tmp_path)  # opencode storage
    monkeypatch.setenv('CTOOLS_CONFIG', str(tmp_path))  # resume-commands config
    result = _run_opencode_cli(tmp_path, ["opencode/"])
    assert result.exit_code == 0
    assert "myoc --continue <id>" in result.stdout


def _make_opencode_db_with_subagent(tmp_path):
    """Create an opencode.db with one parent session and one subagent session.

    The subagent (parent_id set) is the *newer* session, so it would sort
    first under the default time ordering.
    """
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
        INSERT INTO session (id, title, parent_id, time_created, time_updated,
                             tokens_input, tokens_output, directory)
        VALUES ('ses_parent', 'Main work', NULL, 1700000000000,
                1700000000000, 100, 200, '/tmp')
    ''')
    cursor.execute('''
        INSERT INTO session (id, title, parent_id, time_created, time_updated,
                             tokens_input, tokens_output, directory)
        VALUES ('ses_child', 'Explore (@subagent)', 'ses_parent',
                1700000100000, 1700000100000, 50, 60, '/tmp')
    ''')
    conn.commit()
    conn.close()
    return db_path


def test_cli_subagents_hidden_by_default(tmp_path):
    """Subagent sessions are hidden from the default listing; a footer note
    points at -a."""
    _make_opencode_db_with_subagent(tmp_path)
    result = _run_opencode_cli(tmp_path, ["opencode/"])
    assert result.exit_code == 0
    assert 'ses_parent' in result.stdout
    assert 'ses_child' not in result.stdout
    # footer mentions the hidden subagents and the flag
    assert 'subagent(s) hidden' in result.stdout
    assert '-a' in result.stdout


def test_cli_subagents_included_with_a_flag(tmp_path):
    """-a lists subagents nested under their parent with tree connectors."""
    _make_opencode_db_with_subagent(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-a", "opencode/"])
    assert result.exit_code == 0
    assert 'ses_parent' in result.stdout
    assert 'ses_child' in result.stdout
    # tree connector on the child row
    assert '┗━' in result.stdout
    # footer counts both, no "hidden" note
    assert 'subagent(s) hidden' not in result.stdout
    assert '1 session(s), 1 subagent(s)' in result.stdout


def test_cli_subagents_hidden_one_line(tmp_path):
    """-1 default prints only resumable (top-level) session ids."""
    _make_opencode_db_with_subagent(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-1", "opencode/"])
    assert result.exit_code == 0
    assert 'ses_parent' in result.stdout
    assert 'ses_child' not in result.stdout

    result_a = _run_opencode_cli(tmp_path, ["-1", "-a", "opencode/"])
    assert 'ses_parent' in result_a.stdout
    assert 'ses_child' in result_a.stdout


def test_cli_subagents_hidden_recursive(tmp_path):
    """-R (all agents) hides subagents by default; -a includes them."""
    _make_opencode_db_with_subagent(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-R"])
    assert result.exit_code == 0
    assert 'opencode/ses_parent' in result.stdout
    assert 'opencode/ses_child' not in result.stdout

    result_a = _run_opencode_cli(tmp_path, ["-R", "-a"])
    assert 'opencode/ses_parent' in result_a.stdout
    assert 'opencode/ses_child' in result_a.stdout


def test_cli_exact_subagent_id_still_lists(tmp_path):
    """An explicit agent/session_id reference to a subagent is honored —
    hiding only affects browse-style listings."""
    _make_opencode_db_with_subagent(tmp_path)
    result = _run_opencode_cli(tmp_path, ["opencode/ses_child"])
    assert result.exit_code == 0
    assert 'ses_child' in result.stdout


def test_cli_recursive_has_header(tmp_path):
    """Test -R output has a header row."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-R"])
    assert result.exit_code == 0
    assert "MODIFIED" in result.stdout
    assert "opencode/ses_test123" in result.stdout


def _make_opencode_db_two_sizes(tmp_path):
    """Create an opencode.db with one small/newer and one large/older session."""
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
        INSERT INTO session (id, title, time_created, time_updated,
                             tokens_input, tokens_output, directory)
        VALUES ('ses_small', 'Small', 1700000000000, 1700000060000, 10, 10, '/tmp')
    ''')
    cursor.execute('''
        INSERT INTO session (id, title, time_created, time_updated,
                             tokens_input, tokens_output, directory)
        VALUES ('ses_big', 'Big', 1700000000000, 1700000010000, 5000, 5000, '/tmp')
    ''')
    # Content sizes: ses_big has a much larger stored body than ses_small, so
    # a size (bytes) sort puts it first.
    cursor.execute('''
        INSERT INTO message (id, session_id, time_created, time_updated, data)
        VALUES ('m_small', 'ses_small', 1700000000000, 1700000000000, '{"role":"user","content":"hi"}')
    ''')
    cursor.execute('''
        INSERT INTO message (id, session_id, time_created, time_updated, data)
        VALUES ('m_big', 'ses_big', 1700000000000, 1700000000000, ?)
    ''', ('{"role":"user","content":"' + ('x' * 2000) + '"}',))
    conn.commit()
    conn.close()
    return db_path


def test_cli_sort_by_size_flag(tmp_path):
    """-S sorts by size (ls -S), biggest first, regardless of mtime."""
    _make_opencode_db_two_sizes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-S", "-l", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_big') < result.stdout.index('ses_small')


def test_cli_sort_by_size_default_sorts_by_time(tmp_path):
    """Without -S the newest session still leads."""
    _make_opencode_db_two_sizes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-l", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_small') < result.stdout.index('ses_big')


def test_cli_quiet_is_the_suppress_flag(tmp_path):
    """-q/--quiet is the decoration-stripping flag (renamed from -s so that
    -s stays free for ls's size meaning)."""
    _make_opencode_db(tmp_path)
    short = _run_opencode_cli(tmp_path, ["-q", "opencode/"])
    long_form = _run_opencode_cli(tmp_path, ["--quiet", "opencode/"])
    assert short.stdout == long_form.stdout
    assert 'ID' not in short.stdout
    assert 'session(s)' not in short.stdout


def test_cli_size_sort_S_flag(tmp_path):
    """-S sorts by size; -s is no longer a hidden size alias (it was the old
    suppress flag, now -q), so the size sort goes through -S/--size."""
    _make_opencode_db_two_sizes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-S", "-l", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_big') < result.stdout.index('ses_small')


def test_cli_sort_last_flag_wins_size_then_time(tmp_path):
    """With -S -t, the later -t wins: time order (ls style)."""
    _make_opencode_db_two_sizes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-S", "-t", "-l", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_small') < result.stdout.index('ses_big')


def test_cli_sort_bundled_short_flags(tmp_path):
    """argparse bundles short flags: `-lS` means `-l -S`, so the size sort
    must survive inside a bundle (the raw token is `-lS`, not `-S`)."""
    _make_opencode_db_two_sizes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-lS", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_big') < result.stdout.index('ses_small')


def test_cli_sort_bundled_flag_last_wins(tmp_path):
    """Inside and across bundles, the later sort flag still wins: `-lSt`
    sorts by time, `-ltS` by size."""
    _make_opencode_db_two_sizes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-lSt", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_small') < result.stdout.index('ses_big')

    result = _run_opencode_cli(tmp_path, ["-ltS", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_big') < result.stdout.index('ses_small')


def test_cli_sort_last_flag_wins_time_then_size(tmp_path):
    """With -t -S, the later -S wins: size order (ls style)."""
    _make_opencode_db_two_sizes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-t", "-S", "-l", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_big') < result.stdout.index('ses_small')


def _make_opencode_db_glob(tmp_path):
    """Create an opencode.db with three sessions for glob testing."""
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
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT
        )
    ''')
    cursor.execute("""INSERT INTO session (id, title, time_created, time_updated,
        tokens_input, tokens_output, model, directory)
        VALUES ('ses_llcat1', 'llcat JSON export', 1705312200000, 1705316700000,
        100, 200, 'gpt-4', '/home/user/llcat')""")
    cursor.execute("""INSERT INTO session (id, title, time_created, time_updated,
        tokens_input, tokens_output, model, directory)
        VALUES ('ses_abc123', 'Python Help', 1705398000000, 1705403400000,
        800, 1200, 'claude-3', '/home/user/project')""")
    cursor.execute("""INSERT INTO session (id, title, time_created, time_updated,
        tokens_input, tokens_output, model, directory)
        VALUES ('ses_def456', 'llcat debug', 1705400000000, 1705405400000,
        500, 600, 'gpt-4', '/home/user/other')""")
    cursor.execute("""INSERT INTO message (id, session_id, role, content)
        VALUES ('msg1', 'ses_llcat1', 'user', 'hello')""")
    cursor.execute("""INSERT INTO message (id, session_id, role, content)
        VALUES ('msg2', 'ses_abc123', 'user', 'python help please')""")
    conn.commit()
    conn.close()


def test_cli_glob_matches_name(tmp_path):
    """A glob in the session part filters sessions whose name matches."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(tmp_path, ["opencode/*llcat*"])
    assert result.exit_code == 0
    assert 'ses_llcat1' in result.stdout
    assert 'ses_def456' in result.stdout
    assert 'ses_abc123' not in result.stdout


def test_cli_glob_matches_path(tmp_path):
    """A glob matches the session working directory (path) too.

    ses_llcat1 has path='/home/user/llcat'; the pattern '*/llcat' matches the
    path but not the name 'llcat JSON export' (which doesn't start with '/').
    """
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(tmp_path, ["opencode/*/llcat"])
    # Only ses_llcat1's path '/home/user/llcat' matches '*/llcat';
    # its name 'llcat JSON export' does not, so this proves path matching.
    assert 'ses_llcat1' in result.stdout
    assert 'ses_def456' not in result.stdout
    assert 'ses_abc123' not in result.stdout


def test_cli_glob_matches_id(tmp_path):
    """A glob matches the session id itself."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(tmp_path, ["opencode/ses_abc*"])
    assert result.exit_code == 0
    assert 'ses_abc123' in result.stdout
    assert 'ses_llcat1' not in result.stdout


def test_cli_glob_no_match(tmp_path):
    """A glob that matches nothing prints 'No sessions matching'."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(tmp_path, ["opencode/*zzzqqq*"])
    assert result.exit_code == 0
    assert 'No sessions matching' in result.stdout


def test_cli_glob_is_case_insensitive(tmp_path):
    """Filtering matches case-insensitively."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(tmp_path, ["opencode/*LLCAT*"])
    assert result.exit_code == 0
    assert 'ses_llcat1' in result.stdout
    assert 'ses_def456' in result.stdout


def test_cli_exact_id_not_treated_as_glob(tmp_path):
    """A plain session id (no wildcards) is listed as a row (ls file
    semantics), not run through the glob filter (which would print
    'No sessions matching') and not exported as conversation JSON."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(tmp_path, ["opencode/ses_abc123"])
    assert result.exit_code == 0
    assert 'No sessions matching' not in result.stdout
    assert 'ses_abc123' in result.stdout
    assert '"role"' not in result.stdout
    assert '1 session(s)' in result.stdout


def test_cli_single_exact_id_long_lists_row(tmp_path):
    """cdir -l agent/ses_id shows the long row for that session, not the
    conversation (contents are ccat's job)."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-l", "opencode/ses_abc123"])
    assert result.exit_code == 0
    for col in ('MODIFIED', 'SIZE', 'MSGS', 'PATH'):
        assert col in result.stdout
    assert 'ses_abc123' in result.stdout
    assert '"role"' not in result.stdout


def test_cli_one_line_with_glob(tmp_path):
    """-1 with a glob prints matching session refs one per line."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-1", "opencode/*llcat*"])
    assert result.exit_code == 0
    assert 'ses_llcat1' in result.stdout
    assert 'ses_def456' in result.stdout
    assert 'ses_abc123' not in result.stdout


def test_cli_multiple_exact_ids_list_rows(tmp_path):
    """Two bare exact ids are listed as rows (ls semantics), not exported."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(
        tmp_path, ["opencode/ses_abc123", "opencode/ses_llcat1"])
    assert result.exit_code == 0
    assert 'ses_abc123' in result.stdout
    assert 'ses_llcat1' in result.stdout
    assert '"role"' not in result.stdout
    assert '2 session(s)' in result.stdout


def test_cli_multiple_exact_ids_with_long(tmp_path):
    """-l with multiple exact ids shows the long row fields."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(
        tmp_path, ["-l", "opencode/ses_abc123", "opencode/ses_llcat1"])
    assert result.exit_code == 0
    for col in ('MODIFIED', 'SIZE', 'MSGS', 'PATH'):
        assert col in result.stdout


def test_cli_multiple_exact_ids_one_line(tmp_path):
    """-1 with multiple exact ids prints one id per line."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(
        tmp_path, ["-1", "opencode/ses_abc123", "opencode/ses_llcat1"])
    assert result.exit_code == 0
    lines = [l for l in result.stdout.split() if l]
    assert set(lines) == {'ses_abc123', 'ses_llcat1'}


def test_cli_multiple_exact_ids_bad_id_reported(tmp_path):
    """A bad id among good ones is reported; the good ones still list."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(
        tmp_path, ["opencode/ses_abc123", "opencode/ses_bogus"])
    assert result.exit_code == 0
    assert 'No such session: opencode/ses_bogus' in result.stdout


def _run_two_agent_cli(tmp_path, args):
    """Run cdir with opencode and pi base paths pointed at tmp_path."""
    from ctools.cdir import AGENTS
    oc_orig, pi_orig = AGENTS['opencode'].base_path, AGENTS['pi'].base_path
    AGENTS['opencode'].base_path = tmp_path
    AGENTS['pi'].base_path = tmp_path
    try:
        return runner.invoke(app, args)
    finally:
        AGENTS['opencode'].base_path = oc_orig
        AGENTS['pi'].base_path = pi_orig


def _make_pi_session_for_glob(tmp_path):
    """One pi session named 'My Session' to pair with the opencode fixture."""
    _make_pi_session(tmp_path, '019fe37e-d6a2-7344-8a05-5b04d8d40161',
                     name='My Session')


def test_cli_agent_glob_cross_agent(tmp_path):
    """cdir '*/*pattern*' matches sessions across agents, rows prefixed."""
    _make_opencode_db_glob(tmp_path)
    _make_pi_session_for_glob(tmp_path)
    # 'python' matches only the opencode session's name.
    result = _run_two_agent_cli(tmp_path, ["*/*python*"])
    assert result.exit_code == 0
    assert 'opencode/ses_abc123' in result.stdout
    assert 'pi/' not in result.stdout
    assert '1 session(s)' in result.stdout


def test_cli_agent_glob_matches_other_agent_only(tmp_path):
    """A pattern hitting only the pi session lists it with the pi prefix."""
    _make_opencode_db_glob(tmp_path)
    _make_pi_session_for_glob(tmp_path)
    result = _run_two_agent_cli(tmp_path, ["*/*my session*"])
    assert result.exit_code == 0
    assert 'pi/019fe37e-d6a2-7344-8a05-5b04d8d40161' in result.stdout
    assert 'opencode/' not in result.stdout


def test_cli_agent_glob_all_sessions_prefixed(tmp_path):
    """cdir '*/' lists every session of every installed matched agent."""
    _make_opencode_db_glob(tmp_path)
    _make_pi_session_for_glob(tmp_path)
    result = _run_two_agent_cli(tmp_path, ["*/"])
    assert result.exit_code == 0
    assert 'opencode/ses_abc123' in result.stdout
    assert 'opencode/ses_llcat1' in result.stdout
    assert 'pi/019fe37e-d6a2-7344-8a05-5b04d8d40161' in result.stdout
    assert '4 session(s)' in result.stdout


def test_cli_agent_glob_agent_name_segment(tmp_path):
    """The agent segment is itself a glob: 'p*/' selects only pi."""
    _make_opencode_db_glob(tmp_path)
    _make_pi_session_for_glob(tmp_path)
    result = _run_two_agent_cli(tmp_path, ["p*/"])
    assert result.exit_code == 0
    assert 'pi/019fe37e-d6a2-7344-8a05-5b04d8d40161' in result.stdout
    assert 'opencode/' not in result.stdout


def test_cli_agent_glob_exact_id_across_agents(tmp_path):
    """An exact id under a glob agent ('*/<id>') is searched across agents."""
    _make_opencode_db_glob(tmp_path)
    _make_pi_session_for_glob(tmp_path)
    result = _run_two_agent_cli(
        tmp_path, ["*/019fe37e-d6a2-7344-8a05-5b04d8d40161"])
    assert result.exit_code == 0
    assert 'pi/019fe37e-d6a2-7344-8a05-5b04d8d40161' in result.stdout
    assert '1 session(s)' in result.stdout


def test_cli_agent_glob_one_line(tmp_path):
    """-1 with a cross-agent glob prints agent/id refs one per line."""
    _make_opencode_db_glob(tmp_path)
    _make_pi_session_for_glob(tmp_path)
    result = _run_two_agent_cli(tmp_path, ["-1", "*/"])
    assert result.exit_code == 0
    lines = {l for l in result.stdout.split() if l}
    assert 'opencode/ses_abc123' in lines
    assert 'pi/019fe37e-d6a2-7344-8a05-5b04d8d40161' in lines


def test_cli_agent_glob_no_match(tmp_path):
    """A cross-agent glob with no match reports cleanly."""
    _make_opencode_db_glob(tmp_path)
    result = _run_two_agent_cli(tmp_path, ["zz*/nope*"])
    assert result.exit_code == 0
    assert 'No sessions found' in result.stdout


def test_cli_multiple_exact_ids_sorted(tmp_path):
    """Multiple exact ids are sorted like a normal listing (by mtime)."""
    _make_opencode_db_glob(tmp_path)
    # ses_def456 (time_updated 1705405400000) is newer than ses_abc123 (1705403400000).
    result = _run_opencode_cli(
        tmp_path, ["opencode/ses_abc123", "opencode/ses_def456"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_def456') < result.stdout.index('ses_abc123')


def test_cli_mixed_glob_and_exact(tmp_path):
    """A glob and an exact id together: exact listed as a row, glob filtered."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(
        tmp_path, ["opencode/*llcat*", "opencode/ses_abc123"])
    assert result.exit_code == 0
    assert 'ses_llcat1' in result.stdout      # from the glob
    assert 'ses_abc123' in result.stdout      # the exact id


def _make_opencode_db_two_ctimes(tmp_path):
    """Create an opencode.db where mtime order and ctime order disagree."""
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
    # Old creation, recent modification
    cursor.execute('''
        INSERT INTO session (id, title, time_created, time_updated,
                             tokens_input, tokens_output, directory)
        VALUES ('ses_old_created', 'Old created', 1700000000000,
                1700000060000, 10, 10, '/tmp')
    ''')
    # Recent creation, older modification
    cursor.execute('''
        INSERT INTO session (id, title, time_created, time_updated,
                             tokens_input, tokens_output, directory)
        VALUES ('ses_new_created', 'New created', 1700000050000,
                1700000010000, 10, 10, '/tmp')
    ''')
    conn.commit()
    conn.close()
    return db_path


def test_cli_sort_by_ctime(tmp_path):
    """-u sorts by creation time (newest ctime first)."""
    _make_opencode_db_two_ctimes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-u", "-l", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_new_created') < result.stdout.index('ses_old_created')


def test_cli_sort_ctime_long_flag(tmp_path):
    """--ctime is the long form of -u."""
    _make_opencode_db_two_ctimes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["--ctime", "-l", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_new_created') < result.stdout.index('ses_old_created')


def test_cli_sort_by_time_ignores_ctime(tmp_path):
    """Default sort still uses mtime, even when ctimes differ."""
    _make_opencode_db_two_ctimes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-l", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.index('ses_old_created') < result.stdout.index('ses_new_created')


def test_cli_one_line_no_header(tmp_path):
    """-1 prints exactly one session ID per line, no header or footer."""
    _make_opencode_db_two_sizes(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-1", "opencode/"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines == ['ses_small', 'ses_big']
    assert 'Source:' not in result.stdout
    assert 'MODIFIED' not in result.stdout
    assert 'session(s)' not in result.stdout


def test_cli_suppress_strips_all_decorations(tmp_path):
    """-q strips the column headers, count footer, resume hint, Source line,
    and the leading indent, leaving clean aligned rows for scraping."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-q", "opencode/"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    # only the data row remains
    assert lines == ['ses_test123  Test Session']
    # no header, no source, no count, no hint
    assert 'ID' not in result.stdout
    assert 'Source:' not in result.stdout
    assert 'session(s)' not in result.stdout
    assert 'Resume a session with:' not in result.stdout
    # the row starts at column 0 (no leading indent)
    assert lines[0].startswith('ses_test123')
    assert not result.stdout.startswith(' ')


def test_cli_suppress_keeps_data_and_sort(tmp_path):
    """-q changes only the decorations, not the rows or their order."""
    _make_opencode_db_two_sizes(tmp_path)
    plain = _run_opencode_cli(tmp_path, ["-l", "opencode/"])
    supp = _run_opencode_cli(tmp_path, ["-q", "-l", "opencode/"])
    assert supp.exit_code == 0
    # same rows, same order, just without the header/footer/indent
    # (non-terminal output is fixed-gap, never padded)
    supp_rows = [l.rstrip() for l in supp.stdout.splitlines() if l.strip()]
    assert supp_rows == ['ses_small  Small  2023-11-14 14:14  30 B  1  /tmp',
                         'ses_big  Big  2023-11-14 14:13  2.0 KB  1  /tmp']


def test_cli_suppress_long_format(tmp_path):
    """-q -l keeps the long fields but drops the header/count/hint."""
    _make_opencode_db(tmp_path)
    result = _run_opencode_cli(tmp_path, ["-q", "-l", "opencode/"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith('ses_test123')
    assert 'MODIFIED' not in result.stdout
    assert 'session(s)' not in result.stdout
    assert 'Resume a session with:' not in result.stdout


def test_cli_one_line_long_flag(tmp_path):
    """--one-line is the long form of -1."""
    _make_opencode_db(tmp_path)

    result = _run_opencode_cli(tmp_path, ["--one-line", "opencode/"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ['ses_test123']


def test_cli_one_line_recursive(tmp_path):
    """-1 -R prints agent-prefixed IDs, one per line."""
    _make_opencode_db(tmp_path)

    result = _run_opencode_cli(tmp_path, ["-1", "-R"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert 'opencode/ses_test123' in lines
    for line in lines:
        assert '/' in line
        assert 'MODIFIED' not in line


def test_cli_color_always_no_bold(tmp_path):
    """--color always no longer emits ANSI bold (all listed sessions are
    equally resumable now that subagents are hidden by default)."""
    _make_opencode_db(tmp_path)

    result = _run_opencode_cli(tmp_path, ["--color", "always", "opencode/"])
    assert result.exit_code == 0
    assert '\033[1m' not in result.stdout


def test_cli_color_never_has_no_escapes(tmp_path):
    """--color never emits no ANSI escapes."""
    _make_opencode_db(tmp_path)

    result = _run_opencode_cli(tmp_path, ["--color", "never", "opencode/"])
    assert result.exit_code == 0
    assert '\033[' not in result.stdout


def test_cli_color_auto_is_plain_when_piped(tmp_path):
    """--color auto (the default) is plain when stdout is not a tty."""
    _make_opencode_db(tmp_path)

    result = _run_opencode_cli(tmp_path, ["opencode/"])
    assert result.exit_code == 0
    assert '\033[' not in result.stdout


def test_cli_color_invalid_value():
    """An unknown --color value fails with a usage error."""
    result = runner.invoke(app, ["--color", "blink"])
    assert result.exit_code == 2
    assert 'must be one of' in result.output


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
    return PiAgent(tmp_path)


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
    assert _pi_agent(tmp_path).sessions() == []


def test_get_pi_sessions_with_data(tmp_path):
    """Test extracting a session from a pi JSONL file."""
    session_id = '019fe37e-d6a2-7344-8a05-5b04d8d40161'
    _make_pi_session(tmp_path, session_id)
    sessions = _pi_agent(tmp_path).sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == session_id
    assert s.name == 'My Session'  # session_info name wins
    assert s.path == '/home/user/project'  # cwd
    assert s.model == 'moonshotai/kimi-k2.6'
    assert s.message_count == 2
    assert s.size > 0  # stored content bytes (the session file)
    assert s.tokens == 15  # totalTokens from usage
    assert s.ctime and s.mtime
    assert s.parent_id is None


def test_get_pi_sessions_name_fallback(tmp_path):
    """Test name falls back to the first user message text."""
    _make_pi_session(tmp_path, name=None)
    sessions = _pi_agent(tmp_path).sessions()
    assert sessions[0].name == 'hello pi'


def test_get_pi_sessions_parent_id(tmp_path):
    """Test parentSession header maps to a parent session uuid."""
    parent = '2026-08-01T00-00-00-000Z_aaaa-bbbb-cccc-dddd-eeee'
    _make_pi_session(tmp_path, '11111111-2222-3333-4444-555555555555',
                     name='Fork', parent_session=parent)
    sessions = _pi_agent(tmp_path).sessions()
    assert sessions[0].parent_id == 'aaaa-bbbb-cccc-dddd-eeee'


def test_export_pi_session(tmp_path):
    """Test exporting the active branch as user/assistant messages."""
    session_id = '019fe37e-d6a2-7344-8a05-5b04d8d40161'
    _make_pi_session(tmp_path, session_id)
    messages = _pi_agent(tmp_path).messages(session_id)
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
    messages = _pi_agent(tmp_path).messages(session_id)
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
    messages = _pi_agent(tmp_path).messages(session_id)
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
    """cdir pi/<id> lists the session as a row (ls file semantics)."""
    session_id = '019fe37e-d6a2-7344-8a05-5b04d8d40161'
    _make_pi_session(tmp_path, session_id)
    result = _run_pi_cli(tmp_path, [f"pi/{session_id}"])
    assert result.exit_code == 0
    assert session_id in result.stdout
    assert '"role"' not in result.stdout
    assert '1 session(s)' in result.stdout


# --- Goose Session Tests ---

def _make_goose_db(base_path, session_id='20260101_1'):
    """Create a goose sessions.db with the real schema and one session."""
    db_dir = base_path / 'sessions'
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_dir / 'sessions.db'))
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            session_type TEXT NOT NULL DEFAULT 'user',
            working_dir TEXT NOT NULL,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            total_tokens INTEGER,
            provider_name TEXT,
            model_config_json TEXT,
            parent_session_id TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content_json TEXT NOT NULL,
            created_timestamp INTEGER NOT NULL
        )
    ''')
    return conn, cursor


def _goose_agent(base_path):
    return GooseAgent(base_path)


def test_get_goose_sessions_empty(tmp_path):
    """Test extracting sessions from a missing goose data dir."""
    assert _goose_agent(tmp_path).sessions() == []


def test_get_goose_sessions_with_data(tmp_path):
    """Test extracting sessions from the goose SQLite index."""
    conn, cursor = _make_goose_db(tmp_path)
    cursor.execute('''
        INSERT INTO sessions (id, name, description, working_dir, created_at,
                              updated_at, total_tokens, provider_name,
                              model_config_json, parent_session_id)
        VALUES ('20260101_1', 'Monitor Bug', '', '/home/user/project',
                '2026-01-01T12:00:00.123456789+00:00',
                '2026-01-01T13:00:00.987654321+00:00',
                4200, 'openrouter',
                '{"model_name":"gpt-4"}', NULL)
    ''')
    cursor.execute('''
        INSERT INTO sessions (id, name, description, working_dir, parent_session_id)
        VALUES ('20260101_2', '', 'Delegated task', '/home/user/project', '20260101_1')
    ''')
    for i, (role, text) in enumerate([('user', 'hi'), ('assistant', 'hello')]):
        cursor.execute(
            "INSERT INTO messages (session_id, role, content_json, created_timestamp)"
            " VALUES (?, ?, ?, ?)",
            ('20260101_1', role, json.dumps([{"type": "text", "text": text}]), i))
    conn.commit()
    conn.close()

    sessions = _goose_agent(tmp_path).sessions()
    assert len(sessions) == 2

    main = next(s for s in sessions if s.id == '20260101_1')
    assert main.name == 'Monitor Bug'
    assert main.model == 'gpt-4'
    assert main.size > 0  # stored message content bytes
    assert main.tokens == 4200
    assert main.message_count == 2
    assert main.path == '/home/user/project'
    assert main.parent_id is None
    expected_mtime = datetime.fromisoformat(
        '2026-01-01T13:00:00.987654+00:00').astimezone().replace(tzinfo=None)
    assert main.mtime == expected_mtime

    sub = next(s for s in sessions if s.id == '20260101_2')
    assert sub.name == 'Delegated task'
    assert sub.parent_id == '20260101_1'


def test_get_goose_sessions_legacy_jsonl(tmp_path):
    """Test the legacy per-session JSONL fallback."""
    sessions_dir = tmp_path / 'sessions'
    sessions_dir.mkdir()
    (sessions_dir / '20250309_181707.jsonl').write_text(
        '{"description":"legacy chat","id":"20250309_181707",'
        '"created_at":"2025-03-09T18:17:07+00:00",'
        '"updated_at":"2025-03-09T18:20:00+00:00","total_tokens":900}\n'
        '{"role":"user","created":1741537027,"content":[{"Text":{"text":"Hello"}}]}\n'
        '{"role":"assistant","created":1741537030,"content":[{"Text":{"text":"Hi"}}]}\n'
    )
    (sessions_dir / 'empty.jsonl').write_text('')

    sessions = _goose_agent(tmp_path).sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == '20250309_181707'
    assert s.name == 'legacy chat'
    assert s.message_count == 2
    assert s.size > 0  # the legacy .jsonl file size
    assert s.tokens == 900


def test_export_goose_session(tmp_path):
    """Test exporting messages from the goose SQLite index."""
    conn, cursor = _make_goose_db(tmp_path)
    blocks = [
        ('user', [{"type": "text", "text": "run this"}]),
        ('assistant', [{"type": "text", "text": "running"},
                       {"type": "toolRequest", "id": "t1"}]),
        ('assistant', [{"type": "toolResponse", "id": "t1"}]),
        ('assistant', [{"type": "text", "text": "all done"}]),
    ]
    for i, (role, content) in enumerate(blocks):
        cursor.execute(
            "INSERT INTO messages (session_id, role, content_json, created_timestamp)"
            " VALUES (?, ?, ?, ?)",
            ('20260101_1', role, json.dumps(content), i))
    conn.commit()
    conn.close()

    messages = _goose_agent(tmp_path).messages('20260101_1')
    assert [m.content for m in messages] == ['run this', 'running', 'all done']
    assert [m.role for m in messages] == ['user', 'assistant', 'assistant']


def test_export_goose_session_legacy(tmp_path):
    """Test exporting from a legacy JSONL session file."""
    sessions_dir = tmp_path / 'sessions'
    sessions_dir.mkdir()
    (sessions_dir / '20250309_181707.jsonl').write_text(
        '{"description":"legacy chat"}\n'
        '{"role":"user","created":1741537027,"content":[{"Text":{"text":"Hello"}}]}\n'
        '{"role":"assistant","created":1741537030,"content":[{"Text":{"text":"Hi"}}]}\n'
    )

    with pytest.raises(SessionNotFound):
        _goose_agent(tmp_path).messages('20260309_missing')

    messages = _goose_agent(tmp_path).messages('20250309_181707')
    assert [(m.role, m.content) for m in messages] == [
        ('user', 'Hello'), ('assistant', 'Hi')]


def test_cli_goose_lists(tmp_path, monkeypatch):
    """Test cdir goose/ lists sessions from the SQLite index."""
    conn, cursor = _make_goose_db(tmp_path)
    cursor.execute(
        "INSERT INTO sessions (id, name, working_dir) VALUES ('20260101_1', 'Monitor Bug', '/tmp')")
    conn.commit()
    conn.close()

    original = AGENTS['goose'].base_path
    AGENTS['goose'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["goose/"])
    finally:
        AGENTS['goose'].base_path = original
    assert result.exit_code == 0
    assert '20260101_1' in result.stdout
    assert 'Monitor Bug' in result.stdout


# --- Path filtering (cdir agent <path>) ---

def _s(id, name, path):
    return Session(id=id, name=name, ctime=None, mtime=None, size=0, path=path)


def test_session_path_matches_exact():
    s = _s('a', 'A', '/home/user/project')
    assert cdir._session_path_matches(s, '/home/user/project')
    assert cdir._session_path_matches(s, '/home/user/project/')


def test_session_path_matches_descendant():
    """A session in a subdirectory of the query dir is relevant."""
    s = _s('a', 'A', '/home/user/project/src')
    assert cdir._session_path_matches(s, '/home/user/project')


def test_session_path_matches_ancestor_not_relevant():
    """A session started ABOVE the query dir is not relevant to it: otherwise
    every session ever started at ~ would show up in `cdir opencode .` from
    anywhere under ~."""
    # A session ABOVE the query dir is not relevant to it.
    assert not cdir._session_path_matches(_s('a', 'A', '/home/user'),
                                          '/home/user/project')
    assert not cdir._session_path_matches(_s('h', 'H', '/home/chris'),
                                          '/home/chris/day50/ctools')
    # A query at the session's own level or above is fine (descendant case).
    assert cdir._session_path_matches(_s('b', 'B', '/home/user/project'),
                                      '/home/user')


def test_session_path_matches_no_prefix_collision():
    """/project2 is not under /project: no string-prefix false positives."""
    s = _s('a', 'A', '/home/user/project')
    assert not cdir._session_path_matches(s, '/home/user/project2')
    assert not cdir._session_path_matches(s, '/home/other/project')


def test_session_path_matches_case_insensitive():
    s = _s('a', 'A', '/home/user/Project')
    assert cdir._session_path_matches(s, '/home/user/project')


def test_session_path_matches_none_path():
    """A session with no recorded working directory matches nothing."""
    s = _s('a', 'A', None)
    assert not cdir._session_path_matches(s, '/home/user')


def test_session_path_matches_glob():
    """Glob characters turn the path arg into a pattern over the session dir."""
    s = _s('a', 'A', '/home/user/project')
    assert cdir._session_path_matches(s, '/home/user/*')
    assert not cdir._session_path_matches(s, '/home/other/*')


def test_session_path_matches_root():
    """A query at / matches every session (everything is under /); a session
    at / does not match a deeper query (that would be the ancestor case)."""
    assert cdir._session_path_matches(_s('a', 'A', '/home/user'), '/')
    assert not cdir._session_path_matches(_s('b', 'B', '/'), '/home/user')


def test_session_path_matches_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    s = _s('a', 'A', str(tmp_path))
    assert cdir._session_path_matches(s, '~')


def _make_opencode_db_in(tmp_path, rows):
    """opencode.db from (id, title, directory) rows in one real directory."""
    db_path = tmp_path / 'opencode.db'
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT,
            time_created INTEGER, time_updated INTEGER,
            model TEXT, tokens_input INTEGER, tokens_output INTEGER
        )
    ''')
    cursor.execute('''
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            data TEXT
        )
    ''')
    for i, (sid, title, directory) in enumerate(rows):
        cursor.execute(
            'INSERT INTO session (id, title, directory, time_created, time_updated) '
            'VALUES (?, ?, ?, ?, ?)',
            (sid, title, directory, 1700000000000 + i, 1700000000000 + i))
        cursor.execute(
            'INSERT INTO message (id, session_id, time_created, data) '
            'VALUES (?, ?, ?, ?)',
            (f'm_{sid}', sid, 1700000000000,
             '{"role":"user","content":"hi"}'))
    conn.commit()
    conn.close()


def test_cli_path_arg_lists_relevant_sessions(tmp_path):
    """cdir opencode <path>: only the session that ran in that directory."""
    _make_opencode_db_in(tmp_path, [
        ('ses_here', 'Here', str(tmp_path)),
        ('ses_other', 'Other', '/home/user/elsewhere'),
    ])
    result = _run_opencode_cli(tmp_path, ['opencode', str(tmp_path)])
    assert result.exit_code == 0
    assert 'ses_here' in result.stdout
    assert 'ses_other' not in result.stdout


def test_cli_path_dot_two_word_form(tmp_path, monkeypatch):
    """cdir opencode . (two words) is the same listing as cdir opencode/."""
    _make_opencode_db_in(tmp_path, [
        ('ses_here', 'Here', str(tmp_path)),
        ('ses_elsewhere', 'Elsewhere', str(tmp_path) + '-elsewhere'),
    ])
    monkeypatch.chdir(tmp_path)
    two_word = _run_opencode_cli(tmp_path, ['opencode', '.'])
    one_ref = _run_opencode_cli(tmp_path, ['opencode/.'])
    assert two_word.exit_code == 0
    assert 'ses_here' in two_word.stdout
    assert 'ses_elsewhere' not in two_word.stdout
    assert two_word.stdout == one_ref.stdout


def test_cli_path_dot_includes_subdirectories(tmp_path, monkeypatch):
    """A session started in a subdirectory of the query dir is relevant too."""
    _make_opencode_db_in(tmp_path, [
        ('ses_root', 'Root', str(tmp_path)),
        ('ses_sub', 'Sub', str(tmp_path / 'packages')),
    ])
    monkeypatch.chdir(tmp_path)
    result = _run_opencode_cli(tmp_path, ['opencode', '.'])
    assert result.exit_code == 0
    assert 'ses_root' in result.stdout
    assert 'ses_sub' in result.stdout


def test_cli_path_from_subdirectory_scopes_to_subdirectory(tmp_path, monkeypatch):
    """From a subdirectory, cdir opencode . lists sessions of THAT directory,
    not of the project root above it; `..` reaches the root sessions."""
    _make_opencode_db_in(tmp_path, [
        ('ses_root', 'Root', str(tmp_path)),
        ('ses_deep', 'Deep', str(tmp_path / 'sub' / 'deep')),
    ])
    (tmp_path / 'sub' / 'deep').mkdir(parents=True)
    monkeypatch.chdir(tmp_path / 'sub' / 'deep')
    result = _run_opencode_cli(tmp_path, ['opencode', '.'])
    assert result.exit_code == 0
    assert 'ses_deep' in result.stdout
    assert 'ses_root' not in result.stdout

    result_up = _run_opencode_cli(tmp_path, ['opencode', '../..'])
    assert 'ses_deep' in result_up.stdout
    assert 'ses_root' in result_up.stdout


def test_cli_path_home_sessions_do_not_leak_into_project_listing(tmp_path, monkeypatch):
    """Regression (2026-10-17): `cdir . -l` from ~/day50/ctools listed every
    session ever started at ~ (goose's home-dir chats and friends) because
    the ancestor direction treated ~ as relevant to everything under it. A
    project's listing must be the project's sessions, not its home's."""
    home = tmp_path / 'home'
    project = home / 'day50' / 'ctools'
    project.mkdir(parents=True)
    _make_opencode_db_in(tmp_path, [
        ('ses_home_chat', 'New Chat', str(home)),
        ('ses_disk_space', 'look at this computer disk space', str(home)),
        ('ses_project', 'ctools work', str(project)),
    ])
    monkeypatch.chdir(project)
    result = _run_opencode_cli(tmp_path, ['opencode', '.'])
    assert result.exit_code == 0
    assert 'ses_project' in result.stdout
    # the home-directory sessions are NOT relevant to the project dir
    assert 'ses_home_chat' not in result.stdout
    assert 'ses_disk_space' not in result.stdout


def test_cli_path_descendant_matches(tmp_path):
    """A path above the session's directory matches (descendant session)."""
    _make_opencode_db_in(tmp_path, [('ses_sub', 'Sub', str(tmp_path / 'sub'))])
    result = _run_opencode_cli(tmp_path, ['opencode', str(tmp_path)])
    assert result.exit_code == 0
    assert 'ses_sub' in result.stdout


def test_cli_path_no_match(tmp_path):
    _make_opencode_db_in(tmp_path, [('ses_here', 'Here', str(tmp_path))])
    result = _run_opencode_cli(tmp_path, ['opencode', '/home/user/nowhere'])
    assert result.exit_code == 0
    assert 'No sessions matching' in result.stdout


def test_cli_path_tilde_expands(tmp_path, monkeypatch):
    """~ in the path argument is expanded against $HOME."""
    _make_opencode_db_in(tmp_path, [('ses_home', 'Home', str(tmp_path))])
    monkeypatch.setenv('HOME', str(tmp_path))
    result = _run_opencode_cli(tmp_path, ['opencode', '~'])
    assert result.exit_code == 0
    assert 'ses_home' in result.stdout


def test_fit_to_terminal_shrinks_path_first(monkeypatch):
    """A table wider than the terminal: PATH absorbs the squeeze, NAME keeps
    its width, fixed columns are untouched."""
    monkeypatch.setattr(cdir, '_term_width', lambda default=200: 100)
    widths = {'id': 10, 'name': 30, 'mtime': 16, 'size': 5, 'msgs': 4, 'path': 40}
    fields = ['id', 'name', 'mtime', 'size', 'msgs', 'path']
    cdir._fit_to_terminal(widths, fields, True)
    # fixed 35 + gaps 12 = 47; room = 53; flex was 70 -> path = 53 - 30
    assert widths['name'] == 30
    assert widths['path'] == 23
    assert widths['id'] == 10 and widths['mtime'] == 16


def test_fit_to_terminal_drops_path_when_squeeze_insufficient(monkeypatch):
    """Squeezing alone cannot fit the table: PATH is dropped outright, NAME
    keeps its label floor."""
    monkeypatch.setattr(cdir, '_term_width', lambda default=200: 70)
    widths = {'id': 36, 'name': 10, 'mtime': 16, 'size': 8, 'msgs': 4, 'path': 19}
    fields = ['id', 'name', 'mtime', 'size', 'msgs', 'path']
    cdir._fit_to_terminal(widths, fields, True)
    assert 'path' not in fields
    assert widths['name'] == 4  # 'NAME' label floor
    assert 'id' in fields and 'name' in fields


def test_fit_to_terminal_unchanged_when_fits(monkeypatch):
    monkeypatch.setattr(cdir, '_term_width', lambda default=200: 200)
    widths = {'id': 10, 'name': 12}
    cdir._fit_to_terminal(widths, ['id', 'name'], True)
    assert widths == {'id': 10, 'name': 12}


def test_fit_keeps_path_when_name_is_the_anomaly(monkeypatch):
    """A pi session's NAME is the entire first message (hundreds of chars).
    When NAME is wider than PATH, NAME must ellipsize so PATH stays visible —
    PATH is the "which project" column, not the sacrificial one in this case.
    (Regression: the old PATH-first squeeze dropped PATH entirely.)"""
    monkeypatch.setattr(cdir, '_term_width', lambda default=200: 150)
    widths = {'id': 36, 'name': 418, 'mtime': 16, 'size': 6, 'msgs': 4,
              'path': 29}
    fields = ['id', 'name', 'mtime', 'size', 'msgs', 'path']
    cdir._fit_to_terminal(widths, fields, True)
    # PATH survives and is shown in full; NAME is the one that ellipsizes.
    assert 'path' in fields
    assert widths['path'] == 29
    assert widths['name'] < 418
    # fixed 74 + path 29, so NAME fills the rest to fit: 150-74-29 = 47.
    assert widths['name'] == 47
    assert widths['id'] == 36 and widths['mtime'] == 16  # fixed untouched
    # and the table now fits the terminal.
    total = sum(widths[f] for f in fields) + 2 * (len(fields) - 1) + 2
    assert total <= 150


def test_cli_table_fits_narrow_terminal(tmp_path, monkeypatch):
    """A wide -l listing fits a 60-column terminal: PATH is dropped (it would
    be too narrow to read), NAME stays intact, and no row wraps."""
    _make_opencode_db_in(tmp_path, [
        ('ses_abc123', 'Python Help', '/home/user/llcat'),
    ])
    monkeypatch.setattr(cdir, '_term_width', lambda default=200: 60)
    result = _run_opencode_cli(tmp_path, ['opencode', '/home/user/llcat', '-l'])
    assert result.exit_code == 0
    assert 'ses_abc123' in result.stdout
    assert 'Python Help' in result.stdout  # NAME keeps its width
    lines = result.stdout.splitlines()
    # Table rows only: from the ID header to (not including) the count line.
    start = next(i for i, l in enumerate(lines) if l.startswith('  ID'))
    end = next(i for i, l in enumerate(lines) if 'session(s)' in l)
    header = lines[start]
    assert 'PATH' not in header  # dropped, not squeezed to noise
    for line in lines[start:end]:
        assert len(line) <= 60, line


def test_cli_table_ellipsizes_name_when_only_flex(tmp_path, monkeypatch):
    """With only NAME as a flex column (default fields), NAME is ellipsized
    to fit the terminal rather than dropped."""
    _make_opencode_db_in(tmp_path, [
        ('ses_abc123', 'A rather long session name for truncation testing',
         '/home/user/llcat'),
    ])
    monkeypatch.setattr(cdir, '_term_width', lambda default=200: 60)
    result = _run_opencode_cli(tmp_path, ['opencode', '/home/user/llcat'])
    assert result.exit_code == 0
    assert '…' in result.stdout
    lines = result.stdout.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith('  ID'))
    end = next(i for i, l in enumerate(lines) if 'session(s)' in l)
    for line in lines[start:end]:
        assert len(line) <= 60, line


def test_non_tty_never_truncates(tmp_path):
    """Non-terminal stdout (pipe, file, pager): the table is content-sized,
    never squeezed, no ellipses — the pager wraps and scrolls itself."""
    _make_opencode_db_in(tmp_path, [
        ('ses_abc123', 'A' * 80, '/home/user/llcat'),
    ])
    result = _run_opencode_cli(tmp_path, ['opencode', '/home/user/llcat', '-l'])
    assert result.exit_code == 0
    assert 'A' * 80 in result.stdout  # full name, no squeeze
    assert '…' not in result.stdout


def test_agent_listing_fits_narrow_terminal(monkeypatch):
    """The agent registry table squeezes its path column on a narrow
    terminal, and drops it when it would be too narrow to read."""
    monkeypatch.setattr(cdir, '_term_width', lambda default=10**9: 70)
    squeezed = runner.invoke(app, [])
    assert squeezed.exit_code == 0
    for line in squeezed.stdout.splitlines():
        assert len(line) <= 70, line

    monkeypatch.setattr(cdir, '_term_width', lambda default=10**9: 60)
    dropped = runner.invoke(app, [])
    assert dropped.exit_code == 0
    for line in dropped.stdout.splitlines():
        assert len(line) <= 60, line
    # 60 cols: name+desc (58) leaves < 8 for the path -> dropped outright
    assert '/home/' not in dropped.stdout


def test_pi_name_not_truncated_in_data_layer(tmp_path):
    """Names are stored in full (no data-layer truncation): the display
    layer (the terminal-width fitter) owns width, so pipes/pagers see the
    whole name, not an ellipsis."""
    sess_dir = tmp_path / 'sessions' / '--home-user-project--'
    sess_dir.mkdir(parents=True)
    long_text = 'a' * 120
    lines = [
        {"type": "session", "version": 3,
         "id": "eeeeeeee-0000-1111-2222-333333333333",
         "timestamp": "2026-08-08T22:29:28.354Z",
         "cwd": "/home/user/project"},
        {"type": "message", "id": "m1", "parentId": "s",
         "timestamp": "2026-08-08T22:29:29Z",
         "message": {"role": "user", "content": long_text}},
    ]
    (sess_dir / '2026-08-08T22-29-28-354Z_eeeeeeee-0000-1111-2222-333333333333.jsonl')\
        .write_text('\n'.join(json.dumps(l) for l in lines) + '\n')
    session = _pi_agent(tmp_path).sessions()[0]
    assert session.name == long_text  # full text, no ellipsis anywhere


def test_pi_name_multiline_user_text_stays_one_row(tmp_path):
    """A multi-line first user message becomes a single-line name (all
    whitespace normalized, nothing cut), so it can't break table rows."""
    sess_dir = tmp_path / 'sessions' / '--home-user-project--'
    sess_dir.mkdir(parents=True)
    log = "5f76cea8] trickset loaded\n13:01:03INFO: INFO: calling upstream\n13:01:05ERROR: Error"
    lines = [
        {"type": "session", "version": 3,
         "id": "eeeeeeee-0000-1111-2222-333333333333",
         "timestamp": "2026-08-08T22:29:28.354Z",
         "cwd": "/home/user/project"},
        {"type": "message", "id": "m1", "parentId": "s",
         "timestamp": "2026-08-08T22:29:29Z",
         "message": {"role": "user", "content": log}},
    ]
    (sess_dir / '2026-08-08T22-29-28-354Z_eeeeeeee-0000-1111-2222-333333333333.jsonl')\
        .write_text('\n'.join(json.dumps(l) for l in lines) + '\n')
    session = _pi_agent(tmp_path).sessions()[0]
    assert '\n' not in session.name
    assert session.name == '5f76cea8] trickset loaded 13:01:03INFO: INFO: ' \
        'calling upstream 13:01:05ERROR: Error'


def test_merge_path_args_star_then_path():
    """`* .` / `p* .` merge: the path is a filter on any agent selection."""
    assert cdir._merge_path_args(['*', '.']) == ['*/.']
    assert cdir._merge_path_args(['p*', '.']) == ['p*/.']
    assert cdir._merge_path_args(['*']) == ['*']


def test_cli_path_star_two_word_merges(tmp_path):
    """`cdir '*' <path>` (two words) merges into the cross-agent ref `* <path>`:
    the path is a filter on the agent-glob listing, same result as the bare
    path form."""
    _make_opencode_db_glob(tmp_path)   # project, llcat, other dirs
    _make_pi_session_for_glob(tmp_path)  # pi: cwd /home/user/project
    star = _run_two_agent_cli(tmp_path, ['*', '/home/user/project'])
    bare = _run_two_agent_cli(tmp_path, ['/home/user/project'])
    assert star.exit_code == 0
    assert 'ses_abc123' in star.stdout
    assert '019fe37e-d6a2-7344-8a05-5b04d8d40161' in star.stdout
    assert 'ses_llcat1' not in star.stdout   # /home/user/llcat: not relevant
    assert 'ses_def456' not in star.stdout   # /home/user/other: not relevant
    assert star.stdout == bare.stdout


def test_cli_path_bare_cross_agent(tmp_path):
    """A bare path (no agent part) searches every agent, rows prefixed."""
    _make_opencode_db_glob(tmp_path)   # opencode: ses_abc123 in /home/user/project
    _make_pi_session_for_glob(tmp_path)  # pi: cwd /home/user/project
    result = _run_two_agent_cli(tmp_path, ['/home/user/project'])
    assert result.exit_code == 0
    assert 'opencode/ses_abc123' in result.stdout
    assert 'pi/019fe37e-d6a2-7344-8a05-5b04d8d40161' in result.stdout
    assert 'ses_llcat1' not in result.stdout
    assert 'ses_def456' not in result.stdout


def test_cli_path_glob(tmp_path):
    """A glob in the path argument filters like a pattern over the dir."""
    _make_opencode_db_glob(tmp_path)
    result = _run_opencode_cli(tmp_path, ['opencode', '/home/user/*'])
    assert result.exit_code == 0
    assert 'ses_llcat1' in result.stdout
    assert 'ses_abc123' in result.stdout
    assert 'ses_def456' in result.stdout


def test_cli_path_one_line(tmp_path, monkeypatch):
    """-1 with a path filter prints bare session ids, one per line."""
    _make_opencode_db_in(tmp_path, [('ses_here', 'Here', str(tmp_path))])
    monkeypatch.chdir(tmp_path)
    result = _run_opencode_cli(tmp_path, ['-1', 'opencode', '.'])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ['ses_here']


def test_cli_path_exact_id_still_wins(tmp_path):
    """An exact session id is never misread as a path filter."""
    _make_opencode_db_in(tmp_path, [
        ('ses_here', 'Here', str(tmp_path)),
        ('ses_other', 'Other', '/home/user/elsewhere'),
    ])
    result = _run_opencode_cli(tmp_path, ['opencode/ses_other'])
    assert result.exit_code == 0
    assert 'ses_other' in result.stdout
    assert 'No sessions matching' not in result.stdout


def test_cli_version():
    """--version prints the GNU-style version line and exits 0."""
    from ctools import __version__
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == f"cdir (ctxttools) {__version__}\n"
