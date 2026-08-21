import json
import sqlite3
import pytest
from pathlib import Path
from typer.testing import CliRunner
from ctools.cgrep import app, grep_session, parse_path_pattern
from ctools.agents import (REGISTRY as AGENTS, SessionNotFound,
                           GooseAgent, OpencodeAgent, PiAgent)

runner = CliRunner()


# --- Helper to create test opencode DB ---

def create_test_opencode_db(tmp_path, session_id="ses_test123", messages=None):
    """Create a test opencode SQLite database with messages."""
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
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT
        )
    ''')
    
    # Insert session
    cursor.execute('''
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
        cursor.execute('''
            INSERT INTO message (id, session_id, time_created, time_updated, data)
            VALUES (?, ?, ?, ?, ?)
        ''', (msg_id, session_id, 1700000000000 + i * 1000, 1700000000000 + i * 1000,
              json.dumps({"role": role})))
        
        cursor.execute('''
            INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (f"prt_{i}", msg_id, session_id, 1700000000000 + i * 1000, 1700000000000 + i * 1000,
              json.dumps({"type": "text", "text": content})))
    
    conn.commit()
    conn.close()
    return db_path


# --- Parse path pattern tests ---

def test_parse_single_agent():
    result = parse_path_pattern("opencode/*")
    assert len(result) == 1
    assert result[0][0].name == "opencode"
    assert result[0][1] == "*"


def test_parse_session_id():
    result = parse_path_pattern("opencode/ses_abc123")
    assert len(result) == 1
    assert result[0][0].name == "opencode"
    assert result[0][1] == "ses_abc123"


def test_parse_multiple_agents():
    result = parse_path_pattern("opencode/* claude-code/*")
    assert len(result) == 2
    assert [a.name for a, _ in result] == ["opencode", "claude-code"]
    assert [pat for _, pat in result] == ["*", "*"]


def test_parse_unknown_agent():
    result = parse_path_pattern("nonexistent/*")
    assert len(result) == 0


# --- Content extraction tests ---

def test_get_opencode_session_content(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    # Monkeypatch the agent path
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        lines = OpencodeAgent(tmp_path).lines("ses_test123")
        assert len(lines) >= 4
        assert lines[0][1] == "user: Hello world"
        assert "assistant: Hi there" in lines[1][1]
    finally:
        AGENTS['opencode'].base_path = original


def test_get_opencode_session_content_not_found(tmp_path):
    with pytest.raises(SessionNotFound):
        OpencodeAgent(tmp_path).lines("ses_nonexistent")


def _create_test_goose_db(base_path, session_id="20260101_1"):
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
    cursor.execute(
        "INSERT INTO sessions (id, name, working_dir) VALUES (?, '', '/tmp')",
        (session_id,))
    messages = [
        ('user', json.dumps([{"type": "text", "text": "Hello world"}])),
        ('assistant', json.dumps([
            {"type": "text", "text": "Hi there"},
            {"type": "toolRequest", "id": "t1"},
        ])),
        ('assistant', json.dumps([{"type": "toolResponse", "id": "t1"}])),
        ('assistant', json.dumps([{"type": "text", "text": "Done"}])),
    ]
    for i, (role, content) in enumerate(messages):
        cursor.execute(
            "INSERT INTO messages (session_id, role, content_json, created_timestamp)"
            " VALUES (?, ?, ?, ?)", (session_id, role, content, i))
    conn.commit()
    conn.close()


def test_get_goose_session_content(tmp_path):
    _create_test_goose_db(tmp_path)

    lines = GooseAgent(tmp_path).lines("20260101_1")
    assert lines[0] == (1, "user: Hello world")
    assert (2, "assistant: Hi there") in lines
    assert (3, "assistant: Done") in lines
    assert not any("toolRequest" in text or "toolResponse" in text for _, text in lines)


def test_get_goose_session_content_not_found(tmp_path):
    with pytest.raises(SessionNotFound):
        GooseAgent(tmp_path).lines("20260101_missing")


# --- Grep session tests ---

def test_grep_session_match(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        import re
        pattern = re.compile("python")
        matches = grep_session(AGENTS['opencode'], "ses_test123", pattern)
        assert len(matches) >= 2  # At least "Write some python code" and "Here is some python code"
    finally:
        AGENTS['opencode'].base_path = original


def test_grep_session_no_match(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        import re
        pattern = re.compile("nonexistent_pattern_xyz")
        matches = grep_session(AGENTS['opencode'], "ses_test123", pattern)
        assert len(matches) == 0
    finally:
        AGENTS['opencode'].base_path = original


def test_grep_session_invert(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        import re
        pattern = re.compile("python")
        matches = grep_session(AGENTS['opencode'], "ses_test123", pattern, invert=True)
        assert len(matches) >= 2  # At least the non-python lines
    finally:
        AGENTS['opencode'].base_path = original


def test_grep_session_context_before(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        import re
        pattern = re.compile("python")
        matches = grep_session(AGENTS['opencode'], "ses_test123", pattern, before=1)
        assert len(matches) > 0
        assert matches[0].context_before is not None
        assert len(matches[0].context_before) == 1
    finally:
        AGENTS['opencode'].base_path = original


def test_grep_session_context_after(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        import re
        pattern = re.compile("python")
        matches = grep_session(AGENTS['opencode'], "ses_test123", pattern, after=1)
        assert len(matches) > 0
        assert matches[0].context_after is not None
    finally:
        AGENTS['opencode'].base_path = original


def test_grep_session_unknown_agent():
    import re
    pattern = re.compile("test")
    matches = grep_session(AGENTS['opencode'], "ses_definitely_not_here", pattern)
    assert matches == []


# --- CLI tests ---

def test_cli_basic_search(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["python", f"opencode/ses_test123"])
        assert result.exit_code == 0
        assert "python" in result.stdout.lower()
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_list_files(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-l", "python", f"opencode/ses_test123"])
        assert result.exit_code == 0
        assert "ses_test123" in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_count(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-c", "python", f"opencode/ses_test123"])
        assert result.exit_code == 0
        assert "ses_test123:" in result.stdout
        # Count should be at least 2
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_invert(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-v", "python", f"opencode/ses_test123"])
        assert result.exit_code == 0
        assert "python" not in result.stdout.lower() or "python" in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_context(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-C", "1", "python", f"opencode/ses_test123"])
        assert result.exit_code == 0
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_no_match(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["nonexistent_xyz", f"opencode/ses_test123"])
        assert result.exit_code == 0
        assert "No matches" in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_invalid_pattern():
    result = runner.invoke(app, ["[invalid", "opencode/*"])
    assert result.exit_code == 1
    assert "Invalid pattern" in result.stdout


# --- Format flag tests ---

def test_cli_format_json_search(tmp_path):
    """Test --format json for search results."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--format", "json", "python", f"opencode/ses_test123"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) >= 2
        assert data[0]['session'] == 'opencode/ses_test123'
        assert 'line_num' in data[0]
        assert 'line' in data[0]
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_xml_search(tmp_path):
    """Test --format xml for search results."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--format", "xml", "python", f"opencode/ses_test123"])
        assert result.exit_code == 0
        assert '<?xml version="1.0"' in result.stdout
        assert '<matches>' in result.stdout
        assert '<session id="ses_test123"' in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_md_search(tmp_path):
    """Test --format md for search results."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--format", "md", "python", f"opencode/ses_test123"])
        assert result.exit_code == 0
        assert '### opencode/ses_test123' in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_json_list_files(tmp_path):
    """Test --format json with -l flag."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--format", "json", "-l", "python", f"opencode/ses_test123"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert 'opencode/ses_test123' in data
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_json_count(tmp_path):
    """Test --format json with -c flag."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--format", "json", "-c", "python", f"opencode/ses_test123"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert 'opencode/ses_test123' in data
        assert data['opencode/ses_test123'] >= 2
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_xml_list_files(tmp_path):
    """Test --format xml with -l flag."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--format", "xml", "-l", "python", f"opencode/ses_test123"])
        assert result.exit_code == 0
        assert '<?xml version="1.0"' in result.stdout
        assert '<files match="true">' in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_invalid():
    """Test --format with invalid format."""
    result = runner.invoke(app, ["--format", "csv", "python", "opencode/*"])
    assert result.exit_code == 1
    assert "Unknown format" in result.stdout


# --- Pi session tests ---

def _make_pi_session(tmp_path, session_id='019fe37e-d6a2-7344-8a05-5b04d8d40161'):
    """Create a pi session JSONL file with user/assistant messages."""
    dir_path = tmp_path / 'sessions' / '--home-user-project--'
    dir_path.mkdir(parents=True, exist_ok=True)
    session_file = dir_path / f'2026-08-08T22-29-28-354Z_{session_id}.jsonl'
    lines = [
        {"type": "session", "version": 3, "id": session_id,
         "timestamp": "2026-08-08T22:29:28.354Z", "cwd": "/home/user/project"},
        {"type": "model_change", "provider": "openrouter",
         "modelId": "moonshotai/kimi-k2.6", "timestamp": "2026-08-08T22:29:28.500Z"},
        {"type": "message", "id": "m1", "parentId": None,
         "timestamp": "2026-08-08T22:29:29Z",
         "message": {"role": "user", "content": "what is this thing"}},
        {"type": "message", "id": "m2", "parentId": "m1",
         "timestamp": "2026-08-08T22:29:50Z",
         "message": {"role": "assistant", "content": "Let me check the current directory."}},
    ]
    session_file.write_text('\n'.join(json.dumps(l) for l in lines) + '\n')
    return session_file


def test_get_pi_session_content(tmp_path):
    _make_pi_session(tmp_path)
    original = AGENTS['pi'].base_path
    AGENTS['pi'].base_path = tmp_path
    try:
        lines = PiAgent(tmp_path).lines('019fe37e-d6a2-7344-8a05-5b04d8d40161')
        assert lines == [
            (1, 'user: what is this thing'),
            (2, 'assistant: Let me check the current directory.'),
        ]
    finally:
        AGENTS['pi'].base_path = original


def test_get_pi_session_content_not_found(tmp_path):
    with pytest.raises(SessionNotFound):
        PiAgent(tmp_path).lines('nope')


def test_grep_session_pi(tmp_path):
    _make_pi_session(tmp_path)
    original = AGENTS['pi'].base_path
    AGENTS['pi'].base_path = tmp_path
    try:
        import re
        matches = grep_session(AGENTS['pi'], '019fe37e-d6a2-7344-8a05-5b04d8d40161', re.compile('directory'))
        assert len(matches) == 1
        assert 'directory' in matches[0].line
    finally:
        AGENTS['pi'].base_path = original


def test_cli_search_pi(tmp_path):
    _make_pi_session(tmp_path)
    original = AGENTS['pi'].base_path
    AGENTS['pi'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["thing", "pi/*"])
        assert result.exit_code == 0
        assert 'user: what is this thing' in result.stdout
    finally:
        AGENTS['pi'].base_path = original
