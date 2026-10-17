"""Tests for crm (context removal)."""
import json

import pytest

import ctools.crm as crm
from ctools.crm import app
from ctools.agents import REGISTRY as AGENTS
from ctools.testing import Runner

runner = Runner()


def _make_session(tmp_path, session_id="aaaa-bbbb", n_msgs=4):
    """A claude-code session with n_msgs numbered user/assistant messages
    (claude-code is a JsonlAgent, which supports message removal)."""
    project = tmp_path / 'projects' / 'proj'
    project.mkdir(parents=True)
    entries = []
    for i in range(n_msgs):
        role = "user" if i % 2 == 0 else "assistant"
        entries.append({"type": role,
                        "message": {"role": role,
                                    "content": f"message number {i}"}})
    (project / f'{session_id}.jsonl').write_text(
        '\n'.join(json.dumps(e) for e in entries) + '\n')


def _with_cc(tmp_path, func):
    original = AGENTS['claude-code'].base_path
    AGENTS['claude-code'].base_path = tmp_path
    try:
        return func()
    finally:
        AGENTS['claude-code'].base_path = original


# --- index spec parsing ---

def test_parse_index_spec_ranges():
    assert crm._parse_index_spec("3 7 12-15", 20) == {3, 7, 12, 13, 14, 15}
    assert crm._parse_index_spec("0", 1) == {0}
    assert crm._parse_index_spec("5-5", 10) == {5}
    assert crm._parse_index_spec("", 10) == set()


def test_parse_index_spec_errors():
    with pytest.raises(ValueError):
        crm._parse_index_spec("abc", 10)
    with pytest.raises(ValueError):
        crm._parse_index_spec("10", 10)    # out of range (valid: 0-9)
    with pytest.raises(ValueError):
        crm._parse_index_spec("5-2", 10)    # reversed range
    with pytest.raises(ValueError):
        crm._parse_index_spec("1-", 10)     # dangling range


# --- interactive (no concept files) ---

def test_crm_interactive_remove(tmp_path):
    """crm <agent>/<session> with no concepts: list, pick, confirm, remove."""
    _make_session(tmp_path)

    def run():
        result = runner.invoke(app, ["claude-code/aaaa-bbbb"],
                               input="1 2-3\ny\n")
        assert result.exit_code == 0, result.output
        assert "message number 1" in result.stdout
        assert "Removed 3 message(s)" in result.stdout
        remaining = AGENTS['claude-code'].raw_messages("aaaa-bbbb")
        assert len(remaining) == 1
        assert remaining[0].content == "message number 0"

    _with_cc(tmp_path, run)


def test_crm_interactive_abort_empty(tmp_path):
    """An empty answer aborts and removes nothing."""
    _make_session(tmp_path)

    def run():
        result = runner.invoke(app, ["claude-code/aaaa-bbbb"], input="\n")
        assert result.exit_code == 0
        assert "Aborted" in result.stdout
        assert len(AGENTS['claude-code'].raw_messages("aaaa-bbbb")) == 4

    _with_cc(tmp_path, run)


def test_crm_interactive_bad_index(tmp_path):
    """A bad index spec is an error and removes nothing."""
    _make_session(tmp_path)

    def run():
        result = runner.invoke(app, ["claude-code/aaaa-bbbb"], input="99\n")
        assert result.exit_code == 1
        assert "Bad index spec" in result.stdout
        assert len(AGENTS['claude-code'].raw_messages("aaaa-bbbb")) == 4

    _with_cc(tmp_path, run)


def test_crm_concept_mode_still_works(tmp_path, monkeypatch):
    """Concept files still take the concept path, not the picker."""
    _make_session(tmp_path)

    def run():
        concept = tmp_path / "concept.json"
        concept.write_text(json.dumps([
            {"name": "junk", "short": "message number 2"}]))
        result = runner.invoke(app, ["claude-code/aaaa-bbbb", str(concept)])
        assert result.exit_code == 0, result.output
        assert "Removed 1 message(s)" in result.stdout
        assert len(AGENTS['claude-code'].raw_messages("aaaa-bbbb")) == 3

    _with_cc(tmp_path, run)
# --- --backup ---

def test_crm_backup_copies_session_file(tmp_path, monkeypatch):
    """--backup copies the session file to $CTOOLS_TMP/ctools/<agent>/<uuid>."""
    monkeypatch.setenv("CTOOLS_TMP", str(tmp_path / "tmp"))
    _make_session(tmp_path)

    def run():
        result = runner.invoke(app, ["claude-code/aaaa-bbbb", "--backup"],
                               input="0\ny\n")
        assert result.exit_code == 0, result.output
        dest = tmp_path / "tmp" / "ctools" / "claude-code" / "aaaa-bbbb"
        assert dest.exists()
        # the backup kept every message
        backup_msgs = [json.loads(l) for l in dest.read_text().splitlines() if l]
        assert len(backup_msgs) == 4
        # the live session lost message 0
        assert len(AGENTS['claude-code'].raw_messages("aaaa-bbbb")) == 3
        assert "Backed up session to" in result.stdout

    _with_cc(tmp_path, run)


def test_crm_backup_never_overwrites(tmp_path, monkeypatch):
    """A second --backup run gets a suffixed name; the first is untouched."""
    monkeypatch.setenv("CTOOLS_TMP", str(tmp_path / "tmp"))
    _make_session(tmp_path)

    def run():
        r1 = runner.invoke(app, ["claude-code/aaaa-bbbb", "--backup"],
                           input="0\ny\n")
        assert r1.exit_code == 0, r1.output
        r2 = runner.invoke(app, ["claude-code/aaaa-bbbb", "--backup"],
                           input="0\ny\n")
        assert r2.exit_code == 0, r2.output
        first = tmp_path / "tmp" / "ctools" / "claude-code" / "aaaa-bbbb"
        second = tmp_path / "tmp" / "ctools" / "claude-code" / "aaaa-bbbb-1"
        assert first.exists() and second.exists()
        # the first backup still holds the pre-first-removal state
        msgs = [json.loads(l) for l in first.read_text().splitlines() if l]
        assert len(msgs) == 4

    _with_cc(tmp_path, run)


def test_crm_backup_sqlite_agent(tmp_path, monkeypatch):
    """sqlite agents (opencode) back up via VACUUM INTO: a real DB copy."""
    import sqlite3
    from ctools.tests.test_cgrep import create_test_opencode_db
    monkeypatch.setenv("CTOOLS_TMP", str(tmp_path / "tmp"))
    db = create_test_opencode_db(tmp_path)
    assert db == tmp_path / "opencode.db"

    def run():
        from ctools.crm import _backup_session
        dest = _backup_session(AGENTS['opencode'], "ses_test123")
        assert dest == tmp_path / "tmp" / "ctools" / "opencode" / "ses_test123"
        conn = sqlite3.connect(str(dest))
        try:
            n = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        finally:
            conn.close()
        assert n == 4

    original = AGENTS['opencode'].base_path
    AGENTS['opencode'].base_path = tmp_path
    try:
        run()
    finally:
        AGENTS['opencode'].base_path = original
