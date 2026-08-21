#!/usr/bin/env python3
"""
cgrep - grep for LLM context windows

Search through agent session content using PCRE patterns.

Usage:
    cgrep -r "pattern" "opencode/*"
    cgrep -l "pattern" "opencode/ses_abc123"
    cgrep -c "pattern" "opencode/*" "claude-code/*"
"""

import fnmatch
import re
from typing import List, Tuple

import typer
from rich.console import Console

from ctools.agents import Agent, AgentError, Match, REGISTRY as AGENTS
from ctools.lib import get_formatter

__all__ = ['app', 'parse_path_pattern', 'sessions_for_pattern', 'grep_session']

app = typer.Typer()
console = Console()


def parse_path_pattern(pattern: str) -> List[Tuple[Agent, str]]:
    """Parse patterns like 'opencode/*' or 'opencode/ses_abc123'.

    Returns (agent, session_glob) pairs. Unknown agents are reported and
    skipped rather than aborting the whole search.
    """
    results = []
    for pat in pattern.split():
        agent_name, _, session_pat = pat.strip('/').partition('/')
        agent = AGENTS.get(agent_name)
        if agent is None:
            console.print(f"[red]Unknown agent: {agent_name}[/red]")
            continue
        results.append((agent, session_pat or '*'))
    return results


def sessions_for_pattern(agent: Agent, session_pat: str) -> List[str]:
    """Session IDs of `agent` matching a glob."""
    if not agent.exists():
        return []
    try:
        sessions = agent.sessions()
    except AgentError:
        return []
    return [s.id for s in sessions if fnmatch.fnmatch(s.id, session_pat)]


def grep_session(agent: Agent, session_id: str, pattern: re.Pattern,
                 invert: bool = False, before: int = 0, after: int = 0) -> List[Match]:
    """Search one session for pattern matches."""
    if not agent.exists():
        return []
    try:
        lines = agent.lines(session_id)
    except AgentError:
        return []

    matches = []
    for i, (line_num, line) in enumerate(lines):
        if bool(pattern.search(line)) == invert:
            continue
        matches.append(Match(
            session_id=session_id,
            agent=agent.name,
            line_num=line_num,
            line=line,
            context_before=[l for _, l in lines[max(0, i - before):i]] if before else None,
            context_after=[l for _, l in lines[i + 1:i + 1 + after]] if after else None,
        ))
    return matches


def _print_matches(matches: List[Match]) -> None:
    """grep-style output: matches grouped by session, separated by '--'."""
    current_session = None
    for m in matches:
        path = f"{m.agent}/{m.session_id}"
        if path != current_session:
            if current_session is not None:
                print("--")
            current_session = path
        for line in m.context_before or ():
            print(f"  {line}")
        print(f"{m.line_num}:{m.line}")
        for line in m.context_after or ():
            print(f"  {line}")


@app.command()
def main(
    pattern: str = typer.Argument(..., help="PCRE search pattern"),
    paths: List[str] = typer.Argument(..., help="Agent/session paths (e.g., opencode/*)"),
    list_files: bool = typer.Option(False, "--files-with-matches", "-l", help="Show only session IDs with matches"),
    list_files_neg: bool = typer.Option(False, "--files-without-match", "-L", help="Show only session IDs without matches"),
    count: bool = typer.Option(False, "--count", "-c", help="Show match count per session"),
    invert: bool = typer.Option(False, "--invert-match", "-v", help="Invert match"),
    before: int = typer.Option(0, "--before", "-B", help="Show N lines before match"),
    after: int = typer.Option(0, "--after", "-A", help="Show N lines after match"),
    context: int = typer.Option(0, "--context", "-C", help="Show N lines before and after match"),
    ignore_case: bool = typer.Option(False, "--ignore-case", "-i", help="Ignore case"),
    fmt: str = typer.Option("default", "--format", "-f", help="Output format: json, xml, md, or default"),
):
    """
    Search through agent session content.

    Patterns are PCRE. Paths specify agents and optionally session IDs.

    Examples:
        cgrep "error" "opencode/*"
        cgrep -l "TODO" "opencode/*" "claude-code/*"
        cgrep -c "import" "opencode/*"
        cgrep -B2 -A2 "FIXME" "opencode/ses_abc123"
    """
    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        console.print(f"[red]Invalid pattern: {e}[/red]")
        raise typer.Exit(1)

    if context > 0:
        before = after = context

    formatter = None
    if fmt != "default":
        try:
            formatter = get_formatter(fmt)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    all_matches = []
    with_matches = set()
    without_matches = set()
    counts = {}

    for agent, session_pat in parse_path_pattern(' '.join(paths)):
        for session_id in sessions_for_pattern(agent, session_pat):
            matches = grep_session(agent, session_id, compiled,
                                   invert=invert, before=before, after=after)
            path = f"{agent.name}/{session_id}"
            if matches:
                with_matches.add(path)
                counts[path] = len(matches)
                all_matches.extend(matches)
            else:
                without_matches.add(path)

    if list_files or list_files_neg:
        found = list_files
        files = sorted(with_matches if found else without_matches)
        if formatter:
            print(formatter.format_match_files(files, has_matches=found))
        else:
            for path in files:
                print(path)
    elif count:
        if formatter:
            print(formatter.format_match_counts(counts))
        else:
            for path, cnt in sorted(counts.items()):
                print(f"{path}:{cnt}")
    elif formatter:
        print(formatter.format_matches(all_matches))
    elif all_matches:
        _print_matches(all_matches)
    else:
        console.print("[dim]No matches found[/dim]")


if __name__ == "__main__":
    app()
