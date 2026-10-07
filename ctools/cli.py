#!/usr/bin/env python3
"""
cli - plumbing shared by the ctools commands.

Reference parsing ("opencode/ses_abc"), agent lookup and uniform error
reporting live here so that every command resolves and complains the
same way.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from typing import List, Optional, Tuple

import typer
from rich.console import Console

from ctools import __version__
from ctools.agents import Agent, AgentError, REGISTRY, get_agent

console = Console()


def version_option(tool: str):
    """A GNU-style ``--version`` option printing ``tool (ctxttools) X.Y.Z``.

    Eager, so it wins over required arguments: ``crm --version`` works
    without a session.
    """
    def callback(ctx, value):
        if value:
            typer.echo(f"{tool} (ctxttools) {__version__}")
            raise typer.Exit()
    return typer.Option(False, "--version", callback=callback, is_eager=True,
                        help="Print version information and exit")


def parse_ref(ref: str) -> Tuple[str, Optional[str]]:
    """Split an ``agent`` or ``agent/session_id`` reference.

    A leading ``@`` (used by cextract and cconnect to mark session arguments)
    and surrounding slashes are ignored.
    """
    parts = ref.lstrip('@').strip('/').split('/', 1)
    return parts[0], (parts[1] if len(parts) > 1 else None)


@dataclass
class Remote:
    """A remote host addressed as ``ssh://[user@]host[:port]/path``."""

    host: str
    user: Optional[str] = None
    port: Optional[int] = None
    path: str = ""

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def ssh_command(self, command: str) -> List[str]:
        """argv for ``ssh`` running `command` on the host.

        ``-T`` disables tty allocation so the remote command's stdout stays
        clean for capture.
        """
        args = ["ssh", "-T"]
        if self.port is not None:
            args += ["-p", str(self.port)]
        return args + [self.target, command]

    def __str__(self) -> str:
        auth = f"{self.user}@" if self.user else ""
        port = f":{self.port}" if self.port is not None else ""
        return f"ssh://{auth}{self.host}{port}"


def parse_remote_ref(ref: str) -> Tuple[Optional[Remote], str]:
    """Split a reference into ``(remote, local_ref)``.

    A reference of the form ``ssh://[user@]host[:port]/path`` yields
    ``(Remote, path)``; anything else is local and yields ``(None, ref)``.
    """
    if not ref.startswith("ssh://"):
        return None, ref
    rest = ref[len("ssh://"):]
    head, _, path = rest.partition("/")
    if not head:
        raise ValueError(f"no host in {ref}")
    user: Optional[str] = None
    host = head
    if "@" in head:
        user, _, host = head.partition("@")
    port: Optional[int] = None
    if ":" in host:
        maybe_host, _, maybe_port = host.partition(":")
        if maybe_port.isdigit():
            host, port = maybe_host, int(maybe_port)
    if not host:
        raise ValueError(f"no host in {ref}")
    return Remote(host=host, user=user or None, port=port, path=path), path


def require_agent(name: str) -> Agent:
    """Look up an agent by name, or exit with a usage message."""
    agent = get_agent(name)
    if agent is None:
        console.print(f"[red]Unknown agent: {name}[/red]")
        console.print(f"[dim]Available agents: {', '.join(REGISTRY)}[/dim]")
        raise typer.Exit(1)
    return agent


def require_installed(name: str) -> Agent:
    """Look up an agent and confirm its storage is present."""
    agent = require_agent(name)
    if not agent.exists():
        console.print(f"[yellow]Agent path not found: {agent.base_path}[/yellow]")
        console.print(f"[dim]Is {agent.name} installed?[/dim]")
        raise typer.Exit(1)
    return agent


def require_session(ref: str) -> Tuple[Agent, str]:
    """Resolve ``agent/session_id`` to an installed agent and a session id."""
    name, session_id = parse_ref(ref)
    agent = require_installed(name)
    if not session_id:
        console.print(f"[red]No session ID in {ref}[/red]")
        raise typer.Exit(1)
    return agent, session_id


@contextmanager
def reporting():
    """Turn an :class:`AgentError` into a clean message and exit code 1."""
    try:
        yield
    except AgentError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1)


_COMMANDS = {
    "cdir": "ctools.cdir:app",
    "cgrep": "ctools.cgrep:app",
    "ccat": "ctools.ccat:app",
    "ccopy": "ctools.ccopy:app",
    "cextract": "ctools.cextract:app",
    "cconnect": "ctools.cconnect:app",
    "cdu": "ctools.cdu:app",
    "crm": "ctools.crm:app",
}


def run_command(name: str, argv: List[str]) -> None:
    """Invoke a ctools command by name with `argv`.

    Backs ``python -m ctools.cli run NAME ARGS...`` — a handy way to run a
    ctools command through a python interpreter directly.
    """
    entry = _COMMANDS.get(name)
    if entry is None:
        print(f"unknown command: {name} (known: {', '.join(sorted(_COMMANDS))})")
        raise typer.Exit(2)
    module_name, attr = entry.split(":")
    import importlib
    app = getattr(importlib.import_module(module_name), attr)
    app(args=argv, prog_name=name, standalone_mode=False)


def _main() -> None:
    import sys
    argv = sys.argv[1:]
    if not argv:
        print("usage: python -m ctools.cli run <command> [args...]")
        sys.exit(2)
    if argv[0] != "run":
        print("usage: python -m ctools.cli run <command> [args...]")
        sys.exit(2)
    name, rest = argv[1], argv[2:]
    try:
        run_command(name, rest)
    except typer.Exit as e:
        sys.exit(e.exit_code)
    except SystemExit as e:
        sys.exit(e.code if isinstance(e.code, int) else 1)


if __name__ == "__main__":
    _main()
