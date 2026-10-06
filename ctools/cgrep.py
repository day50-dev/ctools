#!/usr/bin/env python3
"""
cgrep - grep for LLM context windows

Search through agent session content using regex patterns.

Exit status is grep-compatible:
    0   at least one match was found
    1   no matches
    2   error (bad pattern, unknown agent, missing agent data)

Usage:
    cgrep "pattern" "opencode/*"
    cgrep -l "pattern" "opencode/ses_abc123"
    cgrep -c "pattern" "opencode/*" "claude-code/*"
    cgrep -h "pattern" "opencode/ses_abc123"   # drop the session path prefix
    cgrep -q "pattern" "opencode/*"            # exit status only
"""

import fnmatch
import re
from typing import List, Optional, Tuple

import typer
from rich.console import Console

from ctools.agents import Agent, AgentError, Match, get_agent
from ctools.cli import version_option
from ctools.lib import get_formatter

__all__ = ['app', 'parse_path_pattern', 'sessions_for_pattern', 'grep_session',
           'EXIT_MATCH', 'EXIT_NO_MATCH', 'EXIT_ERROR']

app = typer.Typer()
console = Console()

# grep-compatible exit statuses.
EXIT_MATCH = 0
EXIT_NO_MATCH = 1
EXIT_ERROR = 2


def _resolve_path_patterns(pattern: str) -> Tuple[List[Tuple[Agent, str]], List[str]]:
    """Resolve path patterns into (agent, session_glob) pairs plus unknown names."""
    results, unknown = [], []
    for pat in pattern.split():
        agent_name, _, session_pat = pat.strip('/').partition('/')
        agent = get_agent(agent_name)
        if agent is None:
            unknown.append(agent_name)
            continue
        results.append((agent, session_pat or '*'))
    return results, unknown


def parse_path_pattern(pattern: str) -> List[Tuple[Agent, str]]:
    """Parse patterns like 'opencode/*' or 'opencode/ses_abc123'.

    Returns (agent, session_glob) pairs. Unknown agents are reported and
    skipped rather than aborting the whole search.
    """
    results, unknown = _resolve_path_patterns(pattern)
    for agent_name in unknown:
        console.print(f"[red]Unknown agent: {agent_name}[/red]")
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


def _session_selected(path: str, include: Optional[List[str]],
                      exclude: Optional[List[str]]) -> bool:
    """Apply --include/--exclude globs to an agent/session_id path."""
    if include and not any(fnmatch.fnmatch(path, pat) for pat in include):
        return False
    if exclude and any(fnmatch.fnmatch(path, pat) for pat in exclude):
        return False
    return True


def _compile_pattern(pattern: str, flags: int = 0, whole_word: bool = False,
                     whole_line: bool = False, fixed_string: bool = False) -> re.Pattern:
    """Compile a pattern with -w/-x/-F applied.

    -F treats the pattern as a fixed string; -w wraps it in word boundaries;
    -x anchors it to the whole line. Word wrapping happens first so -x -w
    together means "whole line and whole word".
    """
    body = re.escape(pattern) if fixed_string else pattern
    if whole_word:
        body = rf"\b(?:{body})\b"
    if whole_line:
        body = rf"\A(?:{body})\Z"
    return re.compile(body, flags)


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


def _expand_only_matching(matches: List[Match], pattern: re.Pattern) -> List[Match]:
    """Turn each matched line into one Match per occurrence (-o)."""
    expanded = []
    for m in matches:
        for hit in pattern.finditer(m.line):
            expanded.append(Match(
                session_id=m.session_id,
                agent=m.agent,
                line_num=m.line_num,
                line=hit.group(0),
                context_before=m.context_before,
                context_after=m.context_after,
            ))
    return expanded


def _print_matches(matches: List[Match], show_filename: bool = True) -> None:
    """grep-style output: `path:lineno:text`, grouped by session, separated by '--'.

    Context lines use grep's `-` separators (`path-lineno-text`). Session line
    numbers are contiguous, so context numbers follow from the match line.
    """
    current_session = None
    for m in matches:
        path = f"{m.agent}/{m.session_id}"
        if path != current_session:
            if current_session is not None:
                print("--")
            current_session = path
        prefix = f"{path}:" if show_filename else ""
        ctx_prefix = f"{path}-" if show_filename else ""

        before = m.context_before or ()
        for i, line in enumerate(before):
            print(f"{ctx_prefix}{m.line_num - len(before) + i}-{line}")
        print(f"{prefix}{m.line_num}:{m.line}")
        for i, line in enumerate(m.context_after or (), start=1):
            print(f"{ctx_prefix}{m.line_num + i}-{line}")


def _show_filename(no_filename: bool, with_filename: bool) -> bool:
    """Resolve -h/-H the way grep does: -H wins when both are given."""
    return with_filename or not no_filename


def _exit_status(errors: bool, has_result: bool) -> int:
    """grep's exit ladder: an error outranks a match, which outranks nothing."""
    if errors:
        return EXIT_ERROR
    return EXIT_MATCH if has_result else EXIT_NO_MATCH


@app.command()
def main(
    pattern: str = typer.Argument(..., help="Regex search pattern"),
    paths: List[str] = typer.Argument(..., help="Agent/session paths (e.g., opencode/*)"),
    list_files: bool = typer.Option(False, "--files-with-matches", "-l", help="Show only session IDs with matches"),
    list_files_neg: bool = typer.Option(False, "--files-without-match", "-L", help="Show only session IDs without matches"),
    no_filename: bool = typer.Option(False, "--no-filename", "-h", help="Suppress the session path prefix"),
    with_filename: bool = typer.Option(False, "--with-filename", "-H", help="Force the session path prefix (default)"),
    max_matches: Optional[int] = typer.Option(None, "--max-count", "-m", help="Stop after N matches per session"),
    only_matching: bool = typer.Option(False, "--only-matching", "-o", help="Print only the matching text, one hit per line"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress output; rely on the exit status"),
    whole_word: bool = typer.Option(False, "--word-regexp", "-w", help="Match only whole words"),
    whole_line: bool = typer.Option(False, "--line-regexp", "-x", help="Match only whole lines"),
    extended: bool = typer.Option(False, "--extended-regexp", "-E", help="Extended regex (the default; accepted for grep compatibility)"),
    fixed_string: bool = typer.Option(False, "--fixed-strings", "-F", help="Treat the pattern as a fixed string, not a regex"),
    include: Optional[List[str]] = typer.Option(None, "--include", help="Only search sessions matching this glob (e.g. 'opencode/ses_*'); repeatable"),
    exclude: Optional[List[str]] = typer.Option(None, "--exclude", help="Skip sessions matching this glob; repeatable"),
    count: bool = typer.Option(False, "--count", "-c", help="Show match count per session"),
    invert: bool = typer.Option(False, "--invert-match", "-v", help="Invert match"),
    before: int = typer.Option(0, "--before", "-B", help="Show N lines before match"),
    after: int = typer.Option(0, "--after", "-A", help="Show N lines after match"),
    context: int = typer.Option(0, "--context", "-C", help="Show N lines before and after match"),
    ignore_case: bool = typer.Option(False, "--ignore-case", "-i", help="Ignore case"),
    fmt: str = typer.Option("default", "--format", "-f", help="Output format: json, xml, md, or default"),
    version: bool = version_option("cgrep"),
):
    """
    Search through agent session content.

    Patterns are regex. Paths specify agents and optionally session IDs.

    Examples:
        cgrep "error" "opencode/*"
        cgrep -l "TODO" "opencode/*" "claude-code/*"
        cgrep -c "import" "opencode/*"
        cgrep -B2 -A2 "FIXME" "opencode/ses_abc123"
        cgrep -m1 "import" "opencode/*"
        cgrep -o "gpt-[0-9.]+" "opencode/*" --exclude 'opencode/*_tmp'

    Match lines are prefixed with the session path, grep-style. Use -h to
    suppress that prefix, or -H to force it back on. Exit status is 0 when
    anything matched, 1 when nothing did, 2 on error.
    """
    if max_matches is not None and max_matches < 1:
        console.print("[red]Invalid max count: must be >= 1[/red]")
        raise typer.Exit(EXIT_ERROR)

    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = _compile_pattern(pattern, flags, whole_word, whole_line, fixed_string)
    except re.error as e:
        console.print(f"[red]Invalid pattern: {e}[/red]")
        raise typer.Exit(EXIT_ERROR)

    if context > 0:
        before = after = context

    show_filename = _show_filename(no_filename, with_filename)

    formatter = None
    if fmt != "default":
        try:
            formatter = get_formatter(fmt)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(EXIT_ERROR)

    errors = False
    agent_specs, unknown_agents = _resolve_path_patterns(' '.join(paths))
    for agent_name in unknown_agents:
        console.print(f"[red]Unknown agent: {agent_name}[/red]")
        errors = True

    all_matches = []
    with_matches = set()
    without_matches = set()
    counts = {}

    for agent, session_pat in agent_specs:
        if not agent.exists():
            console.print(f"[yellow]{agent.name}: agent data not found[/yellow]")
            errors = True
            continue
        for session_id in sessions_for_pattern(agent, session_pat):
            path = f"{agent.name}/{session_id}"
            if not _session_selected(path, include, exclude):
                continue
            matches = grep_session(agent, session_id, compiled,
                                   invert=invert, before=before, after=after)
            if max_matches is not None:
                matches = matches[:max_matches]
            if only_matching:
                matches = _expand_only_matching(matches, compiled)
            if matches:
                with_matches.add(path)
                counts[path] = len(matches)
                all_matches.extend(matches)
            else:
                without_matches.add(path)

    if list_files:
        has_result = bool(with_matches)
        files = sorted(with_matches)
    elif list_files_neg:
        has_result = bool(without_matches)
        files = sorted(without_matches)
    elif count:
        has_result = bool(with_matches)
        files = []
    else:
        has_result = bool(all_matches)
        files = []

    if quiet:
        raise typer.Exit(_exit_status(errors, has_result))

    if list_files or list_files_neg:
        if formatter:
            print(formatter.format_match_files(files, has_matches=list_files))
        else:
            for path in files:
                print(path)
    elif count:
        if formatter:
            print(formatter.format_match_counts(counts))
        elif not show_filename:
            for path, cnt in sorted(counts.items()):
                print(cnt)
        else:
            for path, cnt in sorted(counts.items()):
                print(f"{path}:{cnt}")
    elif formatter:
        print(formatter.format_matches(all_matches))
    elif all_matches:
        _print_matches(all_matches, show_filename)
    else:
        console.print("[dim]No matches found[/dim]")

    raise typer.Exit(_exit_status(errors, has_result))


if __name__ == "__main__":
    app()
