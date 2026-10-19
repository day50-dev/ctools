"""Tests for ctools.config — the general ctools.toml config (with [hosts])."""

import textwrap

import pytest

from ctools.config import Base, Config, config_file, load_config


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point CTOOLS_CONFIG at a temp dir and return it."""
    monkeypatch.setenv("CTOOLS_CONFIG", str(tmp_path))
    return tmp_path


def _write(config, toml_text):
    (config / "ctools.toml").write_text(textwrap.dedent(toml_text))


def test_config_file_lives_in_config_dir(config):
    assert config_file() == config / "ctools.toml"


def test_no_file_means_empty_config(config):
    c = load_config()
    assert c.hosts == []
    assert c.section("hosts") == {}


def test_malformed_toml_never_breaks(config):
    (config / "ctools.toml").write_text("this is [ not valid toml = = =")
    assert load_config().hosts == []


def test_reads_hosts_table(config):
    _write(config, """
        [hosts]
        fatty  = "ssh://bobby@fatty.local"
        workie = "ssh://10.0.0.4"
        bob    = "ssh://bob@localhost"
    """)
    got = load_config().hosts
    assert [b.name for b in got] == ["fatty", "workie", "bob"]

    assert got[0].target == "bobby@fatty.local"
    assert got[0].user == "bobby"
    assert got[0].host == "fatty.local"
    assert got[0].port is None

    assert got[1].target == "10.0.0.4"
    assert got[1].user is None

    assert got[2].target == "bob@localhost"
    assert got[2].host == "localhost"


def test_port_and_root_override(config):
    _write(config, """
        [hosts]
        box = "ssh://chris@box:2222/some/root"
    """)
    (b,) = load_config().hosts
    assert b.port == 2222
    assert b.root == "some/root"


def test_non_ssh_value_is_skipped(config):
    _write(config, """
        [hosts]
        good = "ssh://chris@box"
        bare = "just-a-name"
    """)
    assert [b.name for b in load_config().hosts] == ["good"]


def test_unparseable_ssh_value_is_skipped(config):
    # ssh:// with no host raises ValueError in parse_remote_ref -> skipped.
    _write(config, """
        [hosts]
        bad = "ssh://"
        good = "ssh://chris@box"
    """)
    assert [b.name for b in load_config().hosts] == ["good"]


def test_other_sections_are_accessible(config):
    # ctools.toml is a general config; [hosts] is one section, others slot in.
    _write(config, """
        [defaults]
        timeout = 30

        [hosts]
        box = "ssh://chris@box"
    """)
    c = load_config()
    assert [b.name for b in c.hosts] == ["box"]
    assert c.section("defaults") == {"timeout": 30}
    assert c.section("nope") == {}


def test_empty_hosts_table(config):
    _write(config, "[hosts]")
    assert load_config().hosts == []


def test_config_explicit_path(config):
    _write(config, "[hosts]\nbox = \"ssh://chris@box\"\n")
    c = Config(config / "ctools.toml")
    assert [b.name for b in c.hosts] == ["box"]


def test_ssh_command_uses_target():
    # A base's Remote builds the ssh argv for running ctools on the host,
    # leaving ~/.ssh/config resolution to ssh (no -F/-o overrides).
    from ctools.cli import Remote
    b = Base(name="x", remote=Remote(host="box", user="chris"))
    assert b.remote.ssh_command("ctools cdir pi .") == [
        "ssh", "-T", "chris@box", "ctools cdir pi .",
    ]