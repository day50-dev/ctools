"""ctools resume: the exec hand-off, and cdir --resume-command."""
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

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


# --- Accepted reference shapes (what cdir prints, pasted back) ---

def test_parse_ref_slash_form(monkeypatch):
    """The `cdir` row shape: agent/session in one token, with or without @."""
    for ref in ("pi/abc", "@pi/abc", "claude-code/55b9c8b7"):
        name, sid, err = resume_mod.parse_ref_args([ref])
        assert err is None, ref
        assert name in ("pi", "claude-code"), ref
        assert sid in ("abc", "55b9c8b7"), ref


def test_parse_ref_two_words(monkeypatch):
    name, sid, err = resume_mod.parse_ref_args(["pi", "abc"])
    assert (name, sid, err) == ("pi", "abc", None)


def test_parse_ref_slash_unknown_agent(monkeypatch):
    name, sid, err = resume_mod.parse_ref_args(["bogus/abc"])
    assert (name, sid) == (None, None)
    assert "unknown agent: bogus" in err


def test_parse_ref_slash_no_id(monkeypatch):
    name, sid, err = resume_mod.parse_ref_args(["pi/"])
    assert err is not None and "no session id" in err


def test_parse_ref_bad_arg_count(monkeypatch):
    for args in ([], ["a", "b", "c"]):
        name, sid, err = resume_mod.parse_ref_args(args)
        assert (name, sid) == (None, None), args
        assert err is not None, args


def test_bare_id_resolves_unique_agent(monkeypatch):
    """A bare id (the `cdir -1` shape) is looked up across the installed agents."""
    agent = _FakeAgent(session_ids={"abc"}, cwd="/tmp/x")
    monkeypatch.setattr(resume_mod, "AGENTS", {"pi": agent})
    name, sid, err = resume_mod.parse_ref_args(["abc"])
    assert (name, sid, err) == ("pi", "abc", None)


def test_bare_id_not_found(monkeypatch):
    monkeypatch.setattr(resume_mod, "AGENTS",
                        {"pi": _FakeAgent(session_ids={"other"})})
    name, sid, err = resume_mod.parse_ref_args(["abc"])
    assert (name, sid) == (None, None)
    assert "no such session: abc" in err


def test_bare_id_ambiguous_across_agents(monkeypatch):
    """An id two agents hold is reported, not guessed at."""
    monkeypatch.setattr(resume_mod, "AGENTS",
                        {"pi": _FakeAgent(session_ids={"abc"}),
                         "opencode": _FakeAgent(session_ids={"abc"})})
    name, sid, err = resume_mod.parse_ref_args(["abc"])
    assert (name, sid) == (None, None)
    assert "several agents" in err and "pi/abc" in err


def test_bare_id_skips_missing_agents(monkeypatch):
    """Uninstalled agents don't count toward the hit list."""
    monkeypatch.setattr(resume_mod, "AGENTS",
                        {"pi": _FakeAgent(session_ids={"abc"}),
                         "goose": _FakeAgent(installed=False, session_ids={"abc"})})
    name, sid, err = resume_mod.parse_ref_args(["abc"])
    assert (name, sid, err) == ("pi", "abc", None)


def test_resolve_accepts_bare_id(monkeypatch):
    """End-to-end: the parsed shapes feed _resolve unchanged."""
    agent = _FakeAgent(session_ids={"abc"}, cwd="/tmp/x")
    monkeypatch.setattr(resume_mod, "get_agent", lambda name: agent)
    monkeypatch.setattr(resume_mod, "get_resume_command", lambda a, s: "pi --session abc")
    cmd, cwd, err = resume_mod._resolve("pi", "abc")
    assert (cmd, cwd, err) == ("pi --session abc", "/tmp/x", None)


def test_resume_app_reports_bad_shape(monkeypatch):
    '''`ctools resume a b c` is an argument error (exit 1), not an exec attempt.'''
    monkeypatch.setattr(resume_mod, "parse_ref_args",
                        lambda args: (None, None, "expected AGENT/SESSION (or AGENT SESSION)"))
    monkeypatch.setattr(sys, "argv", ["ctools", "resume", "a", "b", "c"])
    assert resume_mod.app() == 1


def test_resume_is_a_handoff_verb():
    grouped = {v: t for t, verbs in dispatch.GROUPS for v in verbs}
    assert grouped["resume"].startswith("long-running")


# --- `resume <path>`: the most recent session recorded in a directory ---

class _DirAgent:
    """An installed agent whose sessions() carry a recorded path + mtime."""
    name = "pi"

    def __init__(self, sessions):
        self._sessions = sessions

    def exists(self):
        return True

    def sessions(self):
        return self._sessions


PROJ = "/tmp/ctools-resume-proj"


def _sess(id, path=PROJ, when="2025"):
    y, m, d = (2025, 1, 1) if when == "2025" else (2024, 1, 1)
    return SimpleNamespace(id=id, path=path, mtime=datetime(y, m, d, tzinfo=timezone.utc))


def _dir_agents(monkeypatch, agents):
    monkeypatch.setattr(resume_mod, "AGENTS", agents)
    monkeypatch.setattr(resume_mod, "get_agent", lambda n: agents.get(n))


def test_resolve_dir_picks_most_recent(monkeypatch):
    """Across all installed agents, the newest session in the dir wins."""
    agents = {"pi": _DirAgent([_sess("old", when="2024"), _sess("new", when="2025")])}
    _dir_agents(monkeypatch, agents)
    assert resume_mod._resolve_dir(PROJ) == ("pi", "new", None)


def test_resolve_dir_scopes_to_agent(monkeypatch):
    """`agent <path>` ignores newer sessions held by other agents."""
    oc_newer = _sess("oc", when="2025")
    agents = {"pi": _DirAgent([_sess("pi", when="2024")]),
              "opencode": _DirAgent([oc_newer])}
    _dir_agents(monkeypatch, agents)
    assert resume_mod._resolve_dir(PROJ, "opencode") == ("opencode", "oc", None)
    # ... and scoping to pi keeps the older pi session over the newer opencode one.
    assert resume_mod._resolve_dir(PROJ, "pi") == ("pi", "pi", None)


def test_resolve_dir_no_match(monkeypatch):
    _dir_agents(monkeypatch, {"pi": _DirAgent([])})
    name, sid, err = resume_mod._resolve_dir("/tmp/ctools-resume-empty")
    assert sid is None and "no session recorded" in err


def test_resolve_dir_unknown_agent(monkeypatch):
    _dir_agents(monkeypatch, {})
    name, sid, err = resume_mod._resolve_dir(PROJ, "bogus")
    assert (name, sid) == (None, None) and "unknown agent" in err


def test_parse_ref_path_single(monkeypatch):
    """A single anchored path token resolves to the newest session in it."""
    _dir_agents(monkeypatch, {"pi": _DirAgent([_sess("new")])})
    assert resume_mod.parse_ref_args([PROJ]) == ("pi", "new", None)


def test_parse_ref_path_two_words(monkeypatch):
    """`agent <dir>`: the newest session of that agent inside the dir."""
    import os
    cwd = os.getcwd()
    _dir_agents(monkeypatch, {"pi": _DirAgent([_sess("pi", path=cwd)])})
    assert resume_mod.parse_ref_args(["pi", "."]) == ("pi", "pi", None)


def test_looks_like_dir():
    f = resume_mod._looks_like_dir
    assert f(".") and f("..") and f("./x") and f("/abs") and f("~/x")
    assert not f("abc")          # a bare session id
    assert not f("bogus/abc")    # an unknown-agent/id pair, not an existing dir


def test_dir_arg_detection():
    assert resume_mod._dir_arg(["."]) == "."
    assert resume_mod._dir_arg(["/x"]) == "/x"
    assert resume_mod._dir_arg(["pi", "."]) == "."
    assert resume_mod._dir_arg(["pi", "abc"]) is None
    assert resume_mod._dir_arg(["abc"]) is None
