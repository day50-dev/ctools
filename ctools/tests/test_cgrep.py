import io
import json
import sqlite3
import pytest
from ctools.testing import Runner
from ctools import cgrep
from ctools.cgrep import app, grep_session, parse_path_pattern
from ctools.agents import (REGISTRY as AGENTS, SessionNotFound,
                            GooseAgent, OpencodeAgent, PiAgent)

runner = Runner()


_captured = io.StringIO()


@pytest.fixture(autouse=True)
def _capture_cgrep_stream(monkeypatch):
    """Route cgrep's streaming output into a buffer CliRunner can read.

    cgrep writes default-format matches to the real stdout so they appear
    line-by-line as found; CliRunner captures stdout separately, so tests
    point the module-level stream at a buffer. Read it with _captured.
    """
    _captured.truncate(0)
    _captured.seek(0)
    monkeypatch.setattr(cgrep, "_stream", _captured)
    yield _captured


def _out(result) -> str:
    """Combined stdout: CliRunner's captured stdout plus cgrep's streamed lines."""
    return (result.stdout or "") + _captured.getvalue()


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
        result = runner.invoke(app, ["python", "opencode/ses_test123"])
        assert result.exit_code == 0
        assert "python" in _out(result).lower()
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_list_files(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-l", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        assert "ses_test123" in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_count(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-c", "python", "opencode/ses_test123"])
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
        result = runner.invoke(app, ["-v", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        out = _out(result)
        assert "python" not in out.lower() or "python" in out
    finally:
        AGENTS['opencode'].base_path = original


# --- Filename prefix tests (grep-compatible -h/-H) ---

def test_cli_default_prefixes_session_path(tmp_path):
    """Default output shows agent/session:lineno:line, like grep on many files."""
    create_test_opencode_db(tmp_path, "ses_test123")

    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["python", "opencode/ses_test123"])
        assert result.exit_code == 0
        assert "opencode/ses_test123:3:user: Write some python code" in _out(result)
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_no_filename_suppresses_prefix(tmp_path):
    """-h drops the session path prefix, grep-style single-file output."""
    create_test_opencode_db(tmp_path, "ses_test123")

    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-h", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        out = _out(result)
        assert "opencode/ses_test123:" not in out
        assert "3:user: Write some python code" in out
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_with_filename_forces_prefix(tmp_path):
    """-H keeps the prefix even when -h is also given."""
    create_test_opencode_db(tmp_path, "ses_test123")

    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-h", "-H", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        assert "opencode/ses_test123:3:user: Write some python code" in _out(result)
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_context_lines_are_numbered(tmp_path):
    """Context lines carry grep's `-` separators and line numbers."""
    create_test_opencode_db(tmp_path, "ses_test123")

    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-C1", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        out = _out(result)
        assert "opencode/ses_test123-2-assistant: Hi there! How can I help?" in out
        assert "opencode/ses_test123:3:user: Write some python code" in out
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_count_no_filename(tmp_path):
    """-c with -h prints bare counts, like grep."""
    create_test_opencode_db(tmp_path, "ses_test123")

    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-h", "-c", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        assert "ses_test123" not in result.stdout
        assert ":" not in result.stdout.strip()
    finally:
        AGENTS['opencode'].base_path = original


# --- grep feature parity tests ---

def _with_opencode_db(tmp_path, func):
    """Point the registry at tmp_path, run func, restore."""
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        return func()
    finally:
        AGENTS['opencode'].base_path = original


def _with_registry(tmp_path, monkeypatch, only=("opencode",)):
    """Point only the named agents at tmp_path and every other agent at a
    non-existent directory, so all-agents searches cover exactly those agents."""
    for name, agent in AGENTS.items():
        if name in only:
            agent.base_path = tmp_path
        else:
            agent.base_path = tmp_path / f"__missing__{name}"
    monkeypatch.setattr("ctools.cgrep._all_agents",
                        lambda: [(AGENTS[n], '*') for n in only])


# --- all-agents (no path) tests ---

def test_all_agents_omitted_paths_searches_everything(tmp_path, monkeypatch):
    """cgrep with no path arg searches every installed agent."""
    create_test_opencode_db(tmp_path, "ses_test123")
    _with_registry(tmp_path, monkeypatch, only=("opencode",))
    result = runner.invoke(app, ["python"])
    assert result.exit_code == 0
    out = _out(result)
    assert "python" in out.lower()
    assert "opencode/ses_test123" in out


def test_all_agents_star_path_searches_everything(tmp_path, monkeypatch):
    """An explicit '*' path is the same as omitting paths."""
    create_test_opencode_db(tmp_path, "ses_test123")
    _with_registry(tmp_path, monkeypatch, only=("opencode",))
    result = runner.invoke(app, ["python", "*"])
    assert result.exit_code == 0
    assert "opencode/ses_test123" in _out(result)


def test_all_agents_flag_searches_everything(tmp_path, monkeypatch):
    """-a / --all is equivalent to omitting paths."""
    create_test_opencode_db(tmp_path, "ses_test123")
    _with_registry(tmp_path, monkeypatch, only=("opencode",))
    result = runner.invoke(app, ["-a", "python"])
    assert result.exit_code == 0
    assert "opencode/ses_test123" in _out(result)


def test_all_agents_no_match_exit_one(tmp_path, monkeypatch):
    """With no path arg and no matches, exit status is 1."""
    create_test_opencode_db(tmp_path, "ses_test123")
    _with_registry(tmp_path, monkeypatch, only=("opencode",))
    result = runner.invoke(app, ["zzzz_no_such_thing"])
    assert result.exit_code == 1
    assert "No matches" in _out(result)


def test_all_agents_respects_exclude(tmp_path, monkeypatch):
    """--exclude still applies when searching all agents."""
    create_test_opencode_db(tmp_path, "ses_test123")
    _with_registry(tmp_path, monkeypatch, only=("opencode",))
    result = runner.invoke(app, ["python", "--exclude", "opencode/ses_test123"])
    assert result.exit_code == 1
    out = _out(result)
    assert "No matches" in out
    assert out.strip() == "No matches found"


def test_cli_quiet_exit_status_only(tmp_path):
    """-q prints nothing; the exit status carries the result."""
    create_test_opencode_db(tmp_path, "ses_test123")

    def run():
        match = runner.invoke(app, ["-q", "python", "opencode/ses_test123"])
        assert match.exit_code == 0
        assert match.stdout.strip() == ""

        miss = runner.invoke(app, ["-q", "zzzz_no_such_thing", "opencode/ses_test123"])
        assert miss.exit_code == 1
        assert miss.stdout.strip() == ""

    _with_opencode_db(tmp_path, run)


def test_cli_quiet_still_reports_errors(tmp_path):
    """-q is quiet about results, not about errors."""
    def run():
        result = runner.invoke(app, ["-q", "[invalid", "opencode/ses_test123"])
        assert result.exit_code == 2
        assert "Invalid pattern" in result.stdout

    create_test_opencode_db(tmp_path, "ses_test123")
    _with_opencode_db(tmp_path, run)


def test_cli_max_count(tmp_path):
    """-m N stops after N matches per session."""
    create_test_opencode_db(tmp_path, "ses_test123")

    def run():
        full = runner.invoke(app, ["-c", "python", "opencode/ses_test123"])
        capped = runner.invoke(app, ["-m", "1", "-c", "python", "opencode/ses_test123"])
        assert full.exit_code == 0 and capped.exit_code == 0
        full_count = int(full.stdout.strip().split(":")[-1])
        capped_count = int(capped.stdout.strip().split(":")[-1])
        assert full_count >= 2
        assert capped_count == 1

    _with_opencode_db(tmp_path, run)


def test_cli_max_count_must_be_positive():
    result = runner.invoke(app, ["-m", "0", "python", "opencode/*"])
    assert result.exit_code == 2
    assert "max count" in result.stdout.lower()


def test_cli_word_regexp(tmp_path):
    """-w matches whole words only."""
    create_test_opencode_db(tmp_path, "ses_test123")

    def run():
        substring = runner.invoke(app, ["-c", "cod", "opencode/ses_test123"])
        assert substring.exit_code == 0
        assert int(substring.stdout.strip().split(":")[-1]) >= 1

        whole = runner.invoke(app, ["-w", "-c", "cod", "opencode/ses_test123"])
        assert whole.exit_code == 1

    _with_opencode_db(tmp_path, run)


def test_cli_line_regexp(tmp_path):
    """-x matches whole lines only."""
    create_test_opencode_db(tmp_path, "ses_test123")

    def run():
        partial = runner.invoke(app, ["-c", "user: Hello", "opencode/ses_test123"])
        assert partial.exit_code == 0
        assert int(partial.stdout.strip().split(":")[-1]) >= 1

        whole = runner.invoke(app, ["-x", "-c", "user: Hello", "opencode/ses_test123"])
        assert whole.exit_code == 1

        exact = runner.invoke(app, ["-x", "user: Hello world", "opencode/ses_test123"])
        assert exact.exit_code == 0
        assert "opencode/ses_test123:1:user: Hello world" in _out(exact)

    _with_opencode_db(tmp_path, run)


def test_cli_only_matching(tmp_path):
    """-o prints just the hit text, one per occurrence."""
    create_test_opencode_db(tmp_path, "ses_test123")

    def run():
        result = runner.invoke(app, ["-o", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        lines = [l for l in _out(result).splitlines() if l]
        assert lines == [
            "opencode/ses_test123:3:python",
            "opencode/ses_test123:4:python",
            "opencode/ses_test123:5:python",
        ]

    _with_opencode_db(tmp_path, run)


def test_cli_only_matching_count_is_occurrences(tmp_path):
    """With -o, -c counts hits rather than matching lines."""
    create_test_opencode_db(tmp_path, "ses_test123")

    def run():
        result = runner.invoke(app, ["-o", "-c", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        assert int(result.stdout.strip().split(":")[-1]) == 3

    _with_opencode_db(tmp_path, run)


def test_cli_fixed_strings(tmp_path):
    """-F treats the pattern literally, including regex metacharacters."""
    create_test_opencode_db(tmp_path, "ses_test123")

    def run():
        # As a regex, '.' matches the 'l' in hello.
        regex = runner.invoke(app, ["h.llo", "opencode/ses_test123"])
        assert regex.exit_code == 0
        assert "print('hello')" in _out(regex)

        # As a fixed string, 'h.llo' is not present.
        fixed = runner.invoke(app, ["-F", "h.llo", "opencode/ses_test123"])
        assert fixed.exit_code == 1

        # An invalid regex is an error without -F, and a literal with it.
        bad = runner.invoke(app, ["[invalid", "opencode/ses_test123"])
        assert bad.exit_code == 2
        fixed_bad = runner.invoke(app, ["-F", "[invalid", "opencode/ses_test123"])
        assert fixed_bad.exit_code == 1

    _with_opencode_db(tmp_path, run)


def test_cli_extended_regexp_is_default(tmp_path):
    """-E is accepted as the default regex mode."""
    create_test_opencode_db(tmp_path, "ses_test123")

    def run():
        result = runner.invoke(app, ["-E", "Wri[te]+ some python", "opencode/ses_test123"])
        assert result.exit_code == 0
        assert "opencode/ses_test123:3:user: Write some python code" in _out(result)

    _with_opencode_db(tmp_path, run)


def test_cli_include_exclude_globs(tmp_path):
    """--include/--exclude filter sessions by agent/session path glob."""
    create_test_opencode_db(tmp_path, "ses_test123")

    def run():
        excluded = runner.invoke(app, ["--exclude", "opencode/ses_*", "python", "opencode/ses_test123"])
        assert excluded.exit_code == 1

        included = runner.invoke(app, ["--include", "opencode/ses_test*", "python", "opencode/ses_test123"])
        assert included.exit_code == 0
        assert "opencode/ses_test123" in _out(included)

    _with_opencode_db(tmp_path, run)


def test_cli_missing_agent_data_is_error(tmp_path):
    """Searching an agent that has no data is an error, not a silent miss."""
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path / "does-not-exist"
    try:
        result = runner.invoke(app, ["python", "opencode/*"])
        assert result.exit_code == 2
        assert "not found" in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_context(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-C", "1", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_no_match(tmp_path):
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["nonexistent_xyz", "opencode/ses_test123"])
        assert result.exit_code == 1
        out = _out(result)
        assert "No matches" in out
        assert out.strip() == "No matches found"
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_invalid_pattern():
    result = runner.invoke(app, ["[invalid", "opencode/*"])
    assert result.exit_code == 2
    assert "Invalid pattern" in result.stdout


def test_cli_unknown_agent_exit_status():
    """An unknown agent is an error: exit 2, not a quiet no-match."""
    result = runner.invoke(app, ["pattern", "nonexistent-agent/*"])
    assert result.exit_code == 2
    assert "Unknown agent" in result.stdout


# --- Format flag tests ---

def test_cli_format_json_search(tmp_path):
    """Test --type json for search results."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--type", "json", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) >= 2
        assert data[0]['session'] == 'opencode/ses_test123'
        assert 'line_num' in data[0]
        assert 'line' in data[0]
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_xml_search(tmp_path):
    """Test --type xml for search results."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--type", "xml", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        assert '<?xml version="1.0"' in result.stdout
        assert '<matches>' in result.stdout
        assert '<session id="ses_test123"' in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_md_search(tmp_path):
    """Test --type md for search results."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--type", "md", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        assert '### opencode/ses_test123' in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_json_list_files(tmp_path):
    """Test --type json with -l flag."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--type", "json", "-l", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert 'opencode/ses_test123' in data
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_json_count(tmp_path):
    """Test --type json with -c flag."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--type", "json", "-c", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert 'opencode/ses_test123' in data
        assert data['opencode/ses_test123'] >= 2
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_xml_list_files(tmp_path):
    """Test --type xml with -l flag."""
    create_test_opencode_db(tmp_path, "ses_test123")
    
    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--type", "xml", "-l", "python", "opencode/ses_test123"])
        assert result.exit_code == 0
        assert '<?xml version="1.0"' in result.stdout
        assert '<files match="true">' in result.stdout
    finally:
        AGENTS['opencode'].base_path = original


def test_cli_format_invalid():
    """Test --type with invalid format."""
    result = runner.invoke(app, ["--type", "csv", "python", "opencode/*"])
    assert result.exit_code == 2
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
        assert 'user: what is this thing' in _out(result)
    finally:
        AGENTS['pi'].base_path = original


def test_cli_version():
    """--version prints the GNU-style version line and exits 0."""
    from ctools import __version__
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == f"cgrep (ctxttools) {__version__}\n"
