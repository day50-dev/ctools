import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import pytest
from pathlib import Path
from ctools.testing import Runner
from ctools.ccopy import app, import_conversation
from ctools.lib import AGENTS


@pytest.fixture(scope="session")
def local_sshd(tmp_path_factory):
    """A throwaway sshd on 127.0.0.1 whose remote $HOME is a seeded fake home.

    Lets the real remote paths (ssh -T + tar pull, the ctools wire mode, the
    storage fallback) be tested end to end without depending on a particular
    machine: host key, user key, authorized_keys, and the remote $HOME are all
    temp files. The remote $HOME carries one pi session plus a small set of
    ctools entry-point shims (``python -m ctools``) so the wire-mode commands
    ``ccopy --version``, ``ccat --raw`` and ``cdir -f json`` work there.
    """
    if shutil.which("sshd") is None:
        pytest.skip("sshd binary not available")
    if os.getuid() == 0:
        pytest.skip("skip the local-sshd e2e as root (root ssh quirks)")

    root = tmp_path_factory.mktemp("sshd")
    sshd = "/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else shutil.which("sshd")
    hostkey = root / "hostkey"
    userkey = root / "id_ed25519"
    authorized = root / "authorized_keys"
    pidfile = root / "sshd.pid"

    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hostkey)], check=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(userkey)], check=True)
    authorized.write_text((userkey.with_suffix(".pub")).read_text())
    os.chmod(userkey, 0o600)

    # Fake remote $HOME: one pi session + ctools entry-point shims.
    fakehome = root / "fakehome"
    sid = "01a1b000-0000-4000-8000-000000000001"
    sessdir = fakehome / ".pi" / "agent" / "sessions" / "--tmp-remote--"
    sessdir.mkdir(parents=True)
    lines = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": "2026-02-01T10:00:00.000Z", "cwd": "/tmp/remote"},
        {"type": "message", "id": "m1", "parentId": "session",
         "timestamp": "2026-02-01T10:00:01.000Z",
         "message": {"role": "user", "content": "hello from the remote"}},
        {"type": "message", "id": "m2", "parentId": "m1",
         "timestamp": "2026-02-01T10:00:02.000Z",
         "message": {"role": "assistant",
                      "content": [{"type": "text", "text": "hello from the remote reply"}],
                      "model": "test/model",
                      "usage": {"input": 5, "output": 6, "totalTokens": 11}}},
    ]
    (sessdir / f"2026-02-01T10-00-00-000Z_{sid}.jsonl").write_text(
        "\n".join(json.dumps(l) for l in lines) + "\n")

    # Shims: the real ctools entry points (dispatch:main) for both the long
    # verb form (`ctools dir ...`) and the short command name (`cdir ...`).
    bindir = fakehome / ".local" / "bin"
    bindir.mkdir(parents=True)
    python = sys.executable
    # Short command name -> `python -m ctools <verb>` (empty for `ctools` itself).
    verb_for = {"ctools": "", "cdir": "dir", "ccat": "cat",
                "cgrep": "grep", "ccopy": "copy", "cdu": "du"}
    for name, verb in verb_for.items():
        shim = bindir / name
        body = "#!/bin/sh\nexec {python} -m ctools{verb} \"$@\"\n".format(
            python=python, verb=(" " + verb) if verb else "")
        shim.write_text(body)
        os.chmod(shim, 0o755)

    # Free port.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    proc = subprocess.Popen(
        [sshd, "-D",
         "-o", "UsePAM=no",
         "-o", "PasswordAuthentication=no",
         "-o", "StrictModes=no",
         "-o", "PermitRootLogin=no",
         "-o", "HostKey=" + str(hostkey),
         "-o", "ListenAddress=127.0.0.1",
         "-o", "AuthorizedKeysFile=" + str(authorized),
         "-o", "SetEnv HOME=" + str(fakehome),
         "-o", "PrintMotd=no",
         "-o", "Banner=none",
         "-o", "PidFile=" + str(pidfile),
         "-p", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)

    # Wait until the port accepts connections.
    deadline = time.monotonic() + 10
    up = False
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.25)
            s.close()
            up = True
            break
        except OSError:
            time.sleep(0.1)
    if not up:
        proc.terminate()
        pytest.fail("local sshd did not come up on port %d" % port)

    yield {"host": "127.0.0.1", "port": port, "key": str(userkey),
           "fakehome": str(fakehome), "sid": sid}

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The ccopy remote probe memoizes per-host results in a module dict;
    clear it so tests don't see a stale answer from a prior test."""
    import ctools.ccopy as ccopy
    ccopy._ctools_probes.clear()
    yield
    ccopy._ctools_probes.clear()


@pytest.fixture(autouse=True)
def _isolate_backup_home(tmp_path, monkeypatch, request):
    """ccopy now snapshots the destination agent's storage before every
    local write (``ctools.backup``), writing under the XDG data home. Point
    both HOME and XDG_DATA_HOME at a throwaway dir so the tests never
    create (or read) backups in the developer's real home.

    The live-ssh tests are exempt: ssh itself needs the real ``~/.ssh``.
    """
    if "live_ssh" in request.node.keywords:
        yield
        return
    home = tmp_path / "home"
    home.mkdir()
    data = home / ".local" / "share"
    data.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    yield

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


def test_copy_seeds_destination_for_source_cwd(tmp_path):
    """A copy seeds the destination session for the SOURCE session's cwd, so a
    claude-code copy lands in that project dir (not the invocation dir) and
    resumes from there -- the fix for 'No conversation found' on resume."""
    _make_opencode_conversation_db(tmp_path, "ses_src")  # source directory = '/tmp'
    cc_base = tmp_path / "cc"
    (cc_base / "projects").mkdir(parents=True, exist_ok=True)
    original_oc = AGENTS["opencode"].base_path
    original_cc = AGENTS["claude-code"].base_path
    AGENTS["opencode"].base_path = tmp_path
    AGENTS["claude-code"].base_path = cc_base
    try:
        result = runner.invoke(app, ["opencode/ses_src", "claude-code"])
        assert result.exit_code == 0, result.output
        new_id = result.stdout.split("claude-code session:")[-1].strip().split()[0]
        # Source cwd is '/tmp' -> encoded project dir '-tmp'.
        path = cc_base / "projects" / "-tmp" / f"{new_id}.jsonl"
        assert path.exists(), f"expected the copy under {path}"
    finally:
        AGENTS["opencode"].base_path = original_oc
        AGENTS["claude-code"].base_path = original_cc


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

        def fake_import(remote, agent, records, cwd=None):
            return import_conversation(_mirror(agent), records, cwd=cwd)

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


@pytest.mark.parametrize("dest", ["ssh://chris@remote/", "ssh://chris@remote"])
def test_remote_destination_defaults_to_source_agent(tmp_path, monkeypatch, dest):
    """ccopy opencode/ses ssh://host/ (no agent named) copies into the same
    agent, opencode, on the remote."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    remote_home = tmp_path / "remote"
    (remote_home / ".local" / "share" / "opencode").mkdir(parents=True)
    _make_opencode_conversation_db(remote_home / ".local" / "share" / "opencode",
                                   "ses_remote_existing")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    _mock_transport(monkeypatch, remote_home, has_ctools=True)
    try:
        result = runner.invoke(app, ["opencode/ses_src", dest])
        assert result.exit_code == 0, result.output
        assert "new opencode session" in result.stdout
        import sqlite3
        conn = sqlite3.connect(str(remote_home / ".local" / "share" / "opencode" / "opencode.db"))
        ids = [r[0] for r in conn.execute("SELECT id FROM session")]
        conn.close()
        assert result.stdout.strip().split()[-1] in ids
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_destination_default_agent_dry_run(tmp_path):
    """The dry run names the agent it defaulted to."""
    _make_opencode_conversation_db(tmp_path, "ses_src")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        result = runner.invoke(app, ["opencode/ses_src", "ssh://chris@remote/", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "to a new opencode session" in result.stdout
        assert "chris@remote" in result.stdout
    finally:
        AGENTS["opencode"].base_path = original


def test_remote_destination_without_agent_from_json_file_is_error(tmp_path):
    """A conversation file has no agent to default to, so one must be named."""
    src = tmp_path / "conv.json"
    src.write_text(json.dumps([{"role": "user", "content": "hi"}]))
    result = runner.invoke(app, [str(src), "ssh://chris@remote/"])
    assert result.exit_code == 1
    assert "No destination agent" in result.output
    assert "ssh://chris@remote/AGENT" in result.output


def test_remote_source_wire_failure_is_reported_not_pulled(tmp_path, monkeypatch):
    """If the remote runs a new enough ctools but the wire export fails (e.g.
    unknown session there), ccopy reports what the far side said instead of
    falling back to pulling the agent's storage."""
    remote_home = tmp_path / "remote"
    (remote_home / ".local" / "share" / "opencode").mkdir(parents=True)
    _make_opencode_conversation_db(tmp_path, "ses_dummy")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    _mock_transport(monkeypatch, remote_home, has_ctools=True)
    import ctools.ccopy as ccopy

    def broken_export(remote, agent, session_id):
        raise ccopy.RemoteWireError("ccat --raw opencode/ses_x failed on chris@remote "
                                    "(exit 1): no such session")

    def no_tar(*a, **kw):
        raise AssertionError("a new enough remote ctools must not trigger the tar pull")

    monkeypatch.setattr(ccopy, "_remote_common_doc", broken_export)
    monkeypatch.setattr(ccopy, "_pull", no_tar)
    try:
        result = runner.invoke(app, ["ssh://chris@remote/opencode/ses_x", "opencode"])
        assert result.exit_code == 1
        assert "no such session" in result.output
    finally:
        AGENTS["opencode"].base_path = original


def _completed(rc, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


def test_remote_has_ctools_probe(monkeypatch):
    """_remote_has_ctools asks the remote's ccopy for its version, with the
    user bin dirs on PATH, and caches the answer per host."""
    import ctools.ccopy as ccopy
    from ctools.cli import Remote
    from unittest.mock import patch

    ccopy._ctools_probes.clear()
    remote = Remote(host="probehost")

    with patch.object(ccopy, "_run", return_value=_completed(0, "ccopy (ctxttools) 0.4.0\n")) as probe, \
            patch.object(ccopy.shutil, "which", return_value="/usr/bin/ssh"):
        assert ccopy._remote_has_ctools(remote) is True
        command = probe.call_args.args[0][-1]
        assert '$HOME/.local/bin' in command and "ccopy --version" in command
        with patch.object(ccopy, "_run") as second:
            assert ccopy._remote_has_ctools(remote) is True  # cached
            second.assert_not_called()

    ccopy._ctools_probes.clear()
    with patch.object(ccopy, "_run", return_value=_completed(3)), \
            patch.object(ccopy.shutil, "which", return_value="/usr/bin/ssh"):
        assert ccopy._remote_has_ctools(remote) is False
    ccopy._ctools_probes.clear()


@pytest.mark.parametrize("version_line", ["ccopy (ctxttools) 0.3.1", ""])
def test_remote_has_ctools_too_old(monkeypatch, capsys, version_line):
    """A remote ctools older than WIRE_MIN_VERSION (or one that can't say)
    means the tar path, with a note saying why and how to upgrade."""
    import ctools.ccopy as ccopy
    from ctools.cli import Remote
    from unittest.mock import patch

    ccopy._ctools_probes.clear()
    remote = Remote(host="oldhost", user="chris")
    with patch.object(ccopy, "_run", return_value=_completed(0, version_line + "\n")), \
            patch.object(ccopy.shutil, "which", return_value="/usr/bin/ssh"):
        assert ccopy._remote_has_ctools(remote) is False
        assert ccopy._remote_has_ctools(remote) is False
    err = " ".join(capsys.readouterr().err.split())  # undo Rich's wrapping
    assert err.count("needs ctools 0.4.0") == 1  # said once, not per call
    assert "uv tool upgrade ctxttools" in err and "pipx upgrade ctxttools" in err
    ccopy._ctools_probes.clear()


def test_remote_common_doc_parses_envelope(monkeypatch):
    """_remote_common_doc parses the envelope JSON the far side streams, and
    raises RemoteWireError, carrying the far side's message, on bad output or
    a non-zero exit."""
    import ctools.ccopy as ccopy
    from ctools.cli import Remote
    from unittest.mock import patch
    remote = Remote(host="probehost")
    agent = AGENTS["opencode"]
    doc = {'context': [{'role': 'user', 'content': 'hi'}]}

    with patch.object(ccopy, "_run", return_value=_completed(0, json.dumps(doc))) as call:
        assert ccopy._remote_common_doc(remote, agent, "ses_x") == doc
        assert '$HOME/.local/bin' in call.call_args.args[0][-1]

    with patch.object(ccopy, "_run", return_value=_completed(1, err="nope")):
        with pytest.raises(ccopy.RemoteWireError, match="nope"):
            ccopy._remote_common_doc(remote, agent, "ses_x")

    with patch.object(ccopy, "_run", return_value=_completed(0, "not json")):
        with pytest.raises(ccopy.RemoteWireError):
            ccopy._remote_common_doc(remote, agent, "ses_x")


def test_remote_import_records_reads_id(monkeypatch):
    """_remote_import_records returns the id the far side prints, or raises
    RemoteWireError on failure."""
    import ctools.ccopy as ccopy
    from ctools.cli import Remote
    from unittest.mock import patch
    remote = Remote(host="probehost")
    agent = AGENTS["opencode"]
    records = [{'role': 'user', 'content': 'hi'}]

    with patch.object(ccopy, "_run", return_value=_completed(0, "ses_new_1\n")) as call:
        assert ccopy._remote_import_records(remote, agent, records) == "ses_new_1"
        # the records were piped in as JSON on stdin
        assert call.call_args.kwargs["input_text"] == json.dumps(records)

    with patch.object(ccopy, "_run", return_value=_completed(1, err="boom")):
        with pytest.raises(ccopy.RemoteWireError, match="boom"):
            ccopy._remote_import_records(remote, agent, records)


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


@pytest.mark.live_ssh
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


# --- End-to-end remote copy over a local sshd (no external host required) ---

SESSION_ID = "01a1b000-0000-4000-8000-000000000001"

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _new_session_id(result) -> str:
    """The id of the session ccopy just created (last UUID on stdout)."""
    ids = _UUID_RE.findall(result.stdout or "")
    assert ids, f"no new session id in stdout: {result.stdout!r}"
    return ids[-1]


@pytest.fixture
def _remote_isolated_home(local_sshd, tmp_path, monkeypatch):
    """Isolate everything a real remote path would otherwise touch:
    every agent's local storage points at temp dirs, and the ssh client is
    pointed at a temp known_hosts/identity so the developer's real
    ``~/.ssh`` and storage are never read or written. The remote side is
    unaffected (its $HOME is the fixture's fake home)."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setenv("SSH_CONFIG_DIR", str(home / ".ssh"))
    monkeypatch.setattr("ctools.cli.REMOTE_SSH_OPTIONS",
                        ["-o", "BatchMode=yes", "-F", "/dev/null",
                         "-o", "StrictHostKeyChecking=no",
                         "-o", "UserKnownHostsFile=/dev/null",
                         "-i", local_sshd["key"]])
    for agent in AGENTS.values():
        base = tmp_path / "agents" / agent.name
        base.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(agent, "base_path", base)
        # A copy backs up the destination's live storage before writing, so
        # directory agents need their storage dir to exist (it does on a real
        # machine where the agent is installed). DB agents leave storage_path
        # alone (a .db path, not a dir).
        if getattr(agent, "files_read", None):
            agent.storage_path.mkdir(parents=True, exist_ok=True)
    yield tmp_path


@pytest.mark.live_ssh
def test_remote_cdir_listing(local_sshd, _remote_isolated_home):
    """cdir ssh://host:port/pi lists the remote pi session over one ssh."""
    import ctools.cdir as cdir
    result = runner.invoke(cdir.app,
                           ["ssh://127.0.0.1:%d/pi" % local_sshd["port"]])
    assert result.exit_code == 0, result.stderr
    assert SESSION_ID in result.stdout
    assert "hello from the remote" in result.stdout
    assert "Read a session with:" in result.stdout


@pytest.mark.live_ssh
def test_remote_cdir_host_lists_installed(local_sshd, _remote_isolated_home):
    """cdir ssh://host:port reports which agents the remote actually has."""
    import ctools.cdir as cdir
    result = runner.invoke(cdir.app,
                           ["ssh://127.0.0.1:%d" % local_sshd["port"]])
    assert result.exit_code == 0, result.stderr
    assert "Found:" in result.stdout
    assert "  pi" in result.stdout


@pytest.mark.live_ssh
def test_remote_ccat_wire(local_sshd, _remote_isolated_home):
    """ccat's wire mode (remote `ccat --raw` over ssh) returns the full
    conversation for a remote ref."""
    import ctools.ccat as ccat
    from ctools.cli import Remote
    remote = Remote(host="127.0.0.1", port=local_sshd["port"])
    doc = ccat._remote_common_doc(remote, AGENTS["pi"], SESSION_ID)
    text = "\n".join(str(m.get("content") or "") for m in doc["context"])
    assert "hello from the remote" in text
    assert "hello from the remote reply" in text


@pytest.mark.live_ssh
def test_ccopy_remote_wire_copy(local_sshd, _remote_isolated_home):
    """ccopy ssh://host:port/pi/<id> pi where the remote runs ctools: the
    wire path (remote `ccat --raw` over one ssh) feeds a new local pi
    session."""
    import ctools.ccopy as ccopy
    ref = "ssh://127.0.0.1:%d/pi/%s" % (local_sshd["port"], SESSION_ID)
    result = runner.invoke(ccopy.app, [ref, "pi"])
    assert result.exit_code == 0, result.stderr + result.stdout
    new_id = _new_session_id(result)
    dest = _remote_isolated_home / "agents" / "pi"
    files = list(dest.glob("**/*.jsonl"))
    assert files, "no local session file written"
    agent = AGENTS["pi"]
    agent.base_path = dest
    joined = "\n".join(str(m.content or "") for m in agent.messages(new_id))
    assert "hello from the remote" in joined
    assert "hello from the remote reply" in joined


@pytest.mark.live_ssh
def test_ccopy_remote_pull(local_sshd, _remote_isolated_home, monkeypatch):
    """The real remote pull path: when the remote has no ctools (probe
    fails), ccopy pulls the agent's raw storage with tar over ssh and reads
    it locally. The tar + local read run for real."""
    import ctools.ccopy as ccopy
    ref = "ssh://127.0.0.1:%d/pi/%s" % (local_sshd["port"], SESSION_ID)
    # Force the 'remote has no ctools' branch; _pull (tar) + the local read
    # still run for real.
    monkeypatch.setattr(ccopy, "_remote_has_ctools", lambda remote: False)
    pulled = []
    real_pull = ccopy._pull

    def _spy(remote, agent, dest_base):
        pulled.append(dest_base)
        return real_pull(remote, agent, dest_base)

    monkeypatch.setattr(ccopy, "_pull", _spy)
    result = runner.invoke(ccopy.app, [ref, "pi"])
    assert pulled, "the tar pull path did not run"
    assert result.exit_code == 0, result.stderr + result.stdout
    new_id = _new_session_id(result)
    dest = _remote_isolated_home / "agents" / "pi"
    files = list(dest.glob("**/*.jsonl"))
    assert files, "fallback did not write a local session file"
    agent = AGENTS["pi"]
    agent.base_path = dest
    joined = "\n".join(str(m.content or "") for m in agent.messages(new_id))
    assert "hello from the remote" in joined
    assert "hello from the remote reply" in joined


# --- resume ssh://... : resolve there, copy here, hand off here ---

def _intercept_exec(monkeypatch):
    """Replace the hand-off's exec; returns the list its argv is appended to."""
    import os
    box = []

    def fake(file, args, env):
        box.append(list(args))
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvpe", fake)
    return box


def _resumed_id(argv) -> str:
    """The session id the intercepted `sh -c 'pi --session <id>'` targets."""
    assert argv, "the hand-off never reached the exec"
    parts = argv[-1].split()
    assert parts[:2] == ["pi", "--session"], argv
    return parts[-1]


@pytest.mark.live_ssh
def test_resume_remote_session_ref(local_sshd, _remote_isolated_home, monkeypatch):
    """resume ssh://host:port/pi/<id> copies that remote session into a new
    local pi session and execs the resume command for the copy."""
    import ctools.resume as resume
    box = _intercept_exec(monkeypatch)
    ref = "ssh://127.0.0.1:%d/pi/%s" % (local_sshd["port"], SESSION_ID)
    result = runner.invoke(resume.app, [ref])
    assert result.exit_code == 0, result.stderr + result.stdout
    new_id = _resumed_id(box[0])
    assert new_id != SESSION_ID, "resumed the remote id instead of the local copy"
    agent = AGENTS["pi"]
    joined = "\n".join(str(m.content or "") for m in agent.messages(new_id))
    assert "hello from the remote" in joined
    assert "hello from the remote reply" in joined


@pytest.mark.live_ssh
def test_resume_remote_directory(local_sshd, _remote_isolated_home, monkeypatch):
    """resume ssh://host:port/<dir> asks the remote which session ran in that
    directory (its own `resume --resolve`), then copies and resumes it here."""
    import ctools.resume as resume
    box = _intercept_exec(monkeypatch)
    # The fixture's remote session records cwd /tmp/remote.
    ref = "ssh://127.0.0.1:%d/tmp/remote" % local_sshd["port"]
    result = runner.invoke(resume.app, [ref])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert _resumed_id(box[0]) != SESSION_ID
    assert SESSION_ID in result.stderr, "the remote resolution is not reported"


@pytest.mark.live_ssh
def test_resume_remote_unknown_directory(local_sshd, _remote_isolated_home):
    """A directory with no session there is reported, not guessed at."""
    import ctools.resume as resume
    ref = "ssh://127.0.0.1:%d/tmp/nothing-here" % local_sshd["port"]
    result = runner.invoke(resume.app, [ref])
    assert result.exit_code == 1
    assert "no session recorded" in (result.stdout + result.stderr)


# --- cp(1)-style flags: -t -n -u -i -r ---

def _opencode_at(tmp_path, *session_ids):
    """Point the opencode agent at a throwaway store seeded with `session_ids`."""
    for sid in session_ids:
        _make_opencode_conversation_db(tmp_path, sid)
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    return original


def _new_copy_id(result) -> str:
    """The id of the session the copy just created (what `session:` names).

    Agents name sessions differently (opencode `ses_...`, claude-code a uuid),
    so read the id off the report rather than its shape. The last one is the
    parent copy: `-r` prints its children first.
    """
    ids = re.findall(r"session:\s*(\S+)", result.stdout or "")
    assert ids, f"no new session id in stdout: {result.stdout!r}"
    return ids[-1]


def test_target_directory_seeds_the_copy(tmp_path):
    """`-t DIR` is `--cwd DIR`: the copy is seeded for that working directory.

    Without it the copy is seeded for the *source's* directory; naming a
    target moves where the destination files it, which is where a resume
    looks for it.
    """
    _make_opencode_conversation_db(tmp_path, "ses_src")  # source directory = '/tmp'
    cc_base = tmp_path / "cc"
    (cc_base / "projects").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "chosen"
    original_oc = AGENTS["opencode"].base_path
    original_cc = AGENTS["claude-code"].base_path
    AGENTS["opencode"].base_path = tmp_path
    AGENTS["claude-code"].base_path = cc_base
    try:
        result = runner.invoke(app, ["-t", str(target),
                                     "opencode/ses_src", "claude-code"])
        assert result.exit_code == 0, result.stderr + result.stdout
        new_id = _new_copy_id(result)
        # claude-code encodes a cwd into its project-dir name that way.
        encoded = ''.join(c if c.isalnum() else '-' for c in str(target))
        assert (cc_base / "projects" / encoded / f"{new_id}.jsonl").exists()
        assert not (cc_base / "projects" / "-tmp").exists()
    finally:
        AGENTS["opencode"].base_path = original_oc
        AGENTS["claude-code"].base_path = original_cc


def test_target_and_cwd_are_one_option(tmp_path):
    """`-t`, `--cwd` and `--target` are one option, so they cannot disagree.

    Three spellings of a single option: the last one wins, exactly as any
    other aliased flag, so there is no conflict for ccopy to report.
    """
    _opencode_at(tmp_path, "ses_src")
    result = runner.invoke(app, ["-t", "/a", "--cwd", "/b",
                                 "opencode/ses_src", "opencode"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert "/a" not in result.stdout
    result = runner.invoke(app, ["--target", "/b", "opencode/ses_src", "opencode"])
    assert result.exit_code == 0, result.stderr + result.stdout


def test_no_clobber_skips_an_existing_copy(tmp_path):
    """`-n` refuses to add a second copy of a session the store already holds."""
    _opencode_at(tmp_path, "ses_src")
    first = runner.invoke(app, ["opencode/ses_src", "opencode"])
    assert first.exit_code == 0, first.stderr + first.stdout
    result = runner.invoke(app, ["-n", "opencode/ses_src", "opencode"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert "not overwriting" in result.stdout
    assert "already holds a copy" in result.stdout


def test_without_no_clobber_a_second_copy_is_made(tmp_path):
    """The default is cp's overwrite: each run adds a copy."""
    _opencode_at(tmp_path, "ses_src")
    assert runner.invoke(app, ["opencode/ses_src", "opencode"]).exit_code == 0
    result = runner.invoke(app, ["opencode/ses_src", "opencode"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert "not overwriting" not in result.stdout


def test_update_skips_a_copy_that_is_not_older(tmp_path, monkeypatch):
    """`-u` copies only when the source moved past the copy; equal is not newer."""
    import ctools.ccopy as ccopy
    _opencode_at(tmp_path, "ses_src")
    assert runner.invoke(app, ["opencode/ses_src", "opencode"]).exit_code == 0
    monkeypatch.setattr(ccopy, "_session_mtime", lambda agent, sid: 100.0)
    result = runner.invoke(app, ["-u", "opencode/ses_src", "opencode"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert "skipping" in result.stdout and "not older" in result.stdout


def test_update_copies_a_newer_source(tmp_path, monkeypatch):
    """`-u` does copy when the source is newer than the copy it found."""
    import ctools.ccopy as ccopy
    _opencode_at(tmp_path, "ses_src")
    assert runner.invoke(app, ["opencode/ses_src", "opencode"]).exit_code == 0
    # The source has moved on since the copy was made.
    monkeypatch.setattr(ccopy, "_session_mtime",
                        lambda agent, sid: 200.0 if sid == "ses_src" else 100.0)
    result = runner.invoke(app, ["-u", "opencode/ses_src", "opencode"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert "not older" not in result.stdout and "skipping" not in result.stdout


def test_update_copies_when_no_copy_exists_yet(tmp_path):
    """With nothing copied yet, -u is just a copy."""
    _opencode_at(tmp_path, "ses_src")
    result = runner.invoke(app, ["-u", "opencode/ses_src", "opencode"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert "not older" not in result.stdout


def test_interactive_abort_writes_nothing(tmp_path, monkeypatch):
    """Answering `cp -i` with anything but y leaves the destination alone."""
    import ctools.ccopy as ccopy
    _opencode_at(tmp_path, "ses_src")
    monkeypatch.setattr(ccopy, "_confirm", lambda q: False)
    result = runner.invoke(app, ["-i", "opencode/ses_src", "opencode"])
    assert result.exit_code == 1
    assert "aborted" in result.stdout
    assert [s.id for s in AGENTS["opencode"].sessions()] == ["ses_src"]


def test_interactive_yes_copies(tmp_path, monkeypatch):
    import ctools.ccopy as ccopy
    monkeypatch.setattr(ccopy, "_confirm", lambda q: True)
    _opencode_at(tmp_path, "ses_src")
    result = runner.invoke(app, ["-i", "opencode/ses_src", "opencode"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert "Copied" in result.stdout


def test_confirm_asks_only_when_there_is_a_human(monkeypatch):
    """`cp -i` on a pipe has nobody to ask: it proceeds and says so."""
    import ctools.ccopy as ccopy

    class _NoTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(ccopy.sys, "stdin", _NoTTY())
    monkeypatch.setattr(ccopy.sys, "stdout", _NoTTY())
    assert ccopy._confirm("copy? ") is True


def test_confirm_reads_the_answer(monkeypatch):
    import ctools.ccopy as ccopy

    class _TTY:
        def isatty(self):
            return True

    monkeypatch.setattr(ccopy.sys, "stdin", _TTY())
    monkeypatch.setattr(ccopy.sys, "stdout", _TTY())
    answers = iter(["n", "y", "Y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert ccopy._confirm("copy? ") is False
    assert ccopy._confirm("copy? ") is True
    assert ccopy._confirm("copy? ") is True


def test_recursive_copies_subagent_sessions(tmp_path, monkeypatch):
    """`cp -r` brings the subagent sessions along, each as its own copy."""
    import ctools.ccopy as ccopy
    _opencode_at(tmp_path, "ses_src")
    monkeypatch.setattr(ccopy, "_child_ids", lambda src, sid, remote=None: ["ses_kid"])
    monkeypatch.setattr(ccopy, "_fetch_common_doc",
                        lambda remote, agent, sid: agent.to_common("ses_src"))
    result = runner.invoke(app, ["-r", "opencode/ses_src", "opencode"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert result.stdout.count("Copied") == 2, result.stdout


def test_child_ids_reads_the_parent_link(tmp_path):
    """`_child_ids` is the storage's own parent link, not a name pattern."""
    import ctools.ccopy as ccopy
    _opencode_at(tmp_path, "ses_src")
    assert ccopy._child_ids(AGENTS["opencode"], "ses_src") == []
