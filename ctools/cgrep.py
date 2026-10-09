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
    cgrep -f patterns.txt "opencode/*"         # patterns from a file (one per line)
    cgrep -e "foo" -e "bar" "opencode/*"       # multiple patterns
    cgrep -t json "pattern" "opencode/*"       # output as json (type: json/xml/md)
"""

import fnmatch
import re
import sys
from typing import IO, List, Optional, Tuple

import argparse
from rich.console import Console

from ctools.agents import REGISTRY, Agent, AgentError, Match, get_agent
from ctools.cli import version_option
from ctools.lib import get_formatter

__all__ = ['app', 'parse_path_pattern', 'sessions_for_pattern', 'grep_session',
           'EXIT_MATCH', 'EXIT_NO_MATCH', 'EXIT_ERROR']

console = Console()

# grep-compatible exit statuses.
EXIT_MATCH = 0
EXIT_NO_MATCH = 1
EXIT_ERROR = 2


def _all_agents() -> List[Tuple[Agent, str]]:
    """Every installed agent, matched against all of its sessions."""
    return [(agent, '*') for agent in REGISTRY.values() if agent.exists()]


def _resolve_path_patterns(pattern: str) -> Tuple[List[Tuple[Agent, str]], List[str]]:
    """Resolve path patterns into (agent, session_glob) pairs plus unknown names.

    A bare ``*`` (or empty) means every installed agent, every session.
    """
    tokens = pattern.split()
    if not tokens or tokens == ['*']:
        return _all_agents(), []
    results, unknown = [], []
    for pat in tokens:
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
    """Batch-mode grep-style output: `path:lineno:text`, separated by '--'."""
    state = _StreamState(show_filename, sys.stdout)
    for m in matches:
        state.emit(m)


# Matches in the default (grep-style) format are written here as each session
# is searched, so they reach the terminal line-by-line as soon as they exist.
# Tests point this at a StringIO to capture the streamed output.
_stream: IO[str] = sys.stdout


class _StreamState:
    """Tracks the last session we printed so we can interpose grep's '--'.

    One instance per streamed output; calling .emit for a match prints that
    match's lines immediately (flushed, no end-of-search delay).
    """

    def __init__(self, show_filename: bool, stream: Optional[IO[str]] = None):
        self.show_filename = show_filename
        self.stream = stream if stream is not None else _stream
        self.last_session: Optional[str] = None

    def emit(self, m: Match) -> None:
        path = f"{m.agent}/{m.session_id}"
        if path != self.last_session:
            if self.last_session is not None:
                print("--", file=self.stream, flush=True)
            self.last_session = path
        prefix = f"{path}:" if self.show_filename else ""
        ctx_prefix = f"{path}-" if self.show_filename else ""
        before = m.context_before or ()
        for i, line in enumerate(before):
            print(f"{ctx_prefix}{m.line_num - len(before) + i}-{line}",
                  file=self.stream, flush=True)
        print(f"{prefix}{m.line_num}:{m.line}", file=self.stream, flush=True)
        for i, line in enumerate(m.context_after or (), start=1):
            print(f"{ctx_prefix}{m.line_num + i}-{line}", file=self.stream, flush=True)


def stream_session_matches(state: _StreamState, matches: List[Match]) -> None:
    """Print a session's matches right now, one line at a time."""
    for m in matches:
        state.emit(m)


def _show_filename(no_filename: bool, with_filename: bool) -> bool:
    """Resolve -h/-H the way grep does: -H wins when both are given."""
    return with_filename or not no_filename


def _exit_status(errors: bool, has_result: bool) -> int:
    """grep's exit ladder: an error outranks a match, which outranks nothing."""
    if errors:
        return EXIT_ERROR
    return EXIT_MATCH if has_result else EXIT_NO_MATCH


def build_parser() -> argparse.ArgumentParser:
    # GNU-grep-style layout: a Usage line, a tight summary + example up top,
    # then options grouped the way grep groups them (pattern selection,
    # output control, context control), then a trailing note on paths and
    # exit status (like grep's "When FILE is '-'..." paragraph). The
    # RawDescriptionHelpFormatter keeps our explicit newlines/spaces verbatim
    # instead of reflowing the description into one wrapped blob.
    parser = argparse.ArgumentParser(
        prog="cgrep",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Search for PATTERNS in agent session content.\n"
            "Example: cgrep -i 'hello world' opencode/* claude-code/*\n\n"
            "PATTERNS is a regular expression.\n"
            "Paths are AGENT/SESSION-GLOB pairs (e.g. opencode/ses_abc123);\n"
            "a bare '*' (or no paths) searches every installed agent."
        ),
        epilog=(
            "Match lines are prefixed with the session path, grep-style;\n"
            "--no-filename (-h) suppresses that prefix, --with-filename (-H)\n"
            "forces it on.\n\n"
            "Exit status is 0 if any line is selected, 1 otherwise; if any\n"
            "error occurs (bad pattern, unknown agent, missing agent data)\n"
            "the exit status is 2."
        ),
    )
    parser.add_argument("pattern", nargs="?", default=None,
                        help="regex search pattern (omit if -f/-e supplies it)")
    parser.add_argument("paths", nargs="*",
                        help="AGENT/SESSION paths (e.g. opencode/*); omit or pass '*' for all agents")
    parser.add_argument("--help", action="help", help="display this help text and exit")

    g = parser.add_argument_group("Pattern selection and interpretation")
    g.add_argument("-E", "--extended-regexp", action="store_true",
                   help="PATTERNS are extended regular expressions (the default)")
    g.add_argument("-F", "--fixed-strings", action="store_true",
                   help="PATTERNS are literal strings, not a regex")
    g.add_argument("-e", "--regexp", dest="extra_pattern", metavar="PATTERNS",
                   action="append",
                   help="use PATTERNS for matching (repeatable; combined with the pattern argument)")
    g.add_argument("-f", "--file", dest="pattern_file", metavar="FILE",
                   help="take PATTERNS from FILE, one pattern per line (repeatable)")
    g.add_argument("-i", "--ignore-case", action="store_true",
                   help="ignore case distinctions in patterns and data")
    g.add_argument("-w", "--word-regexp", action="store_true",
                   help="match only whole words")
    g.add_argument("-x", "--line-regexp", action="store_true",
                   help="match only whole lines")

    g = parser.add_argument_group("Output control")
    g.add_argument("-c", "--count", action="store_true",
                   help="print only a count of matches per session")
    g.add_argument("-l", "--files-with-matches", action="store_true",
                   help="print only the session IDs with matches")
    g.add_argument("-L", "--files-without-match", action="store_true",
                   help="print only the session IDs without matches")
    g.add_argument("-m", "--max-count", type=int,
                   help="stop after N matches per session")
    g.add_argument("-o", "--only-matching", action="store_true",
                   help="print only the matching text, one hit per line")
    g.add_argument("-q", "--quiet", "--silent", action="store_true",
                   help="suppress all normal output (exit status only)")
    g.add_argument("-h", "--no-filename", action="store_true",
                   help="suppress the session path prefix")
    g.add_argument("-H", "--with-filename", action="store_true",
                   help="force the session path prefix (default)")
    g.add_argument("--include", action="append",
                   help="search only sessions matching this glob (repeatable)")
    g.add_argument("--exclude", action="append",
                   help="skip sessions matching this glob (repeatable)")
    g.add_argument("-a", "--all", action="store_true",
                   help="search every installed agent (same as omitting paths)")
    g.add_argument("-t", "--type", dest="fmt", default="default",
                   help="output type: default, json, xml, or md")

    g = parser.add_argument_group("Context control")
    g.add_argument("-B", "--before-context", dest="before", type=int, default=0, metavar="NUM",
                   help="print NUM lines of leading context")
    g.add_argument("-A", "--after-context", dest="after", type=int, default=0, metavar="NUM",
                   help="print NUM lines of trailing context")
    g.add_argument("-C", "--context", type=int, default=0, metavar="NUM",
                   help="print NUM lines before and after each match")
    g.add_argument("-v", "--invert-match", action="store_true",
                   help="select non-matching lines")

    version_option(parser, "cgrep")
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        from ctools import __version__
        print(f"cgrep (ctxttools) {__version__}")
        return 0
    ns = build_parser().parse_args(argv)

    # Gather patterns from the positional, -e, and -f (one per line). This
    # mirrors grep: a pattern can come from the command line, from -e
    # repeatable arguments, or from a file (or any combination).
    #
    # argparse's nargs='?' pattern + nargs='*' paths is greedy: when -f/-e is
    # present AND no explicit paths were given, a lone trailing token lands in
    # `pattern` even though the user meant it as a path. Detect and fix that
    # one ambiguous case: if -f/-e supplied patterns and `paths` is empty, the
    # `pattern` token was really a path.
    pattern_arg = ns.pattern
    paths = list(ns.paths or [])
    if pattern_arg is not None and paths == [] and (ns.pattern_file or ns.extra_pattern):
        # The lone token was meant as a path, not a pattern.
        paths = [pattern_arg]
        pattern_arg = None

    raw_patterns: List[str] = []
    if pattern_arg is not None:
        raw_patterns.append(pattern_arg)
    if ns.extra_pattern:
        raw_patterns.extend(ns.extra_pattern)
    if ns.pattern_file:
        # -f is a single path (store, not append), so wrap it for the loop.
        for fname in [ns.pattern_file]:
            try:
                with open(fname, "r", encoding="utf-8") as fh:
                    raw_patterns.extend(
                        ln.rstrip("\r\n") for ln in fh if ln.strip())
            except OSError as e:
                console.print(f"[red]Cannot read pattern file {fname}: {e}[/red]")
                raise SystemExit(EXIT_ERROR)
    if not raw_patterns:
        console.print("[red]No pattern given (use a pattern argument, -e, or -f).[/red]")
        raise SystemExit(EXIT_ERROR)
    list_files = ns.files_with_matches
    list_files_neg = ns.files_without_match
    no_filename = ns.no_filename
    with_filename = ns.with_filename
    max_matches = ns.max_count
    only_matching = ns.only_matching
    quiet = ns.quiet
    whole_word = ns.word_regexp
    whole_line = ns.line_regexp
    fixed_string = ns.fixed_strings
    include = ns.include
    exclude = ns.exclude
    all_agents = ns.all
    count = ns.count
    invert = ns.invert_match
    before = ns.before
    after = ns.after
    context = ns.context
    ignore_case = ns.ignore_case
    fmt = ns.fmt

    if max_matches is not None and max_matches < 1:
        console.print("[red]Invalid max count: must be >= 1[/red]")
        raise SystemExit(EXIT_ERROR)

    if all_agents:
        paths = ["*"]

    flags = re.IGNORECASE if ignore_case else 0
    # Combine every pattern into one alternation so the single compiled
    # pattern works with grep_session / -o unchanged. With -F each line is a
    # literal string, so escape it first; otherwise each is a regex. Joining
    # with '|' means a line matches if ANY pattern matches it (grep semantics).
    # The body is pre-joined/pre-escaped, so pass fixed_string=False to
    # _compile_pattern and let it only apply the -w / -x wrapping.
    body = "|".join(
        (re.escape(p) if fixed_string else p) for p in raw_patterns)
    try:
        compiled = _compile_pattern(body, flags, whole_word, whole_line, fixed_string=False)
    except re.error as e:
        console.print(f"[red]Invalid pattern: {e}[/red]")
        raise SystemExit(EXIT_ERROR)

    if context > 0:
        before = after = context

    show_filename = _show_filename(no_filename, with_filename)

    formatter = None
    if fmt != "default":
        try:
            formatter = get_formatter(fmt)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise SystemExit(EXIT_ERROR)

    errors = False
    agent_specs, unknown_agents = _resolve_path_patterns(' '.join(paths or []))
    for agent_name in unknown_agents:
        console.print(f"[red]Unknown agent: {agent_name}[/red]")
        errors = True

    # Streaming modes print matches as each session is searched, line by line,
    # so results reach the terminal as soon as they exist (like grep on a
    # slow directory). Count/list/formatted modes still buffer.
    streaming = not quiet and not list_files and not list_files_neg \
        and not count and formatter is None
    stream = _StreamState(show_filename) if streaming else None

    with_matches = set()
    without_matches = set()
    counts = {}
    all_matches = []

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
                if stream is not None:
                    stream_session_matches(stream, matches)
                else:
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
        has_result = bool(with_matches)
        files = []

    if quiet:
        raise SystemExit(_exit_status(errors, has_result))

    if streaming:
        if not has_result:
            print("No matches found", file=stream.stream, flush=True)
        raise SystemExit(_exit_status(errors, has_result))

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

    raise SystemExit(_exit_status(errors, has_result))


app = main


if __name__ == "__main__":
    raise SystemExit(main())
