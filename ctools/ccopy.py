#!/usr/bin/env python3
"""
ccopy - copy concepts between sessions and concept files

Concepts are constraints, goals, preferences, observations, and references
that can be extracted from agent sessions and stored in JSON files.

Usage:
    ccopy @opencode/ses_abc                          # dump concepts to stdout (for testing)
    ccopy @opencode/ses_abc concepts/                # extract to directory (one file per concept)
    ccopy @opencode/ses_abc concepts.json            # extract to single file
    ccopy concepts/ @opencode/ses_abc                # inject all concepts from directory
    ccopy constraints.json @opencode/ses_abc         # inject from file
    ccopy @opencode/ses_abc @claude/ses_xyz          # copy concepts between sessions
    ccopy --strategy my-strategy.json @opencode/ses_abc concepts/  # use custom extraction strategy
"""

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import typer
from rich.console import Console

from ctools.agents import Agent, Message
from ctools.cli import reporting, require_session
from ctools.log import configure_logging, get_logger
from ctools.strategy import Strategy, DEFAULT_STRATEGY

app = typer.Typer()
console = Console()
log = get_logger()

CONCEPT_TYPES = ("constraint", "goal", "preference", "observation", "reference")

CONCEPT_PATTERN = re.compile(
    r"Use the following (constraint|goal|preference|observation|reference):\s*(.*)",
    re.IGNORECASE,
)


# --- Concepts ---

def concept_text(concept: dict) -> str:
    """The best available rendering of a concept, longest useful first."""
    return (concept.get("short") or concept.get("medium")
            or concept.get("long") or concept.get("description", ""))


def _filter_concepts(concepts: list, filter_config: dict) -> list:
    """Filter concepts based on filter configuration."""
    if not filter_config:
        return concepts

    prompt = (filter_config.get("prompt") or "").lower()
    types = filter_config.get("types", [])
    exclude_types = filter_config.get("exclude_types", [])

    filtered = []
    for c in concepts:
        ctype = c.get("type", "")
        short = c.get("short", "")[:60]

        if types and ctype not in types:
            log.debug("concept_filtered", reason="type_not_included", type=ctype, short=short)
            continue

        if exclude_types and ctype in exclude_types:
            log.debug("concept_filtered", reason="type_excluded", type=ctype, short=short)
            continue

        if prompt and prompt not in c.get("description", "").lower() \
                and prompt not in c.get("short", "").lower():
            log.debug("concept_filtered", reason="prompt_no_match", prompt=prompt, short=short)
            continue

        log.debug("concept_passed", type=ctype, short=short)
        filtered.append(c)

    return filtered


def parse_args(args: List[str]) -> Tuple[List[str], List[str]]:
    """Split arguments into session refs (@) and concept file paths."""
    sessions = []
    files = []
    for arg in args:
        if arg.startswith("@"):
            sessions.append(arg[1:])
        else:
            files.append(arg)
    return sessions, files


def extract_concepts_from_messages(messages: List[Message]) -> list:
    """Scan messages for concept patterns and return concept objects."""
    concepts = []
    seen = set()
    for msg in messages:
        for line in msg.content.split("\n"):
            m = CONCEPT_PATTERN.match(line.strip())
            if not m:
                continue
            ctype = m.group(1).lower()
            text = m.group(2).strip()
            if (ctype, text) in seen:
                continue
            seen.add((ctype, text))
            concepts.append({
                "type": ctype,
                "description": text[:50],
                "short": text[:250],
                "medium": text[:1000],
                "long": text[:2500],
            })
    return concepts


def concepts_to_text(concepts: list) -> str:
    """Render concepts as the system-message body agents are given."""
    return "\n".join(
        f"Use the following {c.get('type', 'preference')}: {concept_text(c)}"
        for c in concepts
    )


def concepts_to_messages(concepts: list) -> List[Message]:
    """Convert concept objects to a system message with concept lines."""
    if not concepts:
        return []
    return [Message(role="system", content=concepts_to_text(concepts))]


# --- Concept storage ---

def read_concepts_from_file(path: str) -> list:
    """Read concept JSON array from a file."""
    p = Path(path)
    if not p.exists():
        console.print(f"[red]File not found: {path}[/red]")
        raise typer.Exit(1)
    with open(p) as f:
        data = json.load(f)
    if not isinstance(data, list):
        console.print(f"[red]Expected JSON array in {path}[/red]")
        raise typer.Exit(1)
    return data


def write_concepts_to_file(concepts: list, path: str):
    """Write concept JSON array to a file."""
    with open(path, "w") as f:
        json.dump(concepts, f, indent=2)
        f.write("\n")


def concept_id(concept: dict) -> str:
    """Generate a stable ID for a concept based on its content."""
    key = f"{concept.get('type', '')}:{concept.get('description', '')}:{concept.get('short', '')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def write_concept_individual(concept: dict, dir_path: str):
    """Write a single concept to its own JSON file in a directory."""
    p = Path(dir_path)
    p.mkdir(parents=True, exist_ok=True)

    filepath = p / f"{concept.get('type', 'unknown')}_{concept_id(concept)}.json"
    with open(filepath, "w") as f:
        json.dump(concept, f, indent=2)
        f.write("\n")

    return filepath


def read_concepts_from_dir(dir_path: str) -> list:
    """Read all concept JSON files from a directory."""
    p = Path(dir_path)
    if not p.exists():
        console.print(f"[red]Directory not found: {dir_path}[/red]")
        raise typer.Exit(1)

    concepts = []
    for f in sorted(p.glob("*.json")):
        try:
            with open(f) as fh:
                data = json.load(fh)
        except json.JSONDecodeError:
            console.print(f"[yellow]Skipping invalid JSON: {f}[/yellow]")
            continue
        if isinstance(data, dict):
            concepts.append(data)
        elif isinstance(data, list):
            concepts.extend(data)

    return concepts


def write_concepts_individual(concepts: list, dir_path: str):
    """Write each concept as an individual JSON file in a directory."""
    for concept in concepts:
        write_concept_individual(concept, dir_path)


def read_concepts_from_path(path: str) -> list:
    """Read concepts from a file or a directory, whichever `path` names."""
    if path.endswith("/") or Path(path).is_dir():
        return read_concepts_from_dir(path)
    return read_concepts_from_file(path)


def write_concepts_to_path(concepts: list, path: str) -> str:
    """Write concepts to a directory (one file each) or a single file.

    Returns a description of what was written, for the success message.
    """
    if path.endswith("/") or Path(path).is_dir():
        out_dir = path.rstrip("/")
        write_concepts_individual(concepts, out_dir)
        return f"{out_dir}/ ({len(concepts)} files)"
    write_concepts_to_file(concepts, path)
    return path


def load_strategy(strategy_path: Optional[str] = None) -> Strategy:
    """Load a strategy, or return the default.

    Strategy lookup order:
    1. If path contains / or starts with ., use as-is
    2. Check current directory for name.json
    3. Check ~/.config/ctools/strategies/name.json
    """
    if strategy_path:
        return Strategy.resolve(strategy_path)
    return DEFAULT_STRATEGY


# --- Session I/O ---

def session_concepts(agent: Agent, session_id: str, strategy: Optional[str],
                     filter_config: Optional[str]) -> list:
    """Read a session and return the concepts it yields, filtered."""
    with reporting():
        messages = agent.raw_messages(session_id)
    if not messages:
        console.print(f"[yellow]Session not found: {session_id}[/yellow]")
        raise typer.Exit(1)
    log.debug("messages_loaded", agent=agent.name, session=session_id, count=len(messages))

    if strategy:
        strat = load_strategy(strategy)
        concepts = strat.extract([{"role": m.role, "content": m.content} for m in messages])
    else:
        concepts = extract_concepts_from_messages(messages)

    log.info("concepts_extracted", source=f"{agent.name}/{session_id}", count=len(concepts))

    if filter_config:
        filter_path = Path(filter_config)
        if filter_path.exists():
            with open(filter_path) as f:
                filter_data = json.load(f)
            before = len(concepts)
            concepts = _filter_concepts(concepts, filter_data)
            log.info("filter_applied", config=filter_config, input_count=before,
                     output_count=len(concepts), dropped=before - len(concepts))

    return concepts


def inject_concepts(agent: Agent, session_id: str, concepts: list) -> None:
    """Inject concepts into a session as its system message."""
    with reporting():
        agent.inject_system(session_id, concepts_to_text(concepts))


def _dump_concepts(concepts: list, fmt: str) -> None:
    """Write concepts to stdout in the requested format."""
    if fmt in ("json", "default"):
        json.dump(concepts, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif fmt == "md":
        for c in concepts:
            console.print(f"**{c.get('type', '')}**: {c.get('description', '')}")
            console.print(f"> {concept_text(c)}\n")
    else:
        console.print(f"[red]Unknown format: {fmt}[/red]")
        raise typer.Exit(1)


@app.command()
def main(
    args: List[str] = typer.Argument(..., help="Sources and destinations (@ for sessions)"),
    fmt: str = typer.Option("default", "--format", "-f", help="Output format: json, xml, md"),
    strategy: Optional[str] = typer.Option(None, "--strategy", "-s", help="Strategy JSON file for LLM-based extraction"),
    filter_config: Optional[str] = typer.Option(None, "--filter", "-F", help="Filter JSON file"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """
    Copy concepts between sessions and concept directories.

    @ prefix denotes a session (agent/session_id).
    Plain paths are concept directories. Each concept becomes its own file.

    With no destination, concepts are dumped to stdout as JSON.

    Examples:
        ccopy @opencode/ses_abc
        ccopy @opencode/ses_abc concepts/
        ccopy concepts/ @opencode/ses_abc
        ccopy @opencode/ses_abc @claude/ses_xyz
        ccopy --strategy my-strategy.json @opencode/ses_abc concepts/
        ccopy --filter my-filter.json @opencode/ses_abc concepts/
    """
    configure_logging(verbose=verbose)
    sessions, files = parse_args(args)

    if not sessions:
        console.print("[red]No session references (use @ prefix)[/red]")
        raise typer.Exit(1)

    # Files -> session: the destination is the trailing @ref.
    if files and args[-1].startswith("@"):
        agent, session_id = require_session(sessions[-1])
        concepts = [c for f in files for c in read_concepts_from_path(f)]
        if not concepts:
            console.print("[yellow]No concepts found in files[/yellow]")
            return
        log.info("concepts_loaded", count=len(concepts), destination=f"{agent.name}/{session_id}")
        inject_concepts(agent, session_id, concepts)
        console.print(f"[green]Injected {len(concepts)} concepts into {agent.name}/{session_id}[/green]")
        return

    if not args[0].startswith("@"):
        console.print("[red]Ambiguous: mix of sessions and files[/red]")
        raise typer.Exit(1)

    # Everything else reads concepts out of the leading session.
    source, session_id = require_session(sessions[0])
    concepts = session_concepts(source, session_id, strategy, filter_config)
    if not concepts:
        console.print("[yellow]No concepts found in session[/yellow]")
        return

    if files:
        # Session -> concept file or directory.
        written = write_concepts_to_path(concepts, files[0])
        console.print(f"[green]Extracted {len(concepts)} concepts to {written}[/green]")
    elif len(sessions) > 1:
        # Session -> session(s).
        for dest_ref in sessions[1:]:
            dest, dest_id = require_session(dest_ref)
            inject_concepts(dest, dest_id, concepts)
            console.print(f"[green]Copied {len(concepts)} concepts to {dest.name}/{dest_id}[/green]")
    else:
        # No destination: dump to stdout.
        _dump_concepts(concepts, fmt)


if __name__ == "__main__":
    app()
