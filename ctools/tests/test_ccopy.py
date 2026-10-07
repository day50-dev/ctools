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

class FakeSSH:
    """Stands in for subprocess.run, recording commands and returning canned output."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "input": kwargs.get("input")})

        class P:
            pass
        p = P()
        p.returncode = self.returncode
        p.stdout = self.stdout
        p.stderr = self.stderr
        return p


def test_remote_destination(tmp_path, monkeypatch):
    """ccopy local/ses ssh://user@remote/agent: ssh runs --import-json, gets the new id."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    fake = FakeSSH(stdout="ses_remote_new123\n")
    monkeypatch.setattr("ctools.ccopy.subprocess.run", fake)
    try:
        result = runner.invoke(app, ["opencode/ses_src", "ssh://chris@remote/codex"])
        assert result.exit_code == 0, result.output
        assert "on chris@remote" in result.stdout
        assert "ses_remote_new123" in result.stdout
        # Exactly one ssh call: the import, with the conversation JSON piped in.
        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["cmd"] == ["ssh", "-T", "chris@remote", "ccopy --import-json codex"]
        records = json.loads(call["input"])
        assert records[0]["role"] == "user"
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_source(tmp_path, monkeypatch):
    """ccopy ssh://user@remote/agent/ses local-agent: ssh runs --export-json."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    records = [{"role": "user", "content": "pulled across the wire"}]
    fake = FakeSSH(stdout=json.dumps(records))
    monkeypatch.setattr("ctools.ccopy.subprocess.run", fake)
    try:
        result = runner.invoke(app, ["ssh://chris@remote/opencode/ses_src", "opencode"])
        assert result.exit_code == 0, result.output
        assert "from opencode/ses_src on chris@remote" in result.stdout
        assert len(fake.calls) == 1
        assert fake.calls[0]["cmd"] == \
            ["ssh", "-T", "chris@remote", "ccopy --export-json opencode/ses_src"]
        # The new local session holds the pulled conversation.
        new_id = result.stdout.split("session:")[-1].strip().split()[0]
        assert AGENTS["opencode"].messages(new_id)[0].content == records[0]["content"]
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_failure_reports_hint(tmp_path, monkeypatch):
    """A failing ssh (e.g. ctxttools missing on the remote) is reported with a hint."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    # First call: `ccopy` not found. Second call (the python -m fallback) also
    # fails, so the error path reports both attempts.
    fake = FakeSSH(stderr="/bin/sh: 1: ccopy: not found\n", returncode=127)
    monkeypatch.setattr("ctools.ccopy.subprocess.run", fake)
    try:
        result = runner.invoke(app, ["opencode/ses_src", "ssh://chris@remote/codex"])
        assert result.exit_code == 1
        assert "ccopy: not found" in result.stdout
        # After the fallback is exhausted, the hint about installing ctxttools shows.
        assert "pip install ctxttools" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_fallback_to_python_m(tmp_path, monkeypatch):
    """When `ccopy` is missing on the remote ssh PATH, retry via `python -m ctools.cli`."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path

    calls = []

    class P:
        pass

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        p = P()
        if "python -m ctools.cli" in cmd[-1]:
            p.returncode = 0
            p.stdout = "ses_via_fallback\n"
            p.stderr = ""
        else:
            p.returncode = 127
            p.stdout = ""
            p.stderr = "/bin/sh: 1: ccopy: not found\n"
        return p

    monkeypatch.setattr("ctools.ccopy.subprocess.run", fake_run)
    try:
        result = runner.invoke(app, ["opencode/ses_src", "ssh://chris@remote/codex"])
        assert result.exit_code == 0, result.output
        assert "ses_via_fallback" in result.stdout
        assert len(calls) == 2
        # The fallback cd's to $CTOOLS_DIR and runs via python -m, quoting the
        # whole ccopy command so the remote shell sees it as one unit.
        assert "CTOOLS_DIR" in calls[1][-1]
        assert "python -m ctools.cli run 'ccopy --import-json codex'" in calls[1][-1]
    finally:
        AGENTS["opencode"].base_path = original


def test_dry_run_local_source_remote_destination_makes_no_ssh_calls(tmp_path, monkeypatch):
    """With a local source, --dry-run counts locally and never touches ssh."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    fake = FakeSSH()
    monkeypatch.setattr("ctools.ccopy.subprocess.run", fake)
    try:
        result = runner.invoke(app, ["opencode/ses_src", "ssh://chris@remote/codex",
                                     "--dry-run"])
        assert result.exit_code == 0
        assert "Would copy" in result.stdout
        assert fake.calls == []
    finally:
        AGENTS["opencode"].base_path = original


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
