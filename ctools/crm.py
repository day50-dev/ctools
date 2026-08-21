"""
crm - scalpel remove concepts from sessions.

Surgically removes concept-containing sections from agent sessions.
Concept JSON files are NOT deleted - only the relevant sections from the context.
"""

from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.prompt import Confirm

from ctools.agents import Message
from ctools.ccopy import concept_text, load_strategy, read_concepts_from_file
from ctools.cli import reporting, require_session

app = typer.Typer()
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
            return

        # Check if this range contains the concept
        if not _concept_in_range(concept, messages, start, end, strategy):
            return

        # If range is small enough, mark for removal
        if end - start <= 1:
            for i in range(start, end + 1):
                if _concept_matches_concept(concept, messages[i].content, strategy):
                    indices_to_remove.add(i)
                    if verbose:
                        console.print(f"  [yellow]Marking message {i} for removal[/yellow]")
            return

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


@app.command()
def main(
    session: str = typer.Argument(..., help="Session to remove from (@agent/session_id)"),
    concepts: List[str] = typer.Argument(..., help="Concept JSON files to remove"),
    algo: str = typer.Option("divide", "--algo", "-a", help="Algorithm: divide, sliding"),
    size: int = typer.Option(5, "--size", help="Window size for sliding algorithm"),
    strategy: Optional[str] = typer.Option(None, "--strategy", "-s", help="Strategy JSON file for detection"),
    interactive: bool = typer.Option(False, "-i", "--interactive", help="Confirm each removal"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose output"),
):
    """
    Scalpel remove concepts from sessions.

    Surgically removes concept-containing sections from agent sessions.
    Concept JSON files are NOT deleted - only the relevant sections from the context.

    Examples:
        crm @opencode/ses_abc concept.json
        crm @opencode/ses_abc concept1.json concept2.json
        crm -a sliding --size 3 @opencode/ses_abc concept.json
        crm -s my-strategy.json @opencode/ses_abc concept.json
        crm -i -v @opencode/ses_abc concept.json
    """
    agent, session_id = require_session(session)

    with reporting():
        messages = agent.raw_messages(session_id)

    if not messages:
        console.print(f"[yellow]Session not found: {session_id}[/yellow]")
        raise typer.Exit(1)

    if verbose:
        console.print(f"[dim]Loaded {len(messages)} messages from {agent.name}/{session_id}[/dim]")

    # Load all concepts
    all_concepts = []
    for concept_path in concepts:
        if not Path(concept_path).exists():
            console.print(f"[red]Concept file not found: {concept_path}[/red]")
            raise typer.Exit(1)
        all_concepts.extend(read_concepts_from_file(concept_path))

    if not all_concepts:
        console.print("[yellow]No concepts found in files[/yellow]")
        return

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
        raise typer.Exit(1)
    search = SEARCHES[algo]

    # Find messages to remove for each concept
    doomed = set()
    for concept in all_concepts:
        doomed.update(search(messages, concept, size, strat, verbose))

    if not doomed:
        console.print("[yellow]No matching sections found to remove[/yellow]")
        return

    if interactive:
        console.print(f"\n[yellow]Will remove {len(doomed)} message(s):[/yellow]")
        for idx in sorted(doomed):
            preview = messages[idx].content[:100].replace("\n", " ")
            console.print(f"  {idx}: [{messages[idx].role}] {preview}...")

        if not Confirm.ask("\nProceed with removal?"):
            console.print("[dim]Aborted[/dim]")
            return

    with reporting():
        removed = agent.remove_messages(session_id, sorted(doomed))

    console.print(f"[green]Removed {removed} message(s) from {agent.name}/{session_id}[/green]")


if __name__ == "__main__":
    app()
