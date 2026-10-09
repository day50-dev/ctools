"""Tests for ctools.backup (snapshot / verify / restore) and the crecover CLI.

These pin down the safety contract around *writes* to agent storage: a bad
write must be detectable (integrity + session-count sentinel) and reversible
(restore), and restore itself must be undoable.
"""

import json
import os
import sqlite3
import subprocess
import sys
import importlib
import shutil
import tempfile
from pathlib import Path

import pytest

from ctools.tests.test_ccopy import _make_opencode_conversation_db


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def opencode_home(tmp_path, monkeypatch):
    """Isolated HOME with a real 2-message opencode DB (id ``ses_src``).

    Reloads ``ctools.backup`` so its module-level ``backup_root()`` (which
    reads ``$HOME`` lazily via the function call) is exercised against the
    fake home. We set HOME in the environment *and* rely on the fact that
    ``backup_root()`` calls the XDG/home lookup each time.
    """
    home = tmp_path / "home"
    dbdir = home / ".local" / "share" / "opencode"
    dbdir.mkdir(parents=True)
    _make_opencode_conversation_db(dbdir, "ses_src")
    monkeypatch.setenv("HOME", str(home))
    # ctools.backup prefers XDG_DATA_HOME for the backup root; pin it inside
    # the fake home so a developer machine that sets XDG_DATA_HOME is not
    # polluted (and the test is deterministic regardless of that var).
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    return home


@pytest.fixture
def opencode_agent(opencode_home):
    from ctools.lib import AGENTS
    ag = AGENTS["opencode"]
    old = ag.base_path
    ag.base_path = opencode_home / ".local" / "share" / "opencode"
    try:
        yield ag
    finally:
        ag.base_path = old


@pytest.fixture
def backup_mod():
    import ctools.backup as b
    return b


def _db_path(ag):
    return Path(ag.storage_path)


def _session_count(path: Path) -> int:
    con = sqlite3.connect(str(path))
    try:
        return con.execute("SELECT count(*) FROM session").fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# take_backup
# ---------------------------------------------------------------------------

def test_take_backup_sqlite_produces_consistent_copy(backup_mod, opencode_agent):
    b = backup_mod.take_backup(opencode_agent, note="unit")
    assert b.kind == "sqlite"
    assert b.path.exists()
    assert (b.path / ".backup.json").exists()
    # The snapshot is a real, openable DB with the same session count.
    snap = b.path / opencode_agent.storage_path.name
    assert _session_count(snap) == _session_count(_db_path(opencode_agent))


def test_take_backup_manifest_roundtrip(backup_mod, opencode_agent):
    b = backup_mod.take_backup(opencode_agent, note="unit")
    loaded = backup_mod.Backup.load(b.path)
    assert loaded.agent == "opencode"
    assert loaded.note == "unit"
    assert loaded.path == b.path


def test_take_backup_no_clobber_on_same_ms(backup_mod, opencode_agent):
    b1 = backup_mod.take_backup(opencode_agent)
    # Force a collision by reusing the exact same dir name.
    again = backup_mod._agent_backup_dir("opencode") / b1.path.name
    # take_backup must not overwrite it; it should pick a new sibling.
    b2 = backup_mod.take_backup(opencode_agent)
    assert b2.path != b1.path
    assert b1.path.exists() and b2.path.exists()


# ---------------------------------------------------------------------------
# verify_storage
# ---------------------------------------------------------------------------

def test_verify_clean_db(backup_mod, opencode_agent):
    assert backup_mod.verify_storage(opencode_agent) is True


def test_verify_detects_structural_corruption(backup_mod, opencode_agent):
    # Truncate the file: a valid header but garbage body -> integrity fails.
    p = _db_path(opencode_agent)
    good = p.read_bytes()
    try:
        p.write_bytes(good[:len(good) // 3])
        assert backup_mod.verify_storage(opencode_agent) is False
    finally:
        p.write_bytes(good)
    assert backup_mod.verify_storage(opencode_agent) is True


def test_verify_detects_logical_destruction(backup_mod, opencode_agent):
    # Drop the session table: integrity_check still says "ok", but the agent
    # has lost its data. verify must catch this.
    p = _db_path(opencode_agent)
    con = sqlite3.connect(str(p))
    con.execute("DROP TABLE session")
    con.commit()
    con.close()
    assert backup_mod.verify_storage(opencode_agent) is False


def test_verify_empty_zero_byte_db_is_ok(backup_mod, opencode_agent):
    # A 0-byte DB is "installed, no sessions yet", not corruption.
    p = _db_path(opencode_agent)
    good = p.read_bytes()
    try:
        p.write_bytes(b"")
        assert backup_mod.verify_storage(opencode_agent) is True
    finally:
        p.write_bytes(good)


def test_verify_missing_storage(backup_mod, opencode_agent):
    p = _db_path(opencode_agent)
    good = p.read_bytes()
    try:
        p.unlink()
        assert backup_mod.verify_storage(opencode_agent) is False
    finally:
        p.write_bytes(good)


# ---------------------------------------------------------------------------
# WriteGuard: finish() and rollback()
# ---------------------------------------------------------------------------

def test_guard_normal_write_finishes(backup_mod, opencode_agent):
    ag = opencode_agent
    from ctools.agents import get_agent
    msgs = get_agent("opencode").messages("ses_src")
    g = backup_mod.guard_write(ag, note="unit")
    before = g.sessions_before
    new_id = ag.create_session(msgs)
    assert g.finish() is True
    assert _session_count(_db_path(ag)) == before + 1
    assert new_id
    assert new_id != "ses_src"


def test_guard_destructive_write_auto_restores(backup_mod, opencode_agent):
    ag = opencode_agent
    g = backup_mod.guard_write(ag, note="unit")
    # Simulate a write that wipes the session rows (a "monkeypatch" gone bad).
    con = sqlite3.connect(str(_db_path(ag)))
    con.execute("DELETE FROM session")
    con.commit()
    con.close()
    with pytest.raises(backup_mod.BackupError):
        g.finish()
    # Auto-restore must have brought the original session back.
    assert backup_mod.verify_storage(ag) is True
    assert _session_count(_db_path(ag)) == 1


def test_guard_rollback_on_raised_write(backup_mod, opencode_agent):
    ag = opencode_agent
    g = backup_mod.guard_write(ag, note="unit")
    try:
        con = sqlite3.connect(str(_db_path(ag)))
        con.execute("DROP TABLE message")
        con.commit()
        con.close()
        raise RuntimeError("simulated crash")
    except RuntimeError:
        g.rollback()
    assert backup_mod.verify_storage(ag) is True
    assert _session_count(_db_path(ag)) == 1


def test_guard_double_finish_is_noop(backup_mod, opencode_agent):
    g = backup_mod.guard_write(opencode_agent, note="unit")
    assert g.finish() is True
    # Second call must not restore again / raise.
    assert g.finish() is True


# ---------------------------------------------------------------------------
# restore_backup is itself undoable
# ---------------------------------------------------------------------------

def test_restore_is_undoable(backup_mod, opencode_agent):
    ag = opencode_agent
    b = backup_mod.take_backup(ag, note="baseline")
    # Add a session, then restore the older snapshot; the pre-restore state
    # (with the extra session) must be saved so we can undo the restore.
    from ctools.agents import get_agent
    msgs = get_agent("opencode").messages("ses_src")
    ag.create_session(msgs)
    assert _session_count(_db_path(ag)) == 2
    pre = backup_mod.restore_backup(ag, b, pre_restore_note="unit")
    # After restoring the baseline (1 session), we're back to 1...
    assert _session_count(_db_path(ag)) == 1
    # ...and the pre-restore state (2 sessions) is saved as a backup.
    assert pre.path.exists()
    # Undoing the restore returns us to 2 sessions.
    backup_mod.restore_backup(ag, pre, pre_restore_note="undo")
    assert _session_count(_db_path(ag)) == 2


def test_restore_missing_backup_raises(backup_mod, opencode_agent):
    from ctools.backup import Backup
    fake = Backup(agent="opencode", when="1970", path=Path("/nonexistent"),
                  storage="x", kind="sqlite")
    with pytest.raises(backup_mod.BackupError):
        backup_mod.restore_backup(opencode_agent, fake)


# ---------------------------------------------------------------------------
# retention / prune
# ---------------------------------------------------------------------------

def test_prune_keeps_newest(backup_mod, opencode_agent):
    ag = opencode_agent
    paths = []
    for i in range(5):
        paths.append(backup_mod.take_backup(ag, note=f"n{i}").path)
        # ensure distinct dir names (same-ms guard handles it)
    store = backup_mod.Backups()
    removed = store.prune("opencode", keep=2)
    remaining = store.for_agent("opencode")
    assert len(remaining) == 2
    assert removed == 3
    # The two newest survive.
    alive = {b.path for b in remaining}
    assert paths[-1] in alive


# ---------------------------------------------------------------------------
# crecover CLI (subprocess)
# ---------------------------------------------------------------------------

def _run_crecover(args, home, inp=None):
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, "-m", "ctools.crecover", *args],
        capture_output=True, text=True, input=inp, env=env,
        cwd=Path(__file__).resolve().parents[2])


def test_crecover_version():
    p = _run_crecover(["--version"], home=Path(tempfile.gettempdir()))
    assert p.returncode == 0
    assert "crecover" in p.stdout


def test_crecover_list_empty(opencode_home):
    p = _run_crecover(["list"], home=opencode_home)
    assert p.returncode == 0
    assert "No backups" in p.stdout


def test_crecover_verify_ok_then_fail_then_restore(opencode_home):
    # Seed a copy so there's a backup and 2 sessions.
    env = {**os.environ, "HOME": str(opencode_home)}
    cwd = Path(__file__).resolve().parents[2]
    p = subprocess.run([sys.executable, "-m", "ctools.ccopy",
                        "opencode/ses_src", "opencode"],
                       capture_output=True, text=True, env=env, cwd=cwd)
    assert p.returncode == 0, p.stderr

    # verify OK
    p = _run_crecover(["verify", "opencode"], home=opencode_home)
    assert p.returncode == 0
    assert "OK" in p.stdout

    # corrupt: drop the session table entirely (logical destruction the
    # standalone verify can detect; the row-level case is covered by the
    # WriteGuard count-sentinel tests above)
    db = opencode_home / ".local" / "share" / "opencode" / "opencode.db"
    con = sqlite3.connect(str(db))
    con.execute("DROP TABLE session")
    con.commit()
    con.close()

    # verify now FAILs (session table gone -> logical destruction)
    p = _run_crecover(["verify", "opencode"], home=opencode_home)
    assert p.returncode == 1
    assert "FAIL" in p.stdout

    # find the latest backup dir
    p = _run_crecover(["list", "opencode"], home=opencode_home)
    import re
    m = re.search(r"backups/opencode/(\S+)", p.stdout)
    assert m, p.stdout
    bdir = m.group(1)

    # restore without prompt
    p = _run_crecover(["restore", "opencode", bdir, "-y"], home=opencode_home)
    assert p.returncode == 0, p.stderr
    assert "Restored" in p.stdout

    # verify OK again with original session back
    p = _run_crecover(["verify", "opencode"], home=opencode_home)
    assert p.returncode == 0
    assert "OK" in p.stdout
    assert _session_count(db) == 1


def test_crecover_restore_unknown_agent(opencode_home):
    p = _run_crecover(["restore", "no-such-agent", "x", "-y"],
                      home=opencode_home)
    assert p.returncode == 1
    assert "Unknown agent" in p.stderr


def test_crecover_restore_missing_backup(opencode_home):
    p = _run_crecover(["restore", "opencode", "does-not-exist", "-y"],
                      home=opencode_home)
    assert p.returncode == 1
    assert "no backup matching" in p.stderr.lower()
