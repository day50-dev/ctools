"""
crm - scalpel remove concepts from sessions.

Surgically removes concept-containing sections from agent sessions.
Concept JSON files are NOT deleted - only the relevant sections from the context.
"""

import json
import sys
import time
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.prompt import Confirm

from ctools.lib import AGENTS, Session, Agent, Message
from ctools.ccopy import (
    get_session_messages,
    resolve_session,
    load_strategy,
    read_concepts_from_file,
)

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
    concept_text = concept.get("short", "").lower()
    if not concept_text:
        concept_text = concept.get("medium", "").lower()
    if not concept_text:
        concept_text = concept.get("description", "").lower()

    if not concept_text:
        return False

    return concept_text in message_content.lower()


def _concept_in_range(concept: dict, messages: List[Message], start: int, end: int, strategy=None) -> bool:
    """Check if a range of messages contains a concept."""
    if strategy:
        # For strategy-based detection, check the combined text
        combined_text = " ".join(m.content for m in messages[start:end + 1])
        return _detect_with_strategy(strategy, concept, combined_text)
    else:
        # For string matching, check if concept text appears anywhere
        concept_text = concept.get("short", "").lower()
        if not concept_text:
            concept_text = concept.get("medium", "").lower()
        if not concept_text:
            concept_text = concept.get("description", "").lower()

        if not concept_text:
            return False

        range_text = " ".join(m.content.lower() for m in messages[start:end + 1])
        return concept_text in range_text


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


def _remove_messages_sqlite(agent_info, session_id: str, message_indices: List[int],
                           messages: List[Message], verbose: bool = False):
    """Remove messages from a SQLite-backed session."""
    import sqlite3

    db_path = agent_info.base_path / "opencode.db"
    if not db_path.exists():
        console.print(f"[red]Database not found: {db_path}[/red]")
        raise typer.Exit(1)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Get message IDs for the indices we want to remove
    cursor.execute('''
        SELECT m.id
        FROM message m
        WHERE m.session_id = ?
        ORDER BY m.time_created
    ''', (session_id,))

    all_msg_ids = [row[0] for row in cursor.fetchall()]

    if not all_msg_ids:
        console.print(f"[yellow]No messages found for session {session_id}[/yellow]")
        conn.close()
        return

    # Delete messages at the specified indices
    for idx in message_indices:
        if idx < len(all_msg_ids):
            msg_id = all_msg_ids[idx]
            if verbose:
                console.print(f"  [red]Deleting message {idx} (id: {msg_id})[/red]")

            # Delete parts first
            cursor.execute("DELETE FROM part WHERE message_id = ?", (msg_id,))
            # Delete message
            cursor.execute("DELETE FROM message WHERE id = ?", (msg_id,))

    conn.commit()
    conn.close()


def _remove_messages_jsonl(agent_info, session_id: str, message_indices: List[int],
                          messages: List[Message], verbose: bool = False):
    """Remove messages from a JSONL-backed session."""
    for session_file in agent_info.base_path.glob(agent_info.session_pattern):
        if session_file.stem == session_id:
            lines = session_file.read_text().splitlines()
            new_lines = []
            removed_count = 0

            for i, line in enumerate(lines):
                if i in message_indices:
                    removed_count += 1
                    if verbose:
                        console.print(f"  [red]Removing line {i}[/red]")
                    continue
                new_lines.append(line)

            session_file.write_text("\n".join(new_lines) + "\n")
            return

    console.print(f"[yellow]Session file not found for {session_id}[/yellow]")


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
    # Parse session reference
    session_clean = session.lstrip("@")
    parts = session_clean.split("/")
    agent_name = parts[0]
    session_id = parts[1] if len(parts) > 1 else None

    if not session_id:
        console.print("[red]Session must include session_id: @agent/session_id[/red]")
        raise typer.Exit(1)

    # Get messages from session
    messages = get_session_messages(agent_name, session_id)

    if verbose:
        console.print(f"[dim]Loaded {len(messages)} messages from {agent_name}/{session_id}[/dim]")

    # Load all concepts
    all_concepts = []
    for concept_path in concepts:
        path = Path(concept_path)
        if not path.exists():
            console.print(f"[red]Concept file not found: {concept_path}[/red]")
            raise typer.Exit(1)
        all_concepts.extend(read_concepts_from_file(str(path)))

    if not all_concepts:
        console.print("[yellow]No concepts found in files[/yellow]")
        return

    if verbose:
        console.print(f"[dim]Loaded {len(all_concepts)} concepts[/dim]")

    # Load strategy if provided
    strat = None
    if strategy:
        strat = load_strategy(strategy)
        if verbose:
            console.print(f"[dim]Using strategy for detection[/dim]")

    # Find messages to remove for each concept
    all_indices_to_remove = set()

    for concept in all_concepts:
        if algo == "divide":
            indices = _divide_and_conquer(messages, concept, strat, verbose)
        elif algo == "sliding":
            indices = _sliding_window(messages, concept, size, strat, verbose)
        else:
            console.print(f"[red]Unknown algorithm: {algo}[/red]")
            raise typer.Exit(1)

        all_indices_to_remove.update(indices)

    if not all_indices_to_remove:
        console.print("[yellow]No matching sections found to remove[/yellow]")
        return

    # Interactive mode
    if interactive:
        console.print(f"\n[yellow]Will remove {len(all_indices_to_remove)} message(s):[/yellow]")
        for idx in sorted(all_indices_to_remove):
            msg = messages[idx]
            preview = msg.content[:100].replace("\n", " ")
            console.print(f"  {idx}: [{msg.role}] {preview}...")

        if not Confirm.ask("\nProceed with removal?"):
            console.print("[dim]Aborted[/dim]")
            return

    # Remove messages
    agent_info = AGENTS.get(agent_name)
    if not agent_info or not agent_info.base_path.exists():
        console.print(f"[red]Agent {agent_name} not found[/red]")
        raise typer.Exit(1)

    if agent_info.storage_format == "sqlite":
        _remove_messages_sqlite(agent_info, session_id, sorted(all_indices_to_remove),
                               messages, verbose)
    elif agent_info.storage_format == "jsonl":
        _remove_messages_jsonl(agent_info, session_id, sorted(all_indices_to_remove),
                              messages, verbose)
    else:
        console.print(f"[red]Unsupported format: {agent_info.storage_format}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Removed {len(all_indices_to_remove)} message(s) from {agent_name}/{session_id}[/green]")


if __name__ == "__main__":
    app()
