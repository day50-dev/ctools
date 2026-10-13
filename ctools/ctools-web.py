#!/usr/bin/env python3
"""Launch the ctools web dashboard directly, without pip install.

Run it straight from the repo (the path is relative to this file, so it works
no matter where you invoke it from):

    python3 ctools/ctools-web.py                    # http://127.0.0.1:8765
    python3 ctools/ctools-web.py --port 9000        # a different port
    python3 ctools/ctools-web.py --host 0.0.0.0     # expose on the network
    python3 ctools/ctools-web.py --no-logo          # skip the animated banner

This is a thin launcher: it puts the package on sys.path and hands off to
``ctools.webui.main()``. It needs only the Python standard library plus the
rest of ctools (already in this repo). The installed ``ctools-web`` console
command does the same thing and can be used interchangeably.

Stop the server with Ctrl-C, or with the power button in the top-right of the
page.
"""
import sys
from pathlib import Path

# Put the repo root on sys.path so `import ctools` resolves whether or not the
# package is pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ctools.webui import main  # noqa: E402

if __name__ == "__main__":
    main()
