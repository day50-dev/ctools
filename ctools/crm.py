"""
crm - scalpel remove concepts from sessions.

Surgically removes concept-containing sections from agent sessions.
Concept JSON files are NOT deleted - only the relevant sections from the context.

With no concept files, the user is the strategy: the messages are listed and
the ones to remove are picked by hand (`crm claude-code/<uuid>`).
"""

import shutil
import sys
from pathlib import Path
from typing import List

import argparse
from rich.console import Console
from rich.prompt import Confirm

from ctools.agents import Message
from ctools.backup import BackupError, _vacuum_into
from ctools.cextract import concept_text, load_strategy, read_concepts_from_file
from ctools.cli import reporting, require_session, version_option
from ctools.lib import gettmp, install_clean_sigint

console = Console()

__all__ = ['app']


def _detect_with_strategy(strategy, concept: dict, message_content: str) -> bool:
    """Use strategy to detect if a message contains a concept."""
    import requests

    concept_text = concept.get("short", "") or concept.get("medium", "") or concept.get("description", "")
    if not concept_text:
        return False

    prompt = f"""Does the following message contain or relate to this concept?

Concept: {concept_text}

Message: {message_content}

Answer only "yes" or "no"."""

    conversation = [{"role": "user", "content": prompt}]

    headers = {"Content-Type": "application/json"}
    if strategy.api_key:
        headers["Authorization"] = f"Bearer {strategy.api_key}"

    base_url = strategy.host.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"

    req = {
        "model": strategy.model,
        "messages": conversation,
        "temperature": 0.0,
        "max_tokens": 10,
    }

    try:
        r = requests.post(
            f"{base_url}/chat/completions",
            json=req,
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        resp = r.json()
        answer = resp["choices"][0]["message"]["content"].strip().lower()
        return "yes" in answer
    except Exception as e:
        console.print(f"[yellow]Strategy detection failed: {e}[/yellow]")
        return False


def _concept_matches_concept(concept: dict, message_content: str, strategy=None) -> bool:
    """Check if a message contains content related to a concept."""
    if strategy:
        return _detect_with_strategy(strategy, concept, message_content)

    # Fallback to string matching
    text = concept_text(concept).lower()
    return bool(text) and text in message_content.lower()


def _concept_in_range(concept: dict, messages: List[Message], start: int, end: int,
                      strategy=None) -> bool:
    """Check if a range of messages contains a concept."""
    span = messages[start:end + 1]
    if strategy:
        return _detect_with_strategy(strategy, concept, " ".join(m.content for m in span))

    text = concept_text(concept).lower()
    return bool(text) and text in " ".join(m.content.lower() for m in span)


def _divide_and_conquer(messages: List[Message], concept: dict,
                        strategy=None, verbose: bool = False) -> List[int]:
    """
    Divide and conquer algorithm to find concept-containing sections.

    Recursively splits the message range in half until finding the smallest
    unit that contains the concept.
    """
    indices_to_remove = set()

    def search_range(start: int, end: int):
        if start >= end:
            return 0

        # Check if this range contains the concept
        if not _concept_in_range(concept, messages, start, end, strategy):
            return 0

        # If range is small enough, mark for removal
        if end - start <= 1:
            for i in range(start, end + 1):
                if _concept_matches_concept(concept, messages[i].content, strategy):
                    indices_to_remove.add(i)
                    if verbose:
                        console.print(f"  [yellow]Marking message {i} for removal[/yellow]")
            return 0

        # Divide and conquer
        mid = (start + end) // 2
        search_range(start, mid)
        search_range(mid + 1, end)

    search_range(0, len(messages) - 1)
    return sorted(indices_to_remove)


def _sliding_window(messages: List[Message], concept: dict,
                    size: int = 5, strategy=None, verbose: bool = False) -> List[int]:
    """
    Sliding window algorithm to find concept-containing sections.

    Moves a window through the conversation and marks central messages
    for removal when the window contains the concept.
    """
    indices_to_remove = set()

    for i in range(len(messages)):
        # Define window bounds
        window_start = max(0, i - size // 2)
        window_end = min(len(messages), i + size // 2 + 1)

        # Check if window contains concept
        if _concept_in_range(concept, messages, window_start, window_end, strategy):
            # Mark central message for removal
            if _concept_matches_concept(concept, messages[i].content, strategy):
                indices_to_remove.add(i)
                if verbose:
                    console.print(f"  [yellow]Marking message {i} for removal (window {window_start}-{window_end})[/yellow]")

    return sorted(indices_to_remove)


SEARCHES = {
    'divide': lambda messages, concept, size, strat, verbose:
        _divide_and_conquer(messages, concept, strat, verbose),
    'sliding': lambda messages, concept, size, strat, verbose:
        _sliding_window(messages, concept, size, strat, verbose),
}


def _parse_index_spec(spec: str, n: int) -> set:
    """`"3 7 12-15"` -> {3, 7, 12, 13, 14, 15} within 0..n-1.

    Raises ValueError on garbage or out-of-range values.
    """
    picked = set()
    for tok in spec.split():
        lo, _, hi = tok.partition('-')
        if (not lo.isdigit() or tok.endswith('-') or (hi and not hi.isdigit())):
            raise ValueError(f"'{tok}' is not an index or a-b range")
        lo_i = int(lo)
        hi_i = int(hi) if hi else lo_i
        if lo_i > hi_i or hi_i >= n:
            raise ValueError(f"'{tok}' out of range (valid: 0-{n - 1})")
        picked.update(range(lo_i, hi_i + 1))
    return picked


def _preview(m: Message, limit: int = 100) -> str:
    return m.content[:limit].replace("\n", " ")


def _backup_session(agent, session_id: str) -> Path:
    """Back up one session's storage to ``gettmp()/ctools/<agent>/<uuid>``.

    File agents (claude-code, pi, ...) get their session file copied; sqlite
    agents (opencode) get a consistent full-database snapshot via
    ``VACUUM INTO``, since a session's rows live in a shared database.  An
    existing backup is never clobbered: the next free ``<uuid>-N`` name is
    used, so every removal gets its own snapshot.
    """
    base = gettmp() / "ctools" / agent.name
    base.mkdir(parents=True, exist_ok=True)
    dest = base / session_id
    n = 0
    while dest.exists():
        n += 1
        dest = base / f"{session_id}-{n}"
    if getattr(type(agent), "db_name", None):
        storage = agent.storage_path
        if not storage.is_file():
            raise BackupError(
                f"cannot back up {agent.name}: no storage file at {storage}")
        _vacuum_into(storage, dest)
    else:
        src = None
        for s in agent.sessions():
            if s.id == session_id and s.path and Path(s.path).is_file():
                src = Path(s.path)
                break
        if src is None:
            raise BackupError(
                f"cannot back up {agent.name}/{session_id}: "
                f"session file not found")
        shutil.copy2(str(src), str(dest))
    return dest


def _interactive_remove(messages: List[Message], agent, session_id: str,
                        backup: bool = False) -> int:
    """No concept files: the user is the strategy.

    List every message with an index and a preview, ask which to remove
    (space-separated indices, `a-b` ranges), confirm, remove.
    EOF or an empty answer aborts — removing is never done blindly.
    """
    console.print(f"[dim]{len(messages)} messages in {agent.name}/{session_id}[/dim]")
    for idx, m in enumerate(messages):
        console.print(f"  {idx}: [{m.role}] {_preview(m)}")
    try:
        spec = input("\nIndices to remove (e.g. 3 7 12-15), empty to abort: ")
    except EOFError:
        console.print("[dim]Aborted (no input)[/dim]")
        return 0
    if not spec.strip():
        console.print("[dim]Aborted[/dim]")
        return 0
    try:
        doomed = _parse_index_spec(spec, len(messages))
    except ValueError as e:
        console.print(f"[red]Bad index spec: {e}[/red]")
        raise SystemExit(1)
    if not doomed:
        console.print("[dim]Nothing selected, aborted[/dim]")
        return 0

    console.print(f"\n[yellow]Will remove {len(doomed)} message(s):[/yellow]")
    for idx in sorted(doomed):
        console.print(f"  {idx}: [{messages[idx].role}] {_preview(messages[idx])}\u2026")

    if not Confirm.ask("\nProceed with removal?"):
        console.print("[dim]Aborted[/dim]")
        return 0

    if backup:
        dest = _backup_session(agent, session_id)
        console.print(f"[dim]Backed up session to {dest}[/dim]")
    with reporting():
        removed = agent.remove_messages(session_id, sorted(doomed))

    console.print(f"[green]Removed {removed} message(s) from {agent.name}/{session_id}[/green]")
    return 0



def _maybe_backup(ns, agent, session_id: str) -> None:
    """Take the pre-removal backup when --backup was given."""
    if not ns.backup:
        return
    dest = _backup_session(agent, session_id)
    console.print(f"[dim]Backed up session to {dest}[/dim]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crm",
        description=("Scalpel remove concepts from sessions.\n\n"
                     "Surgically removes concept-containing sections from "
                     "agent sessions. Concept JSON files are NOT deleted — only "
                     "the relevant sections from the context."),
    )
    parser.add_argument("session",
                        help="Session to remove from (agent/session_id)")
    parser.add_argument("concepts", nargs="*",
                        help="Concept JSON files to remove (omit to pick "
                             "messages to remove by hand)")
    parser.add_argument("-a", "--algo", default="divide",
                        help="Algorithm: divide, sliding")
    parser.add_argument("--size", type=int, default=5,
                        help="Window size for sliding algorithm")
    parser.add_argument("-s", "--strategy",
                        help="Strategy JSON file for detection")
    parser.add_argument("--backup", action="store_true",
                        help="Back up the session to gettmp()/ctools/<agent>/"
                             "<uuid> before removing")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="Confirm each removal")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output")
    version_option(parser, "crm")
    return parser


def main(argv=None) -> int:
    install_clean_sigint()
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        from ctools import __version__
        print(f"crm (ctxttools) {__version__}")
        return 0
    ns = build_parser().parse_args(argv)

    session = ns.session
    concepts = ns.concepts
    algo = ns.algo
    size = ns.size
    strategy = ns.strategy
    interactive = ns.interactive
    verbose = ns.verbose

    agent, session_id = require_session(session)

    with reporting():
        messages = agent.raw_messages(session_id)

    if not messages:
        console.print(f"[yellow]Session not found: {session_id}[/yellow]")
        raise SystemExit(1)

    if verbose:
        console.print(f"[dim]Loaded {len(messages)} messages from {agent.name}/{session_id}[/dim]")

    if not concepts:
        # No concept files: the user is the strategy — list the messages,
        # pick the indices to remove by hand.
        return _interactive_remove(messages, agent, session_id, ns.backup)

    all_concepts = []
    for concept_path in concepts:
        if not Path(concept_path).exists():
            console.print(f"[red]Concept file not found: {concept_path}[/red]")
            raise SystemExit(1)
        all_concepts.extend(read_concepts_from_file(concept_path))

    if not all_concepts:
        console.print("[yellow]No concepts found in files[/yellow]")
        return 0

    if verbose:
        console.print(f"[dim]Loaded {len(all_concepts)} concepts[/dim]")

    strat = None
    if strategy:
        strat = load_strategy(strategy)
        if verbose:
            console.print("[dim]Using strategy for detection[/dim]")

    if algo not in SEARCHES:
        console.print(f"[red]Unknown algorithm: {algo}[/red]")
        console.print(f"[dim]Available: {', '.join(SEARCHES)}[/dim]")
        raise SystemExit(1)
    search = SEARCHES[algo]

    doomed = set()
    for concept in all_concepts:
        doomed.update(search(messages, concept, size, strat, verbose))

    if not doomed:
        console.print("[yellow]No matching sections found to remove[/yellow]")
        return 0

    if interactive:
        console.print(f"\n[yellow]Will remove {len(doomed)} message(s):[/yellow]")
        for idx in sorted(doomed):
            preview = messages[idx].content[:100].replace("\n", " ")
            console.print(f"  {idx}: [{messages[idx].role}] {preview}…")

        if not Confirm.ask("\nProceed with removal?"):
            console.print("[dim]Aborted[/dim]")
            return 0

    _maybe_backup(ns, agent, session_id)
    with reporting():
        removed = agent.remove_messages(session_id, sorted(doomed))

    console.print(f"[green]Removed {removed} message(s) from {agent.name}/{session_id}[/green]")
    return 0


app = main


if __name__ == "__main__":
    raise SystemExit(main())
