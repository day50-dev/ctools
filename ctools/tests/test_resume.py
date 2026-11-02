"""ctools resume: the exec hand-off, and cdir --resume-command."""
import sys

import pytest

from ctools import resume as resume_mod
from ctools import dispatch


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["ctools", *argv])
    with pytest.raises(SystemExit) as exc:
        dispatch.main()
    return exc.value.code


class _FakeAgent:
    name = "pi"

    def __init__(self, installed=True, session_ids=(), cwd=None):
        self._installed = installed
        self._ids = set(session_ids)
        self._cwd = cwd

    def exists(self):
        return self._installed

    def session(self, session_id):
        return object() if session_id in self._ids else None

    def session_cwd(self, session_id):
        return self._cwd


def _patch(monkeypatch, agent, command):
    monkeypatch.setattr(resume_mod, "get_agent", lambda name: agent if agent is not None else None)
    monkeypatch.setattr(resume_mod, "get_resume_command", lambda a, s: command)


def test_resolve_happy_path(monkeypatch):
    _patch(monkeypatch, _FakeAgent(session_ids={"abc"}, cwd="/tmp/x"), "pi --session abc")
    cmd, cwd, err = resume_mod._resolve("pi", "abc")
    assert err is None
    assert cmd == "pi --session abc"
    assert cwd == "/tmp/x"


def test_resolve_unknown_agent(monkeypatch):
    _patch(monkeypatch, None, None)
    cmd, cwd, err = resume_mod._resolve("bogus", "abc")
    assert cmd is None and "unknown agent" in err


def test_resolve_not_installed(monkeypatch):
    _patch(monkeypatch, _FakeAgent(installed=False), "pi --session abc")
    cmd, cwd, err = resume_mod._resolve("pi", "abc")
    assert cmd is None and "not installed" in err


def test_resolve_no_such_session(monkeypatch):
    _patch(monkeypatch, _FakeAgent(session_ids={"other"}), "pi --session abc")
    cmd, cwd, err = resume_mod._resolve("pi", "abc")
    assert cmd is None and "no such session" in err


def test_resolve_no_command(monkeypatch):
    _patch(monkeypatch, _FakeAgent(session_ids={"abc"}), None)
    cmd, cwd, err = resume_mod._resolve("pi", "abc")
    assert cmd is None and "no resume command" in err


def test_resume_routes_to_its_module(monkeypatch):
    seen = {}

    def fake():
        seen["argv"] = list(sys.argv)
        return 7

    monkeypatch.setattr("ctools.resume.app", fake)
    assert _run(monkeypatch, "resume", "pi", "abc") == 7
    assert seen["argv"] == ["ctools resume", "pi", "abc"]


def test_resume_is_a_handoff_verb():
    grouped = {v: t for t, verbs in dispatch.GROUPS for v in verbs}
    assert grouped["resume"].startswith("long-running")
