#!/usr/bin/env python3
"""
resume - hand a session back to the tool that owns it

`ctools resume agent/session` computes the agent's resume command,
`cd`s into the session's recorded working directory, and `exec`s the
command — replacing the ctools process (the `xdg-open` analogy). It is
the one verb that "leaves ctools".

Usage:
    ctools resume claude-code/55b9c8b7      # what cdir prints, pasted back
    ctools resume claude-code 55b9c8b7      # cd + exec `claude --resume 55b9c8b7`
    ctools resume 55b9c8b7                  # bare id: found in the installed agents
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
from typing import List, Optional, Sequence, Tuple

from rich.console import Console

from ctools.agents import (REGISTRY as AGENTS, AgentError, get_agent,
                          get_resume_command)
from ctools.lib import install_clean_sigint
from ctools.cli import handle_version, version_option

__all__ = ['app', 'parse_ref_args']

console = Console()


def _agent_of(session_id: str) -> Tuple[Optional[str], Optional[str]]:
    """The single installed agent holding `session_id`, or an error.

    A bare id is the laziest thing to paste out of `cdir -1`, so it is looked
    up across the installed agents. It resolves only when exactly one has it;
    an id two agents share is reported rather than guessed at.
    """
    hits: List[str] = []
    for name, agent in AGENTS.items():
        if not agent.exists():
            continue
        try:
            if agent.session(session_id) is not None:
                hits.append(name)
        except AgentError:
            continue
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, (f"no such session: {session_id} "
                      f"(name its agent: agent/{session_id})")
    return None, (f"session {session_id} exists in several agents "
                  f"({', '.join(hits)}); name one, e.g. {hits[0]}/{session_id}")


def parse_ref_args(args: Sequence[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(agent_name, session_id, error)`` for resume's arguments.

    Accepts every shape `cdir` can print, so a listing row pastes straight
    back: ``agent/session`` (or ``@agent/session``), the two-word
    ``agent session`` form, or a bare session id (resolved across the
    installed agents). Errors come back as the third value, never raised.
    """
    args = [str(a) for a in args]
    if len(args) == 1:
        ref = args[0].lstrip("@").strip()
        name, sep, session_id = ref.partition("/")
        if not sep:
            if not ref:
                return None, None, "no session given"
            name, err = _agent_of(ref)
            return (None, None, err) if err else (name, ref, None)
        if get_agent(name) is None:
            if not name:
                return None, None, (f"no agent named in {ref!r} "
                                    f"(available: {', '.join(AGENTS)})")
            return None, None, (f"unknown agent: {name} "
                                f"(available: {', '.join(AGENTS)})")
        if not session_id:
            return None, None, f"no session id given: {ref}"
        return name, session_id, None
    if len(args) == 2:
        return args[0], args[1].lstrip("@"), None
    return None, None, ("expected AGENT/SESSION (or AGENT SESSION), got: "
                        + " ".join(args))


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
        epilog=("The session is written the way cdir prints it: 'agent/session' "
                "(e.g. claude-code/55b9c8b7-788b-4f9e-a8b7-cfa0461b357b), the "
                "two-word 'agent session', or a bare id resolved across the "
                "installed agents."),
    )
    parser.add_argument("refs", nargs="+", metavar="agent/session",
                        help=("The session to hand back: 'agent/session' (what cdir "
                              "prints), 'agent session', or a bare session id"))
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

    # Accept what cdir prints, so a listing row pastes straight back.
    agent_name, session_id, err = parse_ref_args(args.refs)
    if err:
        console.print(f"[red]{err}[/red]")
        return 1

    cmd, cwd, err = _resolve(agent_name, session_id)
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
