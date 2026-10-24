"""Run a ctools search on a remote host and collect the results.

The "dumb" strategy — ssh in, copy the storage over, search locally, throw it
away — is rejected.  Instead the remote host runs ctools *natively* and only
the results cross the wire.  A base is a host (see :mod:`ctools.config`); we
run the same search command there and prefix each result line with the host,
so a hit reads ``@fatty:pi/01a1…:42:let me check``.

**Version negotiation.**  On a bad/old invocation the remote ctools emits
``ctools <version>`` on the first line followed by the error, so a stale host
is detectable: if the first line of the remote output is ``ctools <ver>``, we
report that version instead of treating the output as matches.
"""

import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

from ctools.cli import REMOTE_PATH
from ctools.config import Base


@dataclass
class RemoteResult:
    """The outcome of running a search on one host."""

    host: str  # the base name
    ok: bool  # True if the search ran (exit 0, not a version-negotiation error)
    version: Optional[str] = None  # the remote ctools version, if detected
    lines: List[str] = field(default_factory=list)  # result lines, host-prefixed
    error: Optional[str] = None  # the remote's error, if any


def build_remote_command(program: str, local_argv: List[str],
                        strip: Optional[List[str]] = None) -> str:
    """Rebuild the command to run on the remote host.

    Forwards ``local_argv`` (the original program args) to ``<program>``,
    dropping any flag in ``strip`` (e.g. ``--hosts`` — the remote should not
    recurse into its own hosts).  Each arg is shell-quoted.  ``program`` is the
    installed entry point (``cgrep``, ``cdir`` — there is no ``ctools`` main
    command).
    """
    strip = set(strip or ())
    args = [a for a in local_argv if a not in strip]
    cmd = f"{program} " + " ".join(shlex.quote(a) for a in args)
    return REMOTE_PATH + cmd


def run_remote_search(base: Base, command: str, timeout: float = 300.0) -> RemoteResult:
    """Run ``command`` on the base's host and collect the output.

    ``command`` is the full remote command (e.g. ``"ctools cgrep -i PATTERN"``).
    The host's home is searched by the remote ctools (its local agents).  A
    missing ``ssh`` or a timeout yields a non-ok result with an error, never a
    raise.
    """
    if shutil.which("ssh") is None:
        return RemoteResult(host=base.name, ok=False, error="ssh not found on this machine")
    argv = base.remote.ssh_command(command)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return RemoteResult(host=base.name, ok=False,
                           error=f"timed out after {timeout:.0f}s")
    except OSError as e:
        return RemoteResult(host=base.name, ok=False, error=str(e))

    out_lines = proc.stdout.splitlines()
    version, error = parse_version_prefix(out_lines, proc.stderr)
    if version is not None:
        # A stale/old host told us its version and a gripe — not matches.
        return RemoteResult(host=base.name, ok=False, version=version,
                           lines=[], error=error)
    ok = proc.returncode == 0
    lines = [f"@{base.name}:{ln}" for ln in out_lines if ln.strip()]
    return RemoteResult(host=base.name, ok=ok, lines=lines,
                       error=(proc.stderr.strip() or None) if not ok else None)


def parse_version_prefix(lines: List[str], stderr: str = "") -> tuple:
    """Detect a leading ``ctools <version>`` line (the version-negotiation form).

    Returns ``(version, error)``.  If the first line is ``ctools <version>``,
    the version is extracted and the remainder (falling back to stderr) is the
    error.  Otherwise ``(None, None)`` — the output is ordinary results.
    """
    if lines and lines[0].startswith("ctools "):
        parts = lines[0].split()
        version = parts[1] if len(parts) > 1 else None
        error = "\n".join(lines[1:]).strip() or (stderr or "").strip() or None
        return version, error
    return None, None