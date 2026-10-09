#!/usr/bin/env python3
"""
cextract - extract concepts from sessions into files, and inject them back

Concepts are constraints, goals, preferences, observations, and references
that can be extracted from agent sessions and stored in JSON files. The @ prefix
marks a session (agent/session_id); anything else is a concept file path.

Usage:
    cextract @opencode/ses_abc                          # dump concepts to stdout (for testing)
    cextract @opencode/ses_abc concepts/                # extract to directory (one file per concept)
    cextract @opencode/ses_abc concepts.json            # extract to single file
    cextract concepts/ @opencode/ses_abc                # inject all concepts from directory
    cextract constraints.json @opencode/ses_abc         # inject from file
    cextract @opencode/ses_abc @claude/ses_xyz          # copy concepts between sessions
    cextract --strategy my-strategy.json @opencode/ses_abc concepts/  # use custom extraction strategy
"""

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import argparse
from rich.console import Console

from ctools.agents import Agent, Message
from ctools.cli import reporting, require_session, version_option
from ctools.filterlib import SystemOneFilter
from ctools.log import configure_logging, get_logger
from ctools.strategy import Strategy, DEFAULT_STRATEGY

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


def _is_decision_filter(filter_config: dict) -> bool:
    """True when the filter config names a decision model (not a JSON-RPC command)."""
    if not isinstance(filter_config, dict):
        return False
    if filter_config.get("type") == "systemone":
        return True
    if filter_config.get("type") == "jsonrpc":
        return False
    return bool(filter_config.get("model")) and not filter_config.get("command")


def _decision_filter(filter_config: dict):
    """Build a SystemOneFilter from a decision-model filter config, or None."""
    if not _is_decision_filter(filter_config):
        return None
    if not filter_config.get("model") or not filter_config.get("instructions"):
        log.warning("decision_filter_incomplete", config=filter_config,
                    reason="needs 'model' and 'instructions'")
        return None
    return SystemOneFilter(
        host=filter_config.get("host", "http://localhost:11434"),
        model=filter_config["model"],
        instructions=filter_config["instructions"],
        endpoint=filter_config.get("endpoint", "systemone"),
        question_name=filter_config.get("question_name", "relevant"),
        mode=filter_config.get("mode", "noul"),
        threshold=filter_config.get("threshold", 0.6),
        allowed=filter_config.get("allowed"),
        criteria=filter_config.get("criteria"),
        max_tokens=filter_config.get("max_tokens", 2048),
        api_key=filter_config.get("api_key"),
        timeout=filter_config.get("timeout", 30.0),
    )


def _filter_concepts(concepts: list, filter_config: dict) -> list:
    """Filter concepts based on filter configuration.

    A decision-model config (one with a ``model`` and no ``command``) routes
    each concept through a local decision model. Anything else uses the
    built-in type/prompt filter.
    """
    if not filter_config:
        return concepts

    if _is_decision_filter(filter_config):
        decision = _decision_filter(filter_config)
        if decision is None:
            return concepts
        kept = []
        for c in concepts:
            text = concept_text(c)
            if decision.filter(text):
                kept.append(c)
        return kept

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
    """Split arguments into session refs (@agent/id) and file paths.

    A leading ``@`` marks a session; anything else is a concept file path.
    (To copy a whole conversation into a new agent session, use ``ccopy``.)
    """
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
        raise SystemExit(1)
    with open(p) as f:
        data = json.load(f)
    if not isinstance(data, list):
        console.print(f"[red]Expected JSON array in {path}[/red]")
        raise SystemExit(1)
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
        raise SystemExit(1)

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
        raise SystemExit(1)
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
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cextract",
        description=("Extract concepts from sessions into files, and inject "
                     "them back.\n\n"
                     "A leading @ marks a session (agent/session_id); anything "
                     "else is a concept file path. With no destination, "
                     "extracted concepts dump to stdout as JSON. To copy a whole "
                     "CONVERSATION from one agent to a new session in another, "
                     "use `ccopy`."),
    )
    parser.add_argument("args", nargs="+",
                        help="Sources and destinations (@ for sessions)")
    parser.add_argument("-f", "--format", dest="fmt", default="default",
                        help="Output format: json, xml, md")
    parser.add_argument("-s", "--strategy",
                        help="Strategy JSON file for LLM-based extraction")
    parser.add_argument("-F", "--filter", dest="filter_config",
                        help="Filter JSON file (built-in, JSON-RPC, or decision model)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output")
    version_option(parser, "cextract")
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # --version wins over the required `args` positional (GNU style): the flag is
    # scanned before parsing so `cextract --version` works without a session.
    if "--version" in argv:
        from ctools import __version__
        print(f"cextract (ctxttools) {__version__}")
        return 0
    ns = build_parser().parse_args(argv)

    args = ns.args
    fmt = ns.fmt
    strategy = ns.strategy
    filter_config = ns.filter_config
    verbose = ns.verbose

    configure_logging(verbose=verbose)
    sessions, files = parse_args(args)

    if not sessions:
        console.print("[red]No session references (use @ prefix)[/red]")
        return 1

    # Files -> session: the destination is the trailing @ref.
    if files and args[-1].startswith("@"):
        agent, session_id = require_session(sessions[-1])
        concepts = [c for f in files for c in read_concepts_from_path(f)]
        if not concepts:
            console.print("[yellow]No concepts found in files[/yellow]")
            return 0
        log.info("concepts_loaded", count=len(concepts), destination=f"{agent.name}/{session_id}")
        inject_concepts(agent, session_id, concepts)
        console.print(f"[green]Injected {len(concepts)} concepts into {agent.name}/{session_id}[/green]")
        return 0

    if not args[0].startswith("@"):
        console.print("[red]Ambiguous: mix of sessions and files[/red]")
        return 1

    # Everything else reads concepts out of the leading session.
    source, session_id = require_session(sessions[0])
    concepts = session_concepts(source, session_id, strategy, filter_config)
    if not concepts:
        console.print("[yellow]No concepts found in session[/yellow]")
        return 0

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
    return 0


app = main


if __name__ == "__main__":
    raise SystemExit(main())
