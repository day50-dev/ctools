import json
import sqlite3
import pytest
from typer.testing import CliRunner
from ctools.ccopy import app
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


# --- Remote reference parsing ---

def test_parse_remote_ref_local():
    from ctools.cli import parse_remote_ref
    remote, ref = parse_remote_ref("opencode/ses_abc")
    assert remote is None
    assert ref == "opencode/ses_abc"


def test_parse_remote_ref_bare_host():
    from ctools.cli import parse_remote_ref
    remote, ref = parse_remote_ref("ssh://remote/codex")
    assert remote.host == "remote"
    assert remote.user is None
    assert remote.port is None
    assert ref == "codex"
    assert str(remote) == "ssh://remote"
    assert remote.ssh_command("ccopy --import-json codex") == \
        ["ssh", "-T", "remote", "ccopy --import-json codex"]


def test_parse_remote_ref_user_port_session():
    from ctools.cli import parse_remote_ref
    remote, ref = parse_remote_ref("ssh://chris@remote:2222/opencode/ses_1")
    assert remote.host == "remote"
    assert remote.user == "chris"
    assert remote.port == 2222
    assert ref == "opencode/ses_1"
    assert remote.target == "chris@remote"
    assert remote.ssh_command("x") == ["ssh", "-T", "-p", "2222", "chris@remote", "x"]


def test_parse_remote_ref_no_host():
    from ctools.cli import parse_remote_ref
    with pytest.raises(ValueError):
        parse_remote_ref("ssh://")


# --- Raw wire modes: --export-json / --import-json ---

def test_export_json_mode(tmp_path):
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--export-json", "opencode/ses_src"])
        assert result.exit_code == 0, result.output
        records = json.loads(result.stdout)
        assert [r["role"] for r in records] == ["user", "assistant"]
        assert any("fib" in r["content"] for r in records)
    finally:
        AGENTS["opencode"].base_path = original


def test_import_json_mode(tmp_path):
    """--import-json reads a conversation from stdin and creates a new session."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        records = [
            {"role": "user", "content": "hello over the wire"},
            {"role": "assistant", "content": "received over the wire"},
        ]
        result = runner.invoke(app, ["--import-json", "opencode"],
                               input=json.dumps(records))
        assert result.exit_code == 0, result.output
        new_id = result.stdout.strip()
        assert new_id.startswith("ses_")
        msgs = AGENTS["opencode"].messages(new_id)
        assert [m.content for m in msgs] == [r["content"] for r in records]
    finally:
        AGENTS["opencode"].base_path = original


def test_import_json_rejects_garbage(tmp_path):
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = runner.invoke(app, ["--import-json", "opencode"], input="{not json")
        assert result.exit_code == 1
        assert "Invalid JSON" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


def test_pipe_roundtrip(tmp_path):
    """export | import copies a conversation locally, no ssh involved."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        exported = runner.invoke(app, ["--export-json", "opencode/ses_src"])
        assert exported.exit_code == 0
        imported = runner.invoke(app, ["--import-json", "opencode"],
                                 input=exported.stdout)
        assert imported.exit_code == 0, imported.output
        new_id = imported.stdout.strip()
        assert new_id != "ses_src"
        assert [m.content for m in AGENTS["opencode"].messages(new_id)] == \
               [m.content for m in AGENTS["opencode"].messages("ses_src")]
    finally:
        AGENTS["opencode"].base_path = original


# --- Remote transport (ssh mocked) ---
# --- Remote transport (pulled storage, ssh mocked) ---


def _mock_transport(monkeypatch, remote_home: "object"):
    """Patch ccopy's transport so "ssh to remote" becomes a local file move.

    _pull copies `<remote_home>/<agent-dir>/<storage>` into dest_base as
    `<agent-dir>/<storage>`; _push copies it back. This exercises the whole
    flow (pull -> local agent -> push) with no real network.
    """
    import ctools.ccopy as ccopy

    def fake_pull(remote, agent, dest_base):
        rel = ccopy._remote_storage_rel(agent)
        src = remote_home / rel
        assert src.exists(), f"mock remote is missing {src}"
        dst = dest_base / rel
        if dst.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
        import shutil as _sh
        if src.is_dir():
            _sh.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

    def fake_push(remote, agent, base):
        rel = ccopy._remote_storage_rel(agent)
        src = base / rel
        assert src.exists()
        dst = remote_home / rel
        import shutil as _sh
        if src.is_dir():
            _sh.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

    monkeypatch.setattr(ccopy, "_pull", fake_pull)
    monkeypatch.setattr(ccopy, "_push", fake_push)


def test_remote_destination_pulls_and_pushes(tmp_path, monkeypatch):
    """ccopy local/ses ssh://host/agent: pull remote storage, create the
    session against the local mirror, push it back."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    remote_home = tmp_path / "remote"
    (remote_home / ".local" / "share" / "opencode").mkdir(parents=True)
    # The remote has its own (empty-ish) opencode db; the push must land there.
    _make_opencode_conversation_db(remote_home / ".local" / "share" / "opencode",
                                   "ses_remote_existing")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    _mock_transport(monkeypatch, remote_home)
    try:
        result = runner.invoke(app, ["opencode/ses_src", "ssh://chris@remote/opencode"])
        assert result.exit_code == 0, result.output
        assert "on chris@remote" in result.stdout
        # The new session now exists in the REMOTE db (the pushed mirror).
        import sqlite3
        conn = sqlite3.connect(str(remote_home / ".local" / "share" / "opencode" / "opencode.db"))
        ids = [r[0] for r in conn.execute("SELECT id FROM session")]
        conn.close()
        assert "ses_remote_existing" in ids
        new_id = result.stdout.strip().split()[-1]
        assert new_id in ids
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_source_pulls_conversation(tmp_path, monkeypatch):
    """ccopy ssh://host/agent/ses local-agent: pull remote storage, read the
    conversation locally, create it in the local destination."""
    remote_home = tmp_path / "remote"
    (remote_home / ".local" / "share" / "opencode").mkdir(parents=True)
    _make_opencode_conversation_db(remote_home / ".local" / "share" / "opencode",
                                   "ses_pulled")
    _make_opencode_conversation_db(tmp_path, "ses_dummy")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    _mock_transport(monkeypatch, remote_home)
    try:
        result = runner.invoke(app, ["ssh://chris@remote/opencode/ses_pulled", "opencode"])
        assert result.exit_code == 0, result.output
        assert "from opencode/ses_pulled on chris@remote" in result.stdout
        new_id = result.stdout.strip().split()[-1]
        msgs = AGENTS["opencode"].messages(new_id)
        assert msgs[0].content == "Write a fibonacci function"
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_source_missing_session_is_error(tmp_path, monkeypatch):
    """A remote source whose session doesn't exist is a clean error."""
    remote_home = tmp_path / "remote"
    (remote_home / ".local" / "share" / "opencode").mkdir(parents=True)
    _make_opencode_conversation_db(remote_home / ".local" / "share" / "opencode",
                                   "ses_pulled")
    _make_opencode_conversation_db(tmp_path, "ses_dummy")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    _mock_transport(monkeypatch, remote_home)
    try:
        result = runner.invoke(app, ["ssh://chris@remote/opencode/ses_absent", "opencode"])
        assert result.exit_code == 1
        assert "No conversation" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


def test_dry_run_remote_makes_no_transport_calls(tmp_path, monkeypatch):
    """With a local source, --dry-run counts locally and never pulls or pushes."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    import ctools.ccopy as ccopy

    def boom(*a, **kw):
        raise AssertionError("transport should not run for a dry run")

    monkeypatch.setattr(ccopy, "_pull", boom)
    monkeypatch.setattr(ccopy, "_push", boom)
    try:
        result = runner.invoke(app, ["opencode/ses_src", "ssh://chris@remote/codex",
                                     "--dry-run"])
        assert result.exit_code == 0
        assert "Would copy" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


def test_storage_relative():
    """storage_relative() yields the $HOME-relative path a remote pull/push
    must target."""
    from ctools.agents import get_agent
    assert get_agent("opencode").storage_relative() == \
        ".local/share/opencode/opencode.db"
    assert get_agent("pi").storage_relative() == ".pi/agent/sessions"
    assert get_agent("claude-code").storage_relative() == ".claude/projects"
    assert get_agent("codex").storage_relative() == ".codex/sessions"
    assert get_agent("kilo").storage_relative() == ".local/share/kilo/kilo.db"


def test_remote_storage_rel_anchors_at_home():
    """The remote tar path is relative to $HOME (the agent dir is the anchor)."""
    import ctools.ccopy as ccopy
    from ctools.agents import get_agent
    assert ccopy._remote_storage_rel(get_agent("opencode")) == \
        ".local/share/opencode/opencode.db"
    assert ccopy._remote_storage_rel(get_agent("pi")) == ".pi/agent/sessions"
    assert ccopy._remote_storage_rel(get_agent("claude-code")) == ".claude/projects"
# --- Live ssh roundtrip (skipped unless `ssh localhost` works) ---

def _ssh_localhost_available() -> bool:
    import shutil
    import subprocess
    if shutil.which("ssh") is None:
        return False
    try:
        proc = subprocess.run(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
             "localhost", "true"],
            capture_output=True, text=True, timeout=10)
        return proc.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _ssh_localhost_available(),
                    reason="ssh to localhost not available")
def test_live_ssh_transport():
    """Verify the real ssh transport (ssh -T, capture, -p port) works over sshd."""
    import subprocess
    from ctools.cli import Remote
    # Read-only command; just prove we can run something on localhost and get
    # its stdout back through the same path run_remote uses.
    remote = Remote(host="localhost")
    proc = subprocess.run(remote.ssh_command("echo hello-remote"),
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "hello-remote"
    # And that a failing command surfaces a non-zero returncode (the path that
    # triggers the remote-error reporting in run_remote).
    proc2 = subprocess.run(remote.ssh_command("exit 3"),
                           capture_output=True, text=True, timeout=15)
    assert proc2.returncode == 3
