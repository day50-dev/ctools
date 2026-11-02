#!/usr/bin/env python3
"""
resume - hand a session back to the tool that owns it

`ctools resume <agent> <session>` computes the agent's resume command,
`cd`s into the session's recorded working directory, and `exec`s the
command — replacing the ctools process (the `xdg-open` analogy). It is
the one verb that "leaves ctools".

Usage:
    ctools resume claude-code a293cac2      # cd + exec `claude --resume a293cac2`
    ctools resume pi <id>                   # `pi --session <id>`
    ctools resume opencode <id>             # `opencode -s <id>`

The resume command comes from the agent backend (agents.py), overridable
per-machine in ~/.config/ctools/resume-commands.json. The hand-off lands
in the session's recorded cwd first — the "No conversation found" root
cause was a cwd/location mismatch.
"""

import argparse
import os
import sys
from typing import Optional, Tuple

from rich.console import Console

from ctools.agents import REGISTRY as AGENTS, get_agent, get_resume_command
from ctools.lib import install_clean_sigint
from ctools.cli import handle_version, version_option

__all__ = ['app']

console = Console()


def _resolve(agent_name: str, session_id: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(command, cwd, error)`` for resuming `session_id` under `agent_name`.

    `command` is the agent's resume command (None when unknown); `cwd` is the
    session's recorded working directory (None when it records none); `error`
    is a human-readable reason when the hand-off cannot proceed.
    """
    agent = get_agent(agent_name)
    if agent is None:
        return None, None, f'unknown agent: {agent_name} (available: {", ".join(AGENTS)})'
    if not agent.exists():
        return None, None, f'{agent.name} is not installed'
    if agent.session(session_id) is None:
        return None, None, f'no such session: {agent_name}/{session_id}'
    cmd = get_resume_command(agent_name, session_id)
    if cmd is None:
        return None, None, (f'no resume command known for {agent_name} '
                           f'(set one in ~/.config/ctools/resume-commands.json)')
    return cmd, agent.session_cwd(session_id), None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resume",
        description=("Hand a session back to the tool that owns it: compute the "
                     "agent's resume command, cd into the session's recorded "
                     "working directory, and exec the command (replacing the "
                     "ctools process)."),
    )
    parser.add_argument("agent", help="The agent that owns the session")
    parser.add_argument("session", help="The session id to resume")
    version_option(parser, "resume")
    return parser


def app() -> int:
    install_clean_sigint()
    argv = list(sys.argv[1:])
    # --version wins over the required positionals (GNU style): scanned before
    # parsing so `ctools resume --version` works without an agent/session.
    if "--version" in argv:
        from ctools import __version__
        print(f"resume (ctxttools) {__version__}")
        return 0
    args = build_parser().parse_args(argv)
    if (v := handle_version(args)) is not None:
        return v

    cmd, cwd, err = _resolve(args.agent, args.session)
    if err:
        console.print(f"[red]{err}[/red]")
        return 1

    # R11: land in the recorded cwd before the exec — the hand-off only works
    # if you're in the right directory.
    if cwd:
        try:
            os.chdir(cwd)
        except OSError:
            console.print(f"[yellow]warning: could not cd into {cwd}; "
                          f"resuming from the current directory[/yellow]")

    # R12: `sh -c` so a spaced argument is never word-split by a bare
    # `exec $(...)`.
    os.execvpe("sh", ["sh", "-c", cmd], os.environ)
    # execvpe only returns on failure.
    console.print("[red]failed to exec resume command[/red]")
    return 1


def main() -> int:
    return app()


if __name__ == "__main__":
    sys.exit(main())
