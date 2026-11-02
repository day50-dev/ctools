#!/usr/bin/env python3
"""
pathfilter - "sessions that ran in this directory", shared by the verbs.

Both `cdir` and `cgrep` accept a filesystem path where a session selector
would otherwise go: `cdir opencode .` lists the sessions of this tree, and
`cgrep "pattern" .` searches them.  The two verbs must agree about what that
means, or the same path argument would select different sessions in each, so
the decision lives here:

* :func:`looks_like_path` — is this token a path at all, or an id/glob?
* :func:`session_matches_dir` — does a session belong to this directory?
* :func:`canonical_dir` — the comparison key both use.

A session "belongs" to a directory when it ran *in* it or *inside* it; a
session started above the queried directory is not relevant to it.  Agents
that record no working directory never match a directory filter.
"""

import fnmatch
import os

from ctools.agents import Session

__all__ = ['looks_like_path', 'canonical_dir', 'session_matches_dir', 'is_glob']

# Characters that make a token a glob rather than a literal name.
_GLOB_CHARS = '*?['


def is_glob(token: str) -> bool:
    """Does this token carry glob characters (and so is a pattern, not a path)?"""
    return any(c in token for c in _GLOB_CHARS)


def looks_like_path(part: str) -> bool:
    """Is a token a filesystem path rather than a session id or agent name?

    A path is absolute, ~-anchored, rooted at `.` or `..` (including the bare
    `.`), or carries an inner slash. No agent's session ids look like that,
    so the two shapes never collide.
    """
    return (part.startswith('/') or part.startswith('~')
            or part.startswith('.') or '/' in part)


def canonical_dir(p: str) -> str:
    """Canonical form of a directory for comparison: expand ~, make absolute,
    resolve symlinks. realpath does not require the path to exist, so stale
    session cwds canonicalize as far as their prefix allows."""
    try:
        expanded = os.path.expanduser(p)
    except RuntimeError:
        expanded = p
    try:
        return os.path.realpath(expanded)
    except (OSError, ValueError):
        return os.path.abspath(expanded)


def session_matches_dir(session: Session, path_arg: str) -> bool:
    """Is the session's working directory *relevant* to `path_arg`?

    Relevant means the session ran in the given directory itself or *inside*
    it: `cgrep "x" .` searches the sessions of this tree, not of its
    ancestors (a session started at `~` is not relevant to `~/day50`).
    Comparisons are case-insensitive on canonical (symlink-resolved) paths.
    Glob characters turn the argument into a case-insensitive pattern over
    the session path.
    """
    if not session.path:
        return False
    if is_glob(path_arg):
        pattern = os.path.expanduser(path_arg)
        if not os.path.isabs(pattern):
            pattern = os.path.abspath(pattern)
        haystack = canonical_dir(session.path)
        return (fnmatch.fnmatch(haystack.lower(), pattern.lower())
                or fnmatch.fnmatch(session.path.lower(), pattern.lower()))
    # Compare on lowered canonical keys: matching is case-insensitive (the
    # keys are never used to touch the filesystem).
    target = canonical_dir(path_arg).lower()
    session_dir = canonical_dir(session.path).lower()
    if session_dir == target:
        return True
    # The query at the filesystem root: every session dir is under it.
    if target == os.sep:
        return True
    # Session dir inside the query dir. A session started *above* the query
    # dir is not relevant to it — `cdir pi ..` / `cgrep x ..` find those.
    return (session_dir + os.sep).startswith(target + os.sep)
