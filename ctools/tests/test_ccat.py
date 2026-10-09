"""Tests for ccat (cat for LLM context windows)."""
import json
import sqlite3
from ctools.testing import Runner

from ctools.ccat import app
from ctools.agents import REGISTRY as AGENTS

runner = Runner()


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


def test_ccat_single_session_json_default(tmp_path):
    """The default output is a JSON list of {role, content} (transportable)."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["opencode/ses_a"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data[0] == {"role": "user", "content": "Write a fibonacci function"}
    assert data[1]["role"] == "assistant"


def test_ccat_single_session_text(tmp_path):
    """--text prints a readable transcript with role labels."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["--text", "opencode/ses_a"])
    assert result.exit_code == 0
    assert "Write a fibonacci function" in result.stdout
    assert "fibonacci function: def fib" in result.stdout
    # role labels are present
    assert "you" in result.stdout
    assert "assistant" in result.stdout


def test_ccat_multiple_sessions_text(tmp_path):
    """--text: two sessions in order, separated by a blank line."""
    _make_opencode_conv_db(tmp_path, "ses_a",
                           [("user", "AAA first"), ("assistant", "resp A")])
    _conv(tmp_path, "ses_b", [("user", "BBB second"), ("assistant", "resp B")])
    result = _run_ccat(tmp_path,
                       ["--text", "--color", "never", "opencode/ses_a",
                        "opencode/ses_b"])
    assert result.exit_code == 0
    # AAA comes before BBB (order preserved)
    assert result.stdout.index("AAA first") < result.stdout.index("BBB second")
    # a blank line separates the two transcripts
    assert "\n\n" in result.stdout


def test_ccat_multiple_sessions_json(tmp_path):
    """Default (JSON) with two sessions prints two JSON arrays in order."""
    _make_opencode_conv_db(tmp_path, "ses_a",
                           [("user", "AAA first"), ("assistant", "resp A")])
    _conv(tmp_path, "ses_b", [("user", "BBB second"), ("assistant", "resp B")])
    result = _run_ccat(tmp_path,
                       ["opencode/ses_a", "opencode/ses_b"])
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
    """--text with --color never produces no ANSI escape codes."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["--text", "--color", "never", "opencode/ses_a"])
    assert "\x1b[" not in result.stdout


def test_ccat_transcript_role_alignment(tmp_path):
    """Role labels sit in a fixed column; content follows two spaces."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["--text", "--color", "never", "opencode/ses_a"])
    lines = result.stdout.splitlines()
    # Every transcript line starts with 4 spaces + a role label + 2 spaces.
    for line in lines:
        if line.strip():
            assert line.startswith("    ")
            # after the 4-space margin, the role label (<=9 chars) then "  "
            rest = line[4:]
            assert "  " in rest


# --- --raw lossless envelope ---

def test_ccat_raw_envelope_shape(tmp_path):
    """--raw prints the lossless envelope: context + source + raw + metadata."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["--raw", "opencode/ses_a"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    # context is the llcat/OpenAI spine (a list of {role, content})
    assert isinstance(doc["context"], list)
    assert [r["role"] for r in doc["context"]] == ["user", "assistant"]
    assert doc["context"][0]["content"] == "Write a fibonacci function"
    # source is the origin label
    assert doc["source"] == "opencode/ses_a"
    # session metadata is at the top, not per-record
    assert "created" in doc and "modified" in doc
    # lossless verbatim records are present and parallel to the raw messages
    assert "raw" in doc and len(doc["raw"]) == 2
    assert doc["raw"][0]["message"]["role"] == "user"
    assert doc["raw"][0]["parts"][0]["text"] == "Write a fibonacci function"


def test_ccat_raw_context_is_bare_llcat_spine(tmp_path):
    """The context records carry only role/content (no per-record meta)."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    result = _run_ccat(tmp_path, ["--raw", "opencode/ses_a"])
    doc = json.loads(result.stdout)
    for rec in doc["context"]:
        assert set(rec.keys()) == {"role", "content"}


def test_ccat_raw_omits_raw_when_unintrospectable(tmp_path):
    """When an agent can't introspect its storage, raw is omitted (honest)."""
    # An agent whose raw_records() returns [] must not emit an empty raw key.
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    # AGENTS["opencode"] is a singleton instance; patch the CLASS method so it
    # binds normally (an instance attribute would not receive self).
    opencode_cls = type(AGENTS["opencode"])
    original = opencode_cls.raw_records
    opencode_cls.raw_records = lambda self, sid: []
    try:
        result = _run_ccat(tmp_path, ["--raw", "opencode/ses_a"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert "raw" not in doc
    finally:
        opencode_cls.raw_records = original


def test_raw_records_base_defaults_empty():
    """Base agents that can't introspect return an empty raw list."""
    from ctools.agents import get_agent
    # cline is a base Agent (no custom raw_records), so it returns [].
    assert get_agent("cline").raw_records("anything") == []


def test_opencode_raw_records_roundtrip(tmp_path):
    """raw_records returns each message's verbatim message.data + part rows."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        recs = AGENTS["opencode"].raw_records("ses_a")
        assert len(recs) == 2
        assert recs[0]["message"]["role"] == "user"
        assert recs[0]["parts"][0]["type"] == "text"
        assert recs[1]["message"]["role"] == "assistant"
    finally:
        AGENTS["opencode"].base_path = original


def test_opencode_session_info(tmp_path):
    """session_info surfaces model + timestamps from the session row."""
    _make_opencode_conv_db(tmp_path, "ses_a", DEFAULT_CONV)
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        info = AGENTS["opencode"].session_info("ses_a")
        assert info is not None
        assert "created" in info and "modified" in info
    finally:
        AGENTS["opencode"].base_path = original


# --- Remote source: first pass (remote has ctools) vs second pass (tar) ---

def _remote_opencode_home(tmp_path, session_id):
    """Lay out a fake remote $HOME with an opencode db holding session_id."""
    remote_home = tmp_path / "remote"
    (remote_home / ".local" / "share" / "opencode").mkdir(parents=True)
    _make_opencode_conv_db(remote_home / ".local" / "share" / "opencode",
                           session_id, DEFAULT_CONV)
    return remote_home


def test_ccat_remote_first_pass_when_remote_has_ctools(tmp_path, monkeypatch):
    """When the remote runs ctools, ccat exports the envelope there over one
    ssh (the tar pull never happens)."""
    import ctools.ccat as ccat
    import ctools.ccopy as ccopy
    from pathlib import Path
    remote_home = _remote_opencode_home(tmp_path, "ses_a")
    _make_opencode_conv_db(tmp_path, "ses_dummy", DEFAULT_CONV)
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path

    # Model the remote ctools as a local agent anchored at the remote home.
    rel = Path(ccopy._remote_storage_rel(AGENTS["opencode"]))
    db_name = getattr(type(AGENTS["opencode"]), "db_name", None)
    mirror = type(AGENTS["opencode"])(remote_home / (rel.parent if db_name else rel))

    monkeypatch.setattr(ccat, "_remote_has_ctools", lambda r: True)
    monkeypatch.setattr(ccat, "_remote_common_doc",
                        lambda r, agent, sid: mirror.to_common(sid))

    def no_tar(*a, **kw):
        raise AssertionError("first pass: the tar transport must not run")
    monkeypatch.setattr(ccat, "_pull", no_tar)
    try:
        result = _run_ccat(tmp_path, ["ssh://chris@remote/opencode/ses_a"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert doc[0]["content"] == "Write a fibonacci function"
    finally:
        AGENTS["opencode"].base_path = original


def test_ccat_remote_first_pass_raw_envelope(tmp_path, monkeypatch):
    """--raw over a ctools-equipped remote returns the envelope (context,
    raw, metadata) from the remote's own export."""
    import ctools.ccat as ccat
    import ctools.ccopy as ccopy
    from pathlib import Path
    remote_home = _remote_opencode_home(tmp_path, "ses_a")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    rel = Path(ccopy._remote_storage_rel(AGENTS["opencode"]))
    db_name = getattr(type(AGENTS["opencode"]), "db_name", None)
    mirror = type(AGENTS["opencode"])(remote_home / (rel.parent if db_name else rel))
    monkeypatch.setattr(ccat, "_remote_has_ctools", lambda r: True)
    monkeypatch.setattr(ccat, "_remote_common_doc",
                        lambda r, agent, sid: mirror.to_common(sid))
    monkeypatch.setattr(ccat, "_pull", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("first pass: the tar transport must not run")))
    try:
        result = _run_ccat(tmp_path, ["--raw", "ssh://chris@remote/opencode/ses_a"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert isinstance(doc["context"], list)
        assert doc["source"] == "opencode/ses_a on chris@remote"
        assert "created" in doc and "modified" in doc
    finally:
        AGENTS["opencode"].base_path = original


def test_ccat_remote_falls_back_to_tar_when_no_ctools(tmp_path, monkeypatch):
    """When the remote has no ctools, ccat pulls the storage over ssh and
    reads it locally (the existing expensive path)."""
    import ctools.ccat as ccat
    import ctools.ccopy as ccopy
    remote_home = _remote_opencode_home(tmp_path, "ses_a")
    _make_opencode_conv_db(tmp_path, "ses_dummy", DEFAULT_CONV)
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path

    # No ctools on the remote -> second pass. Model _pull as a local file
    # copy of the remote db into the temp mirror.
    monkeypatch.setattr(ccat, "_remote_has_ctools", lambda r: False)

    def fake_pull(remote, agent, dest_base):
        rel = ccopy._remote_storage_rel(agent)
        src = remote_home / rel
        dst = dest_base / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    monkeypatch.setattr(ccat, "_pull", fake_pull)
    try:
        result = _run_ccat(tmp_path, ["ssh://chris@remote/opencode/ses_a"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert doc[0]["content"] == "Write a fibonacci function"
    finally:
        AGENTS["opencode"].base_path = original
