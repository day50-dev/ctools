"""Tests for ctools.remote_search — running a search on a remote host."""

import subprocess

import pytest

from ctools.cli import Remote
from ctools.config import Base
from ctools.remote_search import (
    build_remote_command,
    parse_version_prefix,
    run_remote_search,
)


def _base(name="fatty", target="bobby@fatty.local"):
    user, _, host = target.partition("@")
    return Base(name=name, remote=Remote(host=host, user=user or None))


# --- build_remote_command ---

def test_build_forwards_args_and_strips_flag():
    cmd = build_remote_command("cgrep", ["-i", "pattern", "--hosts"],
                              strip=["--hosts"])
    # --hosts is dropped; the rest is forwarded, shell-quoted. The command is
    # the bare entry point (there is no `ctools` main command), prefixed with
    # a PATH extension so the non-interactive ssh session finds it.
    assert "cgrep " in cmd
    assert "--hosts" not in cmd
    assert "pattern" in cmd
    assert "-i" in cmd
    assert cmd.startswith("export PATH=")


def test_build_quotes_args_with_spaces():
    cmd = build_remote_command("cgrep", ["hello world"], strip=["--hosts"])
    # The space-containing arg is quoted so the remote shell sees one token.
    assert "'hello world'" in cmd


def test_build_no_strip():
    cmd = build_remote_command("cgrep", ["-i", "x"])
    assert cmd.endswith("cgrep -i x")
    assert cmd.startswith("export PATH=")


# --- parse_version_prefix (the version-negotiation form) ---

def test_version_prefix_detected():
    version, error = parse_version_prefix(
        ["ctools 0.2.0", "unrecognized arguments: --hosts"])
    assert version == "0.2.0"
    assert error == "unrecognized arguments: --hosts"


def test_version_prefix_falls_back_to_stderr():
    version, error = parse_version_prefix(["ctools 0.2.0"], "boom on stderr")
    assert version == "0.2.0"
    assert error == "boom on stderr"


def test_no_version_prefix_is_ordinary_output():
    version, error = parse_version_prefix(
        ["pi/01a1:42:let me check", "pi/01a1:43:next"])
    assert version is None
    assert error is None


def test_empty_lines_no_version():
    assert parse_version_prefix([]) == (None, None)


def test_ctools_word_alone_is_not_a_version():
    # A line that merely starts with "ctools " but has no version token.
    version, _ = parse_version_prefix(["ctools"])
    assert version is None


# --- run_remote_search (ssh mocked) ---

def test_ssh_not_found(monkeypatch):
    monkeypatch.setattr("ctools.remote_search.shutil.which", lambda _: None)
    res = run_remote_search(_base(), "ctools cgrep x")
    assert res.ok is False
    assert "ssh not found" in res.error


def test_timeout_yields_error(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=1)
    monkeypatch.setattr("ctools.remote_search.shutil.which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr("ctools.remote_search.subprocess.run", _raise)
    res = run_remote_search(_base(), "ctools cgrep x", timeout=5)
    assert res.ok is False
    assert "timed out" in res.error


def test_version_negotiation_result(monkeypatch):
    # A stale host emits "ctools <version>" + a gripe -> detected, not matches.
    class _P:
        returncode = 2
        stdout = "ctools 0.2.0\nunrecognized arguments: --hosts\n"
        stderr = ""
    monkeypatch.setattr("ctools.remote_search.shutil.which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr("ctools.remote_search.subprocess.run",
                       lambda *a, **k: _P())
    res = run_remote_search(_base(), "ctools cgrep x")
    assert res.ok is False
    assert res.version == "0.2.0"
    assert res.lines == []
    assert "unrecognized arguments" in res.error


def test_success_tags_lines_with_host(monkeypatch):
    class _P:
        returncode = 0
        stdout = "pi/01a1:42:let me check\npi/01a1:43:next\n"
        stderr = ""
    monkeypatch.setattr("ctools.remote_search.shutil.which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr("ctools.remote_search.subprocess.run",
                       lambda *a, **k: _P())
    res = run_remote_search(_base(), "ctools cgrep x")
    assert res.ok is True
    assert res.version is None
    assert res.lines == ["@fatty:pi/01a1:42:let me check",
                        "@fatty:pi/01a1:43:next"]


def test_failure_reports_stderr(monkeypatch):
    class _P:
        returncode = 1
        stdout = ""
        stderr = "ctools: command not found"
    monkeypatch.setattr("ctools.remote_search.shutil.which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr("ctools.remote_search.subprocess.run",
                       lambda *a, **k: _P())
    res = run_remote_search(_base(), "ctools cgrep x")
    assert res.ok is False
    assert res.error == "ctools: command not found"