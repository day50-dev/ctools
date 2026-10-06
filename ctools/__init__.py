"""ctools - CLI tools for LLM context windows.

Provides cdir (ls for context windows) and cgrep (grep for context windows).
"""

import re
from pathlib import Path

__all__ = ["__version__"]


def _detect_version() -> str:
    """Return the package version.

    pyproject.toml is the single source of truth. Prefer it when running from
    a source checkout so a version bump is visible without reinstalling;
    otherwise fall back to installed metadata (wheel installs ship no
    pyproject.toml next to the package).
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if re.search(r'(?m)^name\s*=\s*"ctxttools"', text):
            match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
            if match:
                return match.group(1)
    try:
        from importlib.metadata import version
        return version("ctxttools")
    except Exception:
        return "unknown"


__version__ = _detect_version()
