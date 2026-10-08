"""2N interchange tests.

The common format is the ccat --raw envelope. Every agent that can be seeded
is tested against the common format *independently* (to_common / from_common),
so the N^2 "A->B works" question falls out as the composition. A cross-agent
copy is only as good as the two agents' individual to/from functions plus the
oracle that proves the written session actually loads in the real agent.

Oracle strength is capability-based:
  * opencode / pi have live oracles (the vendor tool re-reads our write).
  * agents without a CLI fall back to a structural read-back through our reader.
"""
import json
import os
import shutil
import sqlite3
import subprocess

import pytest

from ctools.agents import REGISTRY as AGENTS, get_agent


# --- Fixtures: a minimal opencode DB and a minimal pi session ---

def _opencode_db(tmp_path, session_id="ses_snake",
                 messages=None):
    """A minimal real-shaped opencode DB with a snake-game conversation."""
    if messages is None:
        messages = [
            ("user", "make a simple snake game in python"),
            ("assistant", "Created `snake.py`. Run it with: python3 snake.py"),
        ]
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    for table, cols in {
        "session": ("id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, "
                    "slug TEXT, directory TEXT, title TEXT, version TEXT, "
                    "share_url TEXT, time_created INTEGER, time_updated INTEGER, "
                    "cost REAL, tokens_input INTEGER, tokens_output INTEGER, "
                    "tokens_reasoning INTEGER, tokens_cache_read INTEGER, "
                    "tokens_cache_write INTEGER"),
        "message": ("id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, "
                    "time_updated INTEGER, data TEXT"),
        "part": ("id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
                 "time_created INTEGER, time_updated INTEGER, data TEXT"),
    }.items():
        cur.execute(f"CREATE TABLE {table} ({cols})")
    cur.execute(
        "INSERT INTO session (id, title, time_created, time_updated, "
        "tokens_input, tokens_output, directory) VALUES (?, 'snake', "
        "1700000000000, 1700000060000, 100, 200, '/tmp')", (session_id,))
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


def _pi_session(tmp_path, session_id, messages=None):
    """A minimal pi JSONL session (version-3 header + message entries)."""
    if messages is None:
        messages = [
            ("user", "make a simple snake game in python"),
            ("assistant", "Created `snake.py`. Run it with: python3 snake.py"),
        ]
    import time as _time, uuid as _uuid
    cwd = "/tmp"
    safe = f"--{cwd.lstrip('/').replace('/', '-')}--"
    session_dir = tmp_path / "sessions" / safe
    session_dir.mkdir(parents=True, exist_ok=True)
    now_iso = _time.strftime("%Y-%m-%dT%H:%M:%S.000000")
    lines = [json.dumps({"type": "session", "version": 3, "id": session_id,
                         "timestamp": now_iso, "cwd": cwd,
                         "name": "snake"})]
    prev = None
    for i, (role, content) in enumerate(messages):
        entry = {"type": "message", "id": _uuid.uuid4().hex[:8],
                 "parentId": prev, "timestamp": now_iso,
                 "message": {"role": role, "content": content,
                             "timestamp": 1700000000000 + i * 1000}}
        lines.append(json.dumps(entry))
        prev = entry["id"]
    path = session_dir / f"{1700000000}_{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


# --- Live-oracle availability (skip gracefully if the CLI is absent) ---

def _which(binary):
    return shutil.which(binary) is not None


# =====================================================================
# opencode: to_common / from_common + live oracle
# =====================================================================

def test_opencode_to_common_shape(tmp_path):
    """to_common emits the envelope with context + lossless raw."""
    _opencode_db(tmp_path, "ses_snake")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        doc = AGENTS["opencode"].to_common("ses_snake")
        assert [r["role"] for r in doc["context"]] == ["user", "assistant"]
        assert doc["context"][0]["content"] == "make a simple snake game in python"
        assert doc["source"] == "opencode/ses_snake"
        assert "raw" in doc and len(doc["raw"]) == 2  # lossless verbatim records
    finally:
        AGENTS["opencode"].base_path = original


def test_opencode_from_common_roundtrip(tmp_path):
    """to_common -> from_common reproduces the conversation in a new session."""
    _opencode_db(tmp_path, "ses_snake")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        doc = AGENTS["opencode"].to_common("ses_snake")
        new_id = AGENTS["opencode"].from_common(doc)
        assert new_id != "ses_snake"
        msgs = AGENTS["opencode"].messages(new_id)
        assert [m.content for m in msgs] == [
            "make a simple snake game in python",
            "Created `snake.py`. Run it with: python3 snake.py",
        ]
    finally:
        AGENTS["opencode"].base_path = original


def _opencode_export_readable(agent, session_id):
    """Read back a session through the real `opencode export` CLI and confirm
    the conversation survives. Returns (ok, detail)."""
    try:
        proc = subprocess.run(
            ["opencode", "export", session_id],
            capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"opencode export failed: {e}"
    lines = proc.stdout.splitlines()
    doc = None
    for i, line in enumerate(lines):
        if line.strip() == "{":
            try:
                doc = json.loads("\n".join(lines[i:]))
            except json.JSONDecodeError as e:
                return False, f"export JSON unparseable: {e}"
            break
    if doc is None:
        return False, "no JSON object in export output"
    msgs = doc.get("messages", [])
    if not msgs:
        return False, "export has no messages"
    # Confirm each message's text is recoverable from its parts.
    recovered = []
    for m in msgs:
        text = "".join(p.get("text", "") for p in m.get("parts", [])
                       if p.get("type") == "text")
        recovered.append((m.get("info", {}).get("role"), text))
    return True, recovered


@pytest.mark.skipif(not _which("opencode"), reason="opencode CLI not installed")
def test_opencode_from_common_live_oracle(tmp_path):
    """The written session is re-readable by the real opencode tool (oracle).

    A live oracle must write into the REAL opencode storage, because
    `opencode export` reads ~/.local/share/opencode/opencode.db -- our temp
    base_path is invisible to it. We write there, verify, then clean up.
    """
    # Build the envelope from a temp fixture (no real writes yet).
    _opencode_db(tmp_path, "ses_snake")
    AGENTS["opencode"].base_path = tmp_path
    try:
        doc = AGENTS["opencode"].to_common("ses_snake")
    finally:
        AGENTS["opencode"].base_path = AGENTS["opencode"].default_base_path()
    # Now write into the REAL opencode db (where the CLI looks). Always reset to
    # the default base path, not a possibly-stale current value.
    real_orig = AGENTS["opencode"].default_base_path()
    new_id = ""  # set by from_common; cleaned up in finally if non-empty
    try:
        AGENTS["opencode"].base_path = real_orig
        new_id = AGENTS["opencode"].from_common(doc)
        ok, detail = _opencode_export_readable(AGENTS["opencode"], new_id)
        assert ok, f"oracle failed: {detail}"
        assert detail[0][0] == "user"
    finally:
        # Clean up the session we created in the real db.
        if new_id:
            _drop_opencode_session(new_id)
        AGENTS["opencode"].base_path = real_orig


def _drop_opencode_session(session_id):
    """Remove a session (and its messages/parts) from the real opencode db."""
    import os
    db = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if not os.path.exists(db):
        return
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM message WHERE session_id=?", (session_id,))
        for (mid,) in cur.fetchall():
            cur.execute("DELETE FROM part WHERE message_id=?", (mid,))
            cur.execute("DELETE FROM message WHERE id=?", (mid,))
        cur.execute("DELETE FROM session WHERE id=?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def test_opencode_from_common_real_shape(tmp_path):
    """The written rows carry the fields opencode's runtime needs to load."""
    _opencode_db(tmp_path, "ses_snake")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        doc = AGENTS["opencode"].to_common("ses_snake")
        new_id = AGENTS["opencode"].from_common(doc)
        conn = sqlite3.connect(str(AGENTS["opencode"].db_path))
        cur = conn.cursor()
        cur.execute("SELECT data FROM message WHERE session_id=? ORDER BY time_created",
                    (new_id,))
        rows = [json.loads(r[0]) for r in cur.fetchall()]
        conn.close()
        user, assistant = rows[0], rows[1]
        # Every message has a time block (the TUI reads time.created).
        assert "time" in user and "created" in user["time"]
        assert "time" in assistant and "created" in assistant["time"]
        # Assistant messages carry a parent link and a model (TUI needs these).
        assert "parentID" in assistant
        assert assistant.get("providerID")
        # Parts are linked back to message + session by id.
        cur = sqlite3.connect(str(AGENTS["opencode"].db_path)).cursor()
        cur.execute("SELECT data FROM part WHERE session_id=? ORDER BY time_created",
                    (new_id,))
        parts = [json.loads(r[0]) for r in cur.fetchall()]
        for p in parts:
            assert p["messageID"] and p["sessionID"] and p["id"]
    finally:
        AGENTS["opencode"].base_path = original


# =====================================================================
# pi: to_common / from_common + live oracle
# =====================================================================

def test_pi_to_common_shape(tmp_path):
    _pi_session(tmp_path, "pi_snake")
    original = AGENTS["pi"].base_path
    AGENTS["pi"].base_path = tmp_path
    try:
        doc = AGENTS["pi"].to_common("pi_snake")
        assert [r["role"] for r in doc["context"]] == ["user", "assistant"]
        assert doc["context"][0]["content"] == "make a simple snake game in python"
        assert doc["source"] == "pi/pi_snake"
        assert "raw" in doc  # pi verbatim entries
    finally:
        AGENTS["pi"].base_path = original


def test_pi_from_common_roundtrip(tmp_path):
    _pi_session(tmp_path, "pi_snake")
    original = AGENTS["pi"].base_path
    AGENTS["pi"].base_path = tmp_path
    try:
        doc = AGENTS["pi"].to_common("pi_snake")
        new_id = AGENTS["pi"].from_common(doc)
        assert new_id != "pi_snake"
        msgs = AGENTS["pi"].messages(new_id)
        assert [m.content for m in msgs] == [
            "make a simple snake game in python",
            "Created `snake.py`. Run it with: python3 snake.py",
        ]
    finally:
        AGENTS["pi"].base_path = original


@pytest.mark.skipif(not _which("pi"), reason="pi CLI not installed")
def test_pi_from_common_live_oracle(tmp_path, monkeypatch):
    """The written pi session loads via `pi --session <id>` (exit 0).

    pi --session reads ~/.pi/agent, so the oracle must write to the real pi
    storage. pi discovers sessions under per-cwd subdirs; from_common writes
    under the current cwd, so we run pi from a scratch cwd and clean up after.
    """
    _pi_session(tmp_path, "pi_snake")
    AGENTS["pi"].base_path = tmp_path
    try:
        doc = AGENTS["pi"].to_common("pi_snake")
    finally:
        AGENTS["pi"].base_path = AGENTS["pi"].default_base_path()
    # Write into the REAL pi storage (where the CLI looks). Always reset to the
    # default base path, not whatever the current (possibly stale) value is, so
    # the oracle writes where `pi --session` will actually look.
    real_orig = AGENTS["pi"].default_base_path()
    new_id = ""  # set by from_common; cleaned up in finally if non-empty
    # pi keys sessions by cwd; write and read under one explicit, fixed cwd so
    # the from_common write and the pi subprocess always agree (pytest may
    # chdir between calls).
    work_cwd = os.getcwd()
    try:
        AGENTS["pi"].base_path = real_orig
        new_id = AGENTS["pi"].from_common(doc)
        proc = subprocess.run(["pi", "--session", new_id],
                              capture_output=True, text=True, timeout=60,
                              cwd=work_cwd)
        assert proc.returncode == 0, proc.stderr
    finally:
        # Remove the session file we created in real pi storage.
        import glob
        for f in glob.glob(os.path.expanduser("~/.pi/agent/sessions/*/*" + new_id + "*")):
            os.remove(f)
        AGENTS["pi"].base_path = real_orig


# =====================================================================
# cross-agent composition (the N^2 that falls out of 2N)
# =====================================================================

def test_cross_agent_opencode_to_pi(tmp_path):
    """opencode snake -> common -> pi keeps the prompt + answer."""
    _opencode_db(tmp_path, "ses_snake")
    oc_orig, pi_orig = AGENTS["opencode"].base_path, AGENTS["pi"].base_path
    AGENTS["opencode"].base_path = tmp_path
    pi_dest = tmp_path / "pi_dest"
    pi_dest.mkdir()
    AGENTS["pi"].base_path = pi_dest
    try:
        doc = AGENTS["opencode"].to_common("ses_snake")
        new_id = AGENTS["pi"].from_common(doc)
        msgs = AGENTS["pi"].messages(new_id)
        assert msgs[0].content == "make a simple snake game in python"
        assert any("snake.py" in m.content for m in msgs)
    finally:
        AGENTS["opencode"].base_path = oc_orig
        AGENTS["pi"].base_path = pi_orig


def test_cross_agent_pi_to_opencode(tmp_path):
    """pi snake -> common -> opencode keeps the prompt + answer."""
    _pi_session(tmp_path, "pi_snake")
    pi_orig, oc_orig = AGENTS["pi"].base_path, AGENTS["opencode"].base_path
    AGENTS["pi"].base_path = tmp_path
    oc_dest = tmp_path / "oc_dest"
    oc_dest.mkdir()
    _opencode_db(oc_dest, "ses_placeholder")  # ensure the schema exists
    AGENTS["opencode"].base_path = oc_dest
    try:
        doc = AGENTS["pi"].to_common("pi_snake")
        new_id = AGENTS["opencode"].from_common(doc)
        msgs = AGENTS["opencode"].messages(new_id)
        assert msgs[0].content == "make a simple snake game in python"
        assert any("snake.py" in m.content for m in msgs)
    finally:
        AGENTS["pi"].base_path = pi_orig
        AGENTS["opencode"].base_path = oc_orig


# =====================================================================
# capability-based degradation: agents without create refuse cleanly
# =====================================================================

def test_common_format_only_for_seeding_agents(tmp_path):
    """from_common works for seeding agents; non-seeding agents refuse."""
    _opencode_db(tmp_path, "ses_snake")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        doc = AGENTS["opencode"].to_common("ses_snake")
        # A seeding agent imports it fine.
        new_id = AGENTS["opencode"].from_common(doc)
        assert new_id.startswith("ses_")
        # A non-seeding agent (no create_session) refuses via from_common's
        # create_session UnsupportedOperation.
        non_seeding = next(
            (get_agent(n) for n in AGENTS
             if get_agent(n) is not None and not get_agent(n).supports_create()),
            None)
        if non_seeding is None:
            pytest.skip("no non-seeding agent available")
        with pytest.raises(Exception):
            non_seeding.from_common(doc)
    finally:
        AGENTS["opencode"].base_path = original


def test_common_format_missing_context_is_clean(tmp_path):
    """An envelope with no context degrades cleanly (empty session), not a crash."""
    _opencode_db(tmp_path, "ses_snake")
    original = AGENTS["opencode"].base_path
    AGENTS["opencode"].base_path = tmp_path
    try:
        # An envelope with a context-free shape should not crash; it yields an
        # empty (but valid) session rather than raising.
        new_id = AGENTS["opencode"].from_common({"source": "x/y"})
        assert new_id.startswith("ses_")
        assert AGENTS["opencode"].messages(new_id) == []
    finally:
        AGENTS["opencode"].base_path = original
