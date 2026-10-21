"""The ``ctools`` front door: routing, grouped help, and the prog rename."""
import argparse
import sys

import pytest

from ctools import dispatch


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["ctools", *argv])
    with pytest.raises(SystemExit) as exc:
        dispatch.main()
    return exc.value.code


def test_no_args_prints_grouped_help(monkeypatch, capsys):
    assert _run(monkeypatch) == 0
    out = capsys.readouterr().out
    for title in ("query:", "transform:", "long-running:"):
        assert title in out
    for verb in dispatch.VERBS:
        assert f"  {verb} " in out


def test_every_grouped_verb_is_registered_once():
    grouped = [v for _, verbs in dispatch.GROUPS for v in verbs]
    assert sorted(grouped) == sorted(dispatch.VERBS)


def test_entries_match_the_standalone_console_scripts():
    import re
    from pathlib import Path
    toml = (Path(dispatch.__file__).parent.parent / "pyproject.toml").read_text()
    scripts = dict(re.findall(r'^([\w-]+) = "([\w.:]+)"$', toml, re.M))
    for verb, (entry, legacy, _) in dispatch.VERBS.items():
        assert scripts[legacy] == entry, verb
    assert scripts["ctools"] == "ctools.dispatch:main"


def test_unknown_verb_exits_2(monkeypatch, capsys):
    assert _run(monkeypatch, "bogus") == 2
    assert "unknown command 'bogus'" in capsys.readouterr().err


def test_version(monkeypatch, capsys):
    from ctools import __version__
    assert _run(monkeypatch, "--version") == 0
    assert capsys.readouterr().out.strip() == f"ctools (ctxttools) {__version__}"


def test_verb_help_names_the_dispatched_form(monkeypatch, capsys):
    assert _run(monkeypatch, "dir", "--help") == 0
    assert capsys.readouterr().out.startswith("usage: ctools dir ")


def test_verb_errors_name_the_dispatched_form(monkeypatch, capsys):
    assert _run(monkeypatch, "grep", "--no-such-flag") == 2
    assert "ctools grep: error:" in capsys.readouterr().err


def test_subparser_prog_follows(monkeypatch, capsys):
    assert _run(monkeypatch, "mcp", "install", "--help") == 0
    assert capsys.readouterr().out.startswith("usage: ctools mcp install")


def test_verb_sees_only_its_own_args(monkeypatch):
    seen = {}

    def fake():
        seen["argv"] = list(sys.argv)
        return 3

    monkeypatch.setattr("ctools.cdir.app", fake)
    assert _run(monkeypatch, "dir", "-l", "pi/x") == 3
    assert seen["argv"] == ["ctools dir", "-l", "pi/x"]


def test_prog_patch_is_restored(monkeypatch):
    init = argparse.ArgumentParser.__init__
    _run(monkeypatch, "dir", "--help")
    assert argparse.ArgumentParser.__init__ is init
    assert argparse.ArgumentParser(prog="cdir").prog == "cdir"
