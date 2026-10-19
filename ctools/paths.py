"""Platform-resolved ctools directories.

Where ctools keeps its per-user config and data, resolved via
``platformdirs`` (the standard Python library for exactly this) rather than
hand-rolling ``sys.platform`` checks.  Per platform:

- Linux:   ``$XDG_CONFIG_HOME/ctools`` (default ``~/.config/ctools``) for config,
           ``$XDG_DATA_HOME/ctools`` (default ``~/.local/share/ctools``) for data.
- macOS:   ``~/Library/Application Support/ctools`` for both (the macOS convention).
- Windows: ``%APPDATA%\\ctools`` for config, ``%LOCALAPPDATA%\\ctools`` for data.

``$CTOOLS_CONFIG`` and ``$CTOOLS_DATA`` relocate the directories for testing
or power users, mirroring how ``$CTOOLS_TMP`` relocates the tmp base.
"""

import os
from pathlib import Path

import platformdirs

_APP = "ctools"


def config_dir() -> Path:
    """The ctools config directory (per-platform; see module docstring).

    Honours ``$CTOOLS_CONFIG`` when set.  Does not create the directory;
    callers that write into it create it as needed.
    """
    override = os.environ.get("CTOOLS_CONFIG", "").strip()
    if override:
        return Path(override)
    return Path(platformdirs.user_config_dir(_APP))


def data_dir() -> Path:
    """The ctools data directory (per-platform; see module docstring).

    Honours ``$CTOOLS_DATA`` when set.
    """
    override = os.environ.get("CTOOLS_DATA", "").strip()
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir(_APP))


def ensure_config_dir() -> Path:
    """``config_dir()`` with the directory created (parents, no error)."""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d