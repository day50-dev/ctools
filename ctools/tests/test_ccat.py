"""Tests for ccat (cat for LLM context windows)."""
import json
import sqlite3
from typer.testing import CliRunner

from ctools.ccat import app
from ctools.agents import REGISTRY as AGENTS

runner = CliRunner()


def _make_opencode_conv_db(tmp_path, session_id, messages):
    """A minimal opencode DB with a user/assistant conversation."""
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
        "INSERT INTO session (id, title, time_created, time_updated, "
        "tokens_input, tokens_output, directory) VALUES (?, 'Conv', "
        "1700000000000, 1700000060000, 100, 200, '/tmp')",
        (session_id,),
    )
    for i, (role, content) in enumerate(messages):
        mid = f"msg_{i}"
        cur.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, session_id, 1700000000000 + i * 1000,
             1700000000000 + i * 1000,
             json.dumps({"role": role, "content": content})))
        cur.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, "
            "time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (f"prt_{i}", mid, session_id, 1700000000000 + i * 1000,
             1700000000000 + i * 1000,
             json.dumps({"type": "text", "text": content})))
    conn.commit()
    conn.close()
    return db_path


def _run_ccat(tmp_path, args):
    """Point opencode.base_path at tmp_path and run ccat with `args`."""
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        return runner.invoke(app, args)
    finally:
        AGENTS["opencode"].base_path = original


def _conv(db_dir, sid, msgs):
    """Add a conversation to the opencode db rooted at db_dir/opencode.db."""
    db_path = db_dir / "opencode.db"
    # Create the schema if this is the first conversation.
    if not db_path.exists():
        _make_opencode_conv_db(db_dir, sid, msgs)
        return
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO session (id, title, time_created, time_updated, "
        "tokens_input, tokens_output, directory) VALUES (?, 'Conv', "
        "1700000000000, 1700000060000, 100, 200, '/tmp')",
        (sid,),
    )
    for i, (role, content) in enumerate(msgs):
        mid = f"{sid}_msg_{i}"
        cur.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, sid, 1700000000000 + i * 1000,
             1700000000000 + i * 1000,
             json.dumps({"role": role, "content": content})))
        cur.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, "
            "time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (f"{sid}_prt_{i}", mid, sid, 1700000000000 + i * 1000,
             1700000000000 + i * 1000,
             json.dumps({"type": "text", "text": content})))
    conn.commit()
    conn.close()


DEFAULT_CONV = [
    ("user", "Write a fibonacci function"),
    ("assistant", "Here is a fibonacci function: def fib(n): ..."),
]


def test_ccat_single_session(tmp_path):
    """ccat agent/session prints a readable transcript."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["opencode/ses_a"])
    assert result.exit_code == 0
    assert "Write a fibonacci function" in result.stdout
    assert "fibonacci function: def fib" in result.stdout
    # role labels are present
    assert "you" in result.stdout
    assert "assistant" in result.stdout


def test_ccat_single_session_json(tmp_path):
    """ccat --json prints a JSON list of {role, content}."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["--json", "opencode/ses_a"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data[0] == {"role": "user", "content": "Write a fibonacci function"}
    assert data[1]["role"] == "assistant"


def test_ccat_multiple_sessions(tmp_path):
    """Two sessions are printed in order, separated by a blank line."""
    _make_opencode_conv_db(tmp_path, "ses_a",
                           [("user", "AAA first"), ("assistant", "resp A")])
    _conv(tmp_path, "ses_b", [("user", "BBB second"), ("assistant", "resp B")])
    result = _run_ccat(tmp_path,
                       ["--color", "never", "opencode/ses_a", "opencode/ses_b"])
    assert result.exit_code == 0
    # AAA comes before BBB (order preserved)
    assert result.stdout.index("AAA first") < result.stdout.index("BBB second")
    # a blank line separates the two transcripts
    assert "\n\n" in result.stdout


def test_ccat_multiple_sessions_json(tmp_path):
    """--json with two sessions prints two JSON arrays in order."""
    _make_opencode_conv_db(tmp_path, "ses_a",
                           [("user", "AAA first"), ("assistant", "resp A")])
    _conv(tmp_path, "ses_b", [("user", "BBB second"), ("assistant", "resp B")])
    result = _run_ccat(tmp_path,
                       ["--json", "opencode/ses_a", "opencode/ses_b"])
    assert result.exit_code == 0
    # Parse the two concatenated JSON arrays.
    decoder = json.JSONDecoder()
    idx = 0
    parsed = []
    s = result.stdout.strip()
    while idx < len(s):
        while idx < len(s) and s[idx] in " \t\n\r":
            idx += 1
        if idx >= len(s):
            break
        obj, end = decoder.raw_decode(s, idx)
        parsed.append(obj)
        idx = end
    assert len(parsed) == 2
    assert parsed[0][0]["content"] == "AAA first"
    assert parsed[1][0]["content"] == "BBB second"


def test_ccat_bad_session_among_good(tmp_path):
    """A bad session is reported; good sessions still print; exit 0."""
    _make_opencode_conv_db(tmp_path, "ses_a",
                           [("user", "AAA first"), ("assistant", "resp A")])
    result = _run_ccat(tmp_path,
                       ["--color", "never", "opencode/ses_bogus", "opencode/ses_a"])
    assert result.exit_code == 0
    assert "no such session" in result.stdout
    assert "AAA first" in result.stdout


def test_ccat_only_bad_session_exits_nonzero(tmp_path):
    """If no session could be printed, exit non-zero (cat semantics)."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["opencode/ses_bogus"])
    assert result.exit_code != 0
    assert "no such session" in result.stdout


def test_ccat_no_args(tmp_path):
    """No arguments: usage error, exit 1."""
    result = _run_ccat(tmp_path, [])
    assert result.exit_code == 1
    assert "Usage" in result.stdout


def test_ccat_unknown_agent(tmp_path):
    """Unknown agent is reported cleanly."""
    result = _run_ccat(tmp_path, ["noagent/ses_a"])
    assert result.exit_code != 0
    assert "unknown agent" in result.stdout


def test_ccat_not_installed(tmp_path):
    """An agent whose storage is absent is reported as not installed."""
    # Point opencode at a path that does not exist.
    missing = tmp_path / "does-not-exist"
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = missing
    try:
        result = runner.invoke(app, ["--color", "never", "opencode/ses_a"])
    finally:
        AGENTS["opencode"].base_path = original
    assert result.exit_code != 0
    assert "not installed" in result.stdout


def test_ccat_color_never_has_no_ansi(tmp_path):
    """--color never produces no ANSI escape codes."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["--color", "never", "opencode/ses_a"])
    assert "\x1b[" not in result.stdout


def test_ccat_transcript_role_alignment(tmp_path):
    """Role labels sit in a fixed column; content follows two spaces."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["--color", "never", "opencode/ses_a"])
    lines = result.stdout.splitlines()
    # Every transcript line starts with 4 spaces + a role label + 2 spaces.
    for line in lines:
        if line.strip():
            assert line.startswith("    ")
            # after the 4-space margin, the role label (<=9 chars) then "  "
            rest = line[4:]
            assert "  " in rest
