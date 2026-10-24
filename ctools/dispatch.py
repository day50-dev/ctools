"""ctools — one front door for the ctools commands.

``ctools <verb> [args...]`` runs the same code as the matching standalone
command (``ctools dir`` is ``cdir``, ``ctools web`` is ``ctools-web``, ...).
The standalone commands stay as their own console scripts; this module only
routes. All behaviour lives in the verb modules.
"""

import argparse
import importlib
import sys
from contextlib import contextmanager

from ctools import __version__

# verb -> (entry point, the standalone command's prog, one-line summary)
VERBS = {
    "dir":     ("ctools.cdir:app",        "cdir",       "list agents and sessions"),
    "grep":    ("ctools.cgrep:app",       "cgrep",      "search session content"),
    "cat":     ("ctools.ccat:app",        "ccat",       "print a session"),
    "du":      ("ctools.cdu:app",         "cdu",        "context usage per session"),
    "copy":    ("ctools.ccopy:app",       "ccopy",      "copy a session to another agent"),
    "extract": ("ctools.cextract:app",    "cextract",   "extract concepts from a session"),
    "connect": ("ctools.cconnect:app",    "cconnect",   "pipe concepts between sessions"),
    "rm":      ("ctools.crm:app",         "crm",        "cut sections out of a session"),
    "recover": ("ctools.crecover:app",    "crecover",   "restore from a backup"),
    "web":     ("ctools.webui:main",      "ctools-web", "run the web dashboard"),
    "mcp":     ("ctools.ctools_mcp:main", "ctools-mcp", "run the MCP server"),
}

GROUPS = (
    ("query", ("dir", "grep", "cat", "du")),
    ("transform", ("copy", "extract", "connect", "rm", "recover")),
    ("long-running", ("web", "mcp")),
)


def usage() -> str:
    lines = ["usage: ctools <command> [args...]", ""]
    for title, verbs in GROUPS:
        lines.append(f"{title}:")
        for verb in verbs:
            lines.append(f"  {verb:<9} {VERBS[verb][2]:<34} ({VERBS[verb][1]})")
        lines.append("")
    lines.append("Run 'ctools <command> --help' for a command's options.")
    return "\n".join(lines)


@contextmanager
def _prog_as(legacy: str, prog: str):
    """While a verb runs, parsers it builds with ``prog=legacy`` say ``prog``.

    The verb modules hardcode their standalone name (``prog="cdir"``); under
    ``ctools dir`` the help and error lines should name what the user typed.
    Sub-parsers named ``"<legacy> <sub>"`` (``ctools-mcp install``) follow too.
    """
    init = argparse.ArgumentParser.__init__

    def patched(self, *args, **kwargs):
        name = kwargs.get("prog") or ""
        if name == legacy or name.startswith(legacy + " "):
            kwargs["prog"] = prog + name[len(legacy):]
        init(self, *args, **kwargs)

    argparse.ArgumentParser.__init__ = patched
    try:
        yield
    finally:
        argparse.ArgumentParser.__init__ = init


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        sys.exit(0)
    if argv[0] == "--version":
        print(f"ctools (ctxttools) {__version__}")
        sys.exit(0)
    verb, rest = argv[0], argv[1:]
    if verb not in VERBS:
        print(f"ctools: unknown command '{verb}'\n\n{usage()}", file=sys.stderr)
        sys.exit(2)
    entry, legacy, _ = VERBS[verb]
    module_name, attr = entry.split(":")
    fn = getattr(importlib.import_module(module_name), attr)
    prog = f"ctools {verb}"
    # The verb reads sys.argv exactly as its own console script would.
    sys.argv = [prog] + rest
    with _prog_as(legacy, prog):
        sys.exit(fn())


if __name__ == "__main__":
    main()
