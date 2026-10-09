import json
import sqlite3
import subprocess
import pytest
from pathlib import Path
from ctools.testing import Runner
from ctools.ccopy import app, import_conversation
from ctools.lib import AGENTS


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The ccopy remote probe memoizes per-host results in a module dict;
    clear it so tests don't see a stale answer from a prior test."""
    import ctools.ccopy as ccopy
    ccopy._ctools_probes.clear()
    yield
    ccopy._ctools_probes.clear()

runner = Runner()


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


def test_unknown_source_agent_is_error(tmp_path):
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = runner.invoke(app, ["not-an-agent/ses_src", "opencode"])
        assert result.exit_code == 1
        assert "Unknown agent" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


def test_missing_session_id_is_error(tmp_path):
    """A source with no session id is an error."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = runner.invoke(app, ["opencode", "opencode"])
        assert result.exit_code == 1
        assert "No session ID" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


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
        # diagnostics go to stderr; stdout stays clean for the id
        assert "Invalid JSON" in result.stderr
        assert "Invalid JSON" not in result.stdout
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


# --- File / fd / stdin source (the Unix-y form) ---

def _conv_json(tmp_path, records=None):
    if records is None:
        records = [{"role": "user", "content": "from a file"},
                   {"role": "assistant", "content": "ok, from a file"}]
    p = tmp_path / "conv.json"
    p.write_text(json.dumps(records))
    return p, records


def test_file_source_imports_json(tmp_path):
    """ccopy FILE dest reads a conversation JSON from a file."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    p, records = _conv_json(tmp_path)
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = runner.invoke(app, [str(p), "opencode"])
        assert result.exit_code == 0, result.output
        new_id = result.stdout.strip().split()[-1]
        assert new_id.startswith("ses_")
        assert [m.content for m in AGENTS["opencode"].messages(new_id)] == \
            [r["content"] for r in records]
    finally:
        AGENTS["opencode"].base_path = original


def test_stdin_source_via_dash(tmp_path):
    """ccopy - dest reads the conversation JSON from stdin (the Unix-y pipe)."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    records = [{"role": "user", "content": "piped in"}]
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = runner.invoke(app, ["-", "opencode"], input=json.dumps(records))
        assert result.exit_code == 0, result.output
        new_id = result.stdout.strip().split()[-1]
        assert [m.content for m in AGENTS["opencode"].messages(new_id)] == \
            [r["content"] for r in records]
    finally:
        AGENTS["opencode"].base_path = original


def test_file_source_rejects_garbage(tmp_path):
    """A file source that isn't valid conversation JSON is a clean error."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = runner.invoke(app, [str(p), "opencode"])
        assert result.exit_code == 1
        # rich wraps the line, so collapse whitespace before asserting.
        import re
        flat = re.sub(r"\s+", " ", result.stdout)
        assert "is not valid conversation JSON" in flat
    finally:
        AGENTS["opencode"].base_path = original


def test_file_source_dry_run(tmp_path):
    """--dry-run with a file source reports the count and writes nothing."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    p, records = _conv_json(tmp_path)
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        before = {s.id for s in AGENTS["opencode"].sessions()}
        result = runner.invoke(app, [str(p), "opencode", "--dry-run"])
        assert result.exit_code == 0
        assert "Would copy" in result.stdout
        after = {s.id for s in AGENTS["opencode"].sessions()}
        assert before == after
    finally:
        AGENTS["opencode"].base_path = original


def test_is_json_source(tmp_path):
    """_is_json_source recognizes '/', /dev/fd/N, and real files."""
    import ctools.ccopy as ccopy
    assert ccopy._is_json_source("-")
    assert ccopy._is_json_source("/dev/fd/63")
    f = tmp_path / "conv.json"
    f.write_text("[]")
    assert ccopy._is_json_source(str(f))
    # a normal agent ref is NOT a json source
    assert not ccopy._is_json_source("opencode/ses_abc")
    assert not ccopy._is_json_source("ssh://host/opencode/ses_abc")


# --- Remote transport (ssh mocked) ---
# --- Remote transport (pulled storage, ssh mocked) ---


def _mock_transport(monkeypatch, remote_home: "object", has_ctools: bool = False):
    """Patch ccopy's transport so "ssh to remote" becomes a local file move.

    _pull copies `<remote_home>/<agent-dir>/<storage>` into dest_base as
    `<agent-dir>/<storage>`; _push copies it back. This exercises the whole
    flow (pull -> local agent -> push) with no real network. When
    has_ctools=False (the default) the first-pass probe also reports the
    remote as having no ctools, so the expensive tar path is taken.
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

    if has_ctools:
        # First pass: the remote runs ctools. Model it as a local agent
        # anchored at the remote $HOME (remote_home) so the export/import
        # wire commands hit the "remote" db, which lives at
        # remote_home/<storage-relative>.
        def _mirror(agent):
            # Mirror the remote layout: anchor the mirror's base_path at
            # remote_home/<agent-tree> so db_name / files_read resolve to
            # the remote db location (remote_home/<storage-relative>).
            rel = Path(ccopy._remote_storage_rel(agent))
            db_name = getattr(type(agent), 'db_name', None)
            if db_name:
                base_rel = rel.parent
            elif getattr(type(agent), 'files_read', None):
                base_rel = Path('.')
            else:
                base_rel = rel
            return type(agent)(remote_home / base_rel)

        def fake_has_ctools(remote):
            return True

        def fake_common_doc(remote, agent, session_id):
            mirror = _mirror(agent)
            try:
                return mirror.to_common(session_id)
            except Exception:
                return None

        def fake_import(remote, agent, records):
            return import_conversation(_mirror(agent), records)

        monkeypatch.setattr(ccopy, "_remote_has_ctools", fake_has_ctools)
        monkeypatch.setattr(ccopy, "_remote_common_doc", fake_common_doc)
        monkeypatch.setattr(ccopy, "_remote_import_records", fake_import)
    else:
        monkeypatch.setattr(ccopy, "_remote_has_ctools", lambda remote: False)


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
    monkeypatch.setattr(ccopy, "_remote_common_doc", boom)
    monkeypatch.setattr(ccopy, "_remote_import_records", boom)
    try:
        result = runner.invoke(app, ["opencode/ses_src", "ssh://chris@remote/codex",
                                     "--dry-run"])
        assert result.exit_code == 0
        assert "Would copy" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_source_first_pass_when_remote_has_ctools(tmp_path, monkeypatch):
    """When the remote runs ctools, the source is exported there over the
    wire (one ssh) and the expensive tar pull never happens."""
    remote_home = tmp_path / "remote"
    (remote_home / ".local" / "share" / "opencode").mkdir(parents=True)
    _make_opencode_conversation_db(remote_home / ".local" / "share" / "opencode",
                                   "ses_pulled")
    _make_opencode_conversation_db(tmp_path, "ses_dummy")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    _mock_transport(monkeypatch, remote_home, has_ctools=True)
    import ctools.ccopy as ccopy

    def no_tar(*a, **kw):
        raise AssertionError("first pass: the tar transport must not run")

    monkeypatch.setattr(ccopy, "_pull", no_tar)
    monkeypatch.setattr(ccopy, "_push", no_tar)
    try:
        result = runner.invoke(app, ["ssh://chris@remote/opencode/ses_pulled", "opencode"])
        assert result.exit_code == 0, result.output
        assert "from opencode/ses_pulled on chris@remote" in result.stdout
        new_id = result.stdout.strip().split()[-1]
        msgs = AGENTS["opencode"].messages(new_id)
        assert msgs[0].content == "Write a fibonacci function"
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_destination_first_pass_when_remote_has_ctools(tmp_path, monkeypatch):
    """When the remote runs ctools, the new session is seeded there over the
    wire (ccopy --import-json) and the pull/push never happen."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    remote_home = tmp_path / "remote"
    (remote_home / ".local" / "share" / "opencode").mkdir(parents=True)
    _make_opencode_conversation_db(remote_home / ".local" / "share" / "opencode",
                                   "ses_remote_existing")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    _mock_transport(monkeypatch, remote_home, has_ctools=True)
    import ctools.ccopy as ccopy

    def no_tar(*a, **kw):
        raise AssertionError("first pass: the tar transport must not run")

    monkeypatch.setattr(ccopy, "_pull", no_tar)
    monkeypatch.setattr(ccopy, "_push", no_tar)
    try:
        result = runner.invoke(app, ["opencode/ses_src", "ssh://chris@remote/opencode"])
        assert result.exit_code == 0, result.output
        assert "on chris@remote" in result.stdout
        # The new session now exists in the REMOTE db.
        import sqlite3
        conn = sqlite3.connect(str(remote_home / ".local" / "share" / "opencode" / "opencode.db"))
        ids = [r[0] for r in conn.execute("SELECT id FROM session")]
        conn.close()
        assert "ses_remote_existing" in ids
        new_id = result.stdout.strip().split()[-1]
        assert new_id in ids
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_source_falls_back_to_tar_when_ctools_export_fails(tmp_path, monkeypatch):
    """If the remote claims to have ctools but the wire export fails (e.g.
    unknown session there), the copy falls back to the tar storage pull."""
    remote_home = tmp_path / "remote"
    (remote_home / ".local" / "share" / "opencode").mkdir(parents=True)
    _make_opencode_conversation_db(remote_home / ".local" / "share" / "opencode",
                                   "ses_pulled")
    _make_opencode_conversation_db(tmp_path, "ses_dummy")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    _mock_transport(monkeypatch, remote_home, has_ctools=True)
    import ctools.ccopy as ccopy

    def broken_export(remote, agent, session_id):
        return None  # remote ctools failed to export

    monkeypatch.setattr(ccopy, "_remote_common_doc", broken_export)
    try:
        result = runner.invoke(app, ["ssh://chris@remote/opencode/ses_pulled", "opencode"])
        assert result.exit_code == 0, result.output
        assert "from opencode/ses_pulled on chris@remote" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_has_ctools_probe(monkeypatch):
    """_remote_has_ctools: yes/no detection via the `command -v ccopy` probe,
    and the answer is cached per host."""
    import ctools.ccopy as ccopy
    from ctools.cli import Remote
    from unittest.mock import patch

    ccopy._ctools_probes.clear()
    remote = Remote(host="probehost")

    def mk(rc, out):
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")

    with patch.object(ccopy, "_run", return_value=mk(0, "ok\n")), \
            patch.object(ccopy.shutil, "which", return_value="/usr/bin/ssh"):
        assert ccopy._remote_has_ctools(remote) is True
        with patch.object(ccopy, "_run") as second:
            assert ccopy._remote_has_ctools(remote) is True  # cached
            second.assert_not_called()

    ccopy._ctools_probes.clear()
    with patch.object(ccopy, "_run", return_value=mk(1, "")), \
            patch.object(ccopy.shutil, "which", return_value="/usr/bin/ssh"):
        assert ccopy._remote_has_ctools(remote) is False
    ccopy._ctools_probes.clear()


def test_remote_common_doc_parses_envelope(monkeypatch):
    """_remote_common_doc parses the envelope JSON the far side streams, and
    returns None on bad output or a non-zero exit."""
    import ctools.ccopy as ccopy
    from ctools.cli import Remote
    from unittest.mock import patch
    remote = Remote(host="probehost")
    agent = AGENTS["opencode"]
    doc = {'context': [{'role': 'user', 'content': 'hi'}]}

    with patch.object(ccopy, "_run",
                      return_value=subprocess.CompletedProcess(
                          args=[], returncode=0, stdout=json.dumps(doc), stderr="")):
        assert ccopy._remote_common_doc(remote, agent, "ses_x") == doc

    with patch.object(ccopy, "_run",
                      return_value=subprocess.CompletedProcess(
                          args=[], returncode=1, stdout="", stderr="nope")):
        assert ccopy._remote_common_doc(remote, agent, "ses_x") is None

    with patch.object(ccopy, "_run",
                      return_value=subprocess.CompletedProcess(
                          args=[], returncode=0, stdout="not json", stderr="")):
        assert ccopy._remote_common_doc(remote, agent, "ses_x") is None


def test_remote_import_records_reads_id(monkeypatch):
    """_remote_import_records returns the id the far side prints, or None on
    failure."""
    import ctools.ccopy as ccopy
    from ctools.cli import Remote
    from unittest.mock import patch
    remote = Remote(host="probehost")
    agent = AGENTS["opencode"]
    records = [{'role': 'user', 'content': 'hi'}]

    with patch.object(ccopy, "_run",
                      return_value=subprocess.CompletedProcess(
                          args=[], returncode=0, stdout="ses_new_1\n", stderr="")) as call:
        assert ccopy._remote_import_records(remote, agent, records) == "ses_new_1"
        # the records were piped in as JSON on stdin
        assert call.call_args.kwargs["input_text"] == json.dumps(records)

    with patch.object(ccopy, "_run",
                      return_value=subprocess.CompletedProcess(
                          args=[], returncode=1, stdout="", stderr="boom")):
        assert ccopy._remote_import_records(remote, agent, records) is None


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


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_remote_storage_exists_true(monkeypatch):
    import ctools.ccopy as ccopy
    from ctools.cli import Remote
    from ctools.agents import get_agent
    monkeypatch.setattr(ccopy, "_run", lambda argv, input_text=None: _FakeProc(0, "ok\n"))
    assert ccopy._remote_storage_exists(Remote(host="_lorenz"), get_agent("pi")) is True


def test_remote_storage_exists_false(monkeypatch):
    import ctools.ccopy as ccopy
    from ctools.cli import Remote
    from ctools.agents import get_agent
    monkeypatch.setattr(ccopy, "_run", lambda argv, input_text=None: _FakeProc(1, ""))
    assert ccopy._remote_storage_exists(Remote(host="_lorenz"), get_agent("pi")) is False


def test_pull_not_installed_is_clean_error(monkeypatch, capsys):
    """When the agent's storage isn't on the remote, _pull reports it plainly
    (and points at `cdir`) instead of dumping a raw tar failure."""
    import ctools.ccopy as ccopy
    from ctools.cli import Remote
    from ctools.agents import get_agent
    monkeypatch.setattr(ccopy, "_remote_storage_exists", lambda remote, agent: False)
    with pytest.raises(SystemExit) as exc:
        ccopy._pull(Remote(host="_lorenz"), get_agent("pi"), Path("/tmp/nowhere"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "not installed on _lorenz" in out
    assert "cdir ssh://_lorenz" in out


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
