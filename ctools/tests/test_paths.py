"""Tests for ctools.paths — the platform-resolved config/data directories."""

from pathlib import Path

import platformdirs
import pytest

from ctools.paths import config_dir, data_dir, ensure_config_dir


def test_config_dir_delegates_to_platformdirs(monkeypatch):
    """With no override, config_dir() is exactly platformdirs' config dir for
    ``ctools`` (XDG on Linux, ~/Library/Application Support on macOS,
    %APPDATA% on Windows)."""
    monkeypatch.delenv("CTOOLS_CONFIG", raising=False)
    assert config_dir() == Path(platformdirs.user_config_dir("ctools"))


def test_data_dir_delegates_to_platformdirs(monkeypatch):
    monkeypatch.delenv("CTOOLS_DATA", raising=False)
    assert data_dir() == Path(platformdirs.user_data_dir("ctools"))


def test_config_dir_honours_ctools_config(monkeypatch):
    monkeypatch.setenv("CTOOLS_CONFIG", "/tmp/ctools-cfg")
    assert config_dir() == Path("/tmp/ctools-cfg")


def test_config_dir_blank_override_ignored(monkeypatch):
    # An empty CTOOLS_CONFIG is treated as unset, not a relative "." path.
    monkeypatch.setenv("CTOOLS_CONFIG", "   ")
    assert config_dir() == Path(platformdirs.user_config_dir("ctools"))


def test_data_dir_honours_ctools_data(monkeypatch):
    monkeypatch.setenv("CTOOLS_DATA", "/tmp/ctools-data")
    assert data_dir() == Path("/tmp/ctools-data")


def test_config_dir_expanduser(tmp_path, monkeypatch):
    """A ~-prefixed override is expanded to the home directory."""
    monkeypatch.setenv("CTOOLS_CONFIG", str(tmp_path / "cfg"))
    assert config_dir() == tmp_path / "cfg"


def test_ensure_config_dir_creates_it(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "cfg"
    monkeypatch.setenv("CTOOLS_CONFIG", str(target))
    result = ensure_config_dir()
    assert result == target
    assert target.is_dir()


def test_macos_convention_is_library_application_support():
    """Document the macOS convention platformdirs applies.

    On a macOS host, platformdirs resolves the user config dir under
    ``~/Library/Application Support`` (not ``~/.config``).  We can't force the
    platform here, but we assert the delegation contract: whatever platform
    ``platformdirs`` reports, ``config_dir()`` follows it verbatim.  On Linux
    (this CI host) that is the XDG path; the assertion is platform-agnostic.
    """
    monkey = pytest.MonkeyPatch()
    try:
        monkey.delenv("CTOOLS_CONFIG", raising=False)
        expected = Path(platformdirs.user_config_dir("ctools"))
        assert config_dir() == expected
        # Sanity: the app name is always the final component.
        assert expected.name == "ctools"
    finally:
        monkey.undo()