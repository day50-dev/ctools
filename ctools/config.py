"""The ctools config — ``ctools.toml`` in the ctools config directory.

One file, multiple sections.  The ``[hosts]`` table is the search whitelist
(issue #2): hosts to look under for agent session dirs, in addition to the
local current user.  Each value is an ``ssh://[user@]host[:port]`` reference —
the same form ``ccat`` and ``ccopy`` use for a remote side — so the host may
be a ``~/.ssh/config`` alias: we run ``ssh -T <target> <cmd>`` with no ``-F``
/ ``-o`` overrides, so ssh resolves it with whatever rules your config has
(``ProxyCommand``, ``IdentityFile``, ``User``, ...).  We never "explore" or
"discover"; we only ever look at the hosts named here (no NFS/sshfs mounts,
no workgroup broadcasts).

    # ~/.config/ctools/ctools.toml
    # (macOS: ~/Library/Application Support/ctools/ctools.toml)

    [hosts]
    fatty  = "ssh://bobby@fatty.local"
    workie = "ssh://10.0.0.4"
    bob    = "ssh://bob@localhost"

Other sections can slot into the same file.  A missing or malformed file
yields an empty config — it never breaks a search, it just means "local only".

This module only reads the config.  Actually searching across bases — running
ctools *natively on the remote host* rather than copying files over, with a
``ctools <version>`` first line (then the error) on a bad/old invocation — is
a separate, larger feature.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    import tomllib as _toml  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml  # type: ignore

from ctools.cli import Remote, parse_remote_ref
from ctools.paths import config_dir


@dataclass
class Base:
    """One host (an account on a machine) to look under for agent session dirs."""

    name: str
    remote: Remote

    @property
    def target(self) -> str:
        """``user@host`` (or just ``host``) — the ssh target."""
        return self.remote.target

    @property
    def host(self) -> str:
        return self.remote.host

    @property
    def user(self) -> Optional[str]:
        return self.remote.user

    @property
    def port(self) -> Optional[int]:
        return self.remote.port

    @property
    def root(self) -> str:
        """The root to look under: the URI path if given, else ``""`` (that
        account's home)."""
        return self.remote.path


def config_file() -> Path:
    """The ctools config file (``ctools.toml`` in the ctools config dir)."""
    return config_dir() / "ctools.toml"


class Config:
    """The ctools config (``ctools.toml``), loaded once and section-accessed.

    A missing or malformed file yields an empty config (it never raises).
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else config_file()
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            with self.path.open("rb") as f:
                data = _toml.load(f)
        except (OSError, _toml.TOMLDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def hosts(self) -> List[Base]:
        """The ``[hosts]`` table as a list of :class:`Base`."""
        raw = self._data.get("hosts", {})
        if not isinstance(raw, dict):
            return []
        out: List[Base] = []
        for name, uri in raw.items():
            uri = str(uri).strip()
            if not uri.startswith("ssh://"):
                continue
            try:
                remote, _ = parse_remote_ref(uri)
            except ValueError:
                continue
            if remote is None:
                continue
            out.append(Base(name=str(name), remote=remote))
        return out

    def section(self, name: str) -> dict:
        """A raw config section as a dict (empty if absent or not a table)."""
        value = self._data.get(name, {})
        return value if isinstance(value, dict) else {}


def load_config() -> Config:
    """A fresh :class:`Config` for the default ``ctools.toml``."""
    return Config()