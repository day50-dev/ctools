"""
cconnect - Connect context windows via live concept pipelines.

Exposes concepts from one session as a toolcall in another session's context.
Polls the source session and re-injects concepts on each cycle.
Use --count 1 for a one-shot operation.

Supports one-to-many pipelines via --pipeline:
    cconnect --pipeline pipeline.json
"""

import json
import sys
import time
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console

from ctools.lib import AGENTS, Session, Agent, Message
from ctools.ccopy import (
    extract_concepts_from_messages,
    get_session_messages,
    resolve_session,
    load_strategy,
    read_concepts_from_dir,
)
from ctools.filterlib import load_filter

app = typer.Typer()
console = Console()

__all__ = ['app']


def _filter_concepts(concepts: list, filter_config: dict) -> list:
    """Filter concepts based on filter configuration."""
    if not filter_config:
        return concepts

    prompt = filter_config.get("prompt", "")
    types = filter_config.get("types", [])
    exclude_types = filter_config.get("exclude_types", [])

    filtered = []
    for c in concepts:
        ctype = c.get("type", "")

        if types and ctype not in types:
            continue
        if exclude_types and ctype in exclude_types:
            continue
        if prompt:
            description = c.get("description", "").lower()
            short = c.get("short", "").lower()
            if prompt.lower() not in description and prompt.lower() not in short:
                continue

        filtered.append(c)

    return filtered


def _inject_toolcall_sqlite(agent_info, session_id: str, source_agent: str,
                            source_session_id: str, concepts: list,
                            tool_name: str = "context_from_source"):
    """Inject a toolcall into a SQLite-backed session."""
    import sqlite3

    db_path = agent_info.base_path / "opencode.db"
    if not db_path.exists():
        console.print(f"[red]Database not found: {db_path}[/red]")
        raise typer.Exit(1)

    concept_lines = []
    for c in concepts:
        ctype = c.get("type", "preference")
        text = c.get("short") or c.get("medium") or c.get("description", "")
        concept_lines.append(f"- {ctype}: {text}")

    tool_content = f"Concepts from {source_agent}/{source_session_id}:\n" + "\n".join(concept_lines)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id FROM message WHERE session_id = ? AND data LIKE ?",
        (session_id, f'%"name": "{tool_name}"%'),
    )
    row = cursor.fetchone()

    now_ms = int(time.time() * 1000)

    if row:
        msg_id = row[0]
        data = json.dumps({"role": "tool", "name": tool_name, "content": tool_content})
        cursor.execute("UPDATE message SET data = ?, time_updated = ? WHERE id = ?", (data, now_ms, msg_id))
        cursor.execute("UPDATE part SET data = ? WHERE message_id = ?", (json.dumps({"type": "text", "text": tool_content}), msg_id))
    else:
        msg_id = f"cconnect_{now_ms}"
        data = json.dumps({"role": "tool", "name": tool_name, "content": tool_content})
        cursor.execute("INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)", (msg_id, session_id, now_ms, now_ms, data))
        cursor.execute("INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)", (f"part_{msg_id}", msg_id, session_id, now_ms, now_ms, json.dumps({"type": "text", "text": tool_content})))

    conn.commit()
    conn.close()


def _inject_toolcall_jsonl(agent_info, session_id: str, source_agent: str,
                           source_session_id: str, concepts: list,
                           tool_name: str = "context_from_source"):
    """Inject a toolcall into a JSONL-backed session."""
    concept_lines = []
    for c in concepts:
        ctype = c.get("type", "preference")
        text = c.get("short") or c.get("medium") or c.get("description", "")
        concept_lines.append(f"- {ctype}: {text}")

    tool_content = f"Concepts from {source_agent}/{source_session_id}:\n" + "\n".join(concept_lines)

    for session_file in agent_info.base_path.glob(agent_info.session_pattern):
        if session_file.stem == session_id:
            lines = session_file.read_text().splitlines()
            replaced = False
            new_lines = []
            for line in lines:
                try:
                    data = json.loads(line)
                    if data.get("name") == tool_name:
                        data["content"] = tool_content
                        new_lines.append(json.dumps(data))
                        replaced = True
                    else:
                        new_lines.append(line)
                except json.JSONDecodeError:
                    new_lines.append(line)

            if not replaced:
                new_lines.append(json.dumps({"role": "tool", "name": tool_name, "content": tool_content}))

            session_file.write_text("\n".join(new_lines) + "\n")
            return

    console.print(f"[yellow]Session file not found for {session_id}[/yellow]")


def _extract_concepts(source: str, strategy: Optional[str]) -> Optional[list]:
    """Extract concepts from a source. Returns None on error."""
    source_clean = source.lstrip("@")
    source_parts = source_clean.split("/", 2)
    source_agent = source_parts[0]
    source_session_id = source_parts[1] if len(source_parts) > 1 else None
    source_directory = source_parts[2] if len(source_parts) > 2 else None

    if not source_session_id:
        console.print("[red]Source must include session_id: @agent/session_id[/red]")
        return None

    if source_directory:
        source_path = Path(source_directory)
        if not source_path.exists():
            console.print(f"[red]Source directory not found: {source_path}[/red]")
            return None
        concepts = read_concepts_from_dir(str(source_path))
    else:
        messages = get_session_messages(source_agent, source_session_id)
        if strategy:
            strat = load_strategy(strategy)
            concepts = strat.extract([{"role": m.role, "content": m.content} for m in messages])
        else:
            concepts = extract_concepts_from_messages(messages)

    return concepts


def _inject_to_dest(destination: str, concepts: list, source: str,
                    tool_name: str) -> bool:
    """Inject concepts into a destination. Returns True on success."""
    dest_clean = destination.lstrip("@")
    dest_parts = dest_clean.split("/")
    dest_agent = dest_parts[0]
    dest_session_id = dest_parts[1] if len(dest_parts) > 1 else None

    if not dest_session_id:
        console.print(f"[red]Destination must include session_id: {destination}[/red]")
        return False

    source_clean = source.lstrip("@")
    source_parts = source_clean.split("/")
    source_agent = source_parts[0]
    source_session_id = source_parts[1] if len(source_parts) > 1 else "unknown"

    dest_agent_info = AGENTS.get(dest_agent)
    if not dest_agent_info or not dest_agent_info.base_path.exists():
        console.print(f"[red]Destination agent {dest_agent} not found[/red]")
        return False

    if dest_agent_info.storage_format == "sqlite":
        _inject_toolcall_sqlite(dest_agent_info, dest_session_id,
                                source_agent, source_session_id,
                                concepts, tool_name)
    elif dest_agent_info.storage_format == "jsonl":
        _inject_toolcall_jsonl(dest_agent_info, dest_session_id,
                               source_agent, source_session_id,
                               concepts, tool_name)
    else:
        console.print(f"[red]Unsupported format: {dest_agent_info.storage_format}[/red]")
        return False

    return True


def _run_cycle(source: str, destination: str, strategy: Optional[str],
               filter_config: Optional[str], tool_name: str) -> bool:
    """Run one extract-filter-inject cycle. Returns True on success."""
    concepts = _extract_concepts(source, strategy)
    if concepts is None:
        return False

    if not concepts:
        console.print("[yellow]No concepts found in source[/yellow]")
        return False

    if filter_config:
        filter_path = Path(filter_config)
        if filter_path.exists():
            with open(filter_path, 'r') as f:
                filter_data = json.load(f)
            concepts = _filter_concepts(concepts, filter_data)

    if not concepts:
        console.print("[yellow]No concepts after filtering[/yellow]")
        return False

    if _inject_to_dest(destination, concepts, source, tool_name):
        console.print(f"[green]Injected {len(concepts)} concepts as toolcall '{tool_name}'[/green]")
        return True
    return False


def _run_pipeline_cycle(pipeline: dict) -> bool:
    """Run one cycle of a multi-destination pipeline. Returns True on success."""
    source = pipeline["source"]
    strategy = pipeline.get("strategy")
    tool_name = pipeline.get("tool_name", "context_from_source")
    destinations = pipeline.get("destinations", [])

    concepts = _extract_concepts(source, strategy)
    if concepts is None:
        return False

    if not concepts:
        console.print("[yellow]No concepts found in source[/yellow]")
        return False

    injected = 0
    for dest in destinations:
        session = dest["session"]
        dest_concepts = list(concepts)

        dest_filter = dest.get("filter")
        if dest_filter:
            filter_path = Path(dest_filter)
            if filter_path.exists():
                with open(filter_path, 'r') as f:
                    filter_data = json.load(f)
                dest_concepts = _filter_concepts(dest_concepts, filter_data)

        dest_tool_name = dest.get("tool_name", tool_name)

        if dest_concepts:
            if _inject_to_dest(session, dest_concepts, source, dest_tool_name):
                console.print(f"  [green]{session}: {len(dest_concepts)} concepts[/green]")
                injected += 1
        else:
            console.print(f"  [yellow]{session}: no concepts after filtering[/yellow]")

    return injected > 0


@app.command()
def main(
    source: Optional[str] = typer.Argument(None, help="Source session (@agent/session_id)"),
    destination: Optional[str] = typer.Argument(None, help="Destination session (@agent/session_id)"),
    strategy: Optional[str] = typer.Option(None, "--strategy", "-s", help="Strategy JSON file for extraction"),
    filter_config: Optional[str] = typer.Option(None, "--filter", "-f", help="Filter JSON file"),
    tool_name: str = typer.Option("context_from_source", "--tool-name", "-t", help="Name for the toolcall"),
    count: int = typer.Option(0, "--count", "-c", help="Number of cycles (0=infinity)"),
    poll_interval: float = typer.Option(5.0, "--poll-interval", "-p", help="Poll interval in seconds"),
    pipeline: Optional[str] = typer.Option(None, "--pipeline", "-P", help="Pipeline JSON config for one-to-many"),
):
    """
    Connect context windows via live concept pipelines.

    Exposes concepts from source session as a toolcall in destination session's context.
    Polls the source and re-injects concepts on each cycle.

    Simple (one-to-one):
        cconnect @opencode/ses_abc @claude-code/ses_xyz
        cconnect -c 1 @opencode/ses_abc @claude-code/ses_xyz

    Pipeline (one-to-many):
        cconnect --pipeline pipeline.json
    """
    if pipeline:
        pipeline_path = Path(pipeline)
        if not pipeline_path.exists():
            console.print(f"[red]Pipeline config not found: {pipeline}[/red]")
            raise typer.Exit(1)
        with open(pipeline_path) as f:
            config = json.load(f)

        count = config.get("count", count)
        poll_interval = config.get("poll_interval", poll_interval)

        cycle = 0
        try:
            while True:
                cycle += 1
                _run_pipeline_cycle(config)

                if count != 0 and cycle >= count:
                    break

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            pass
    else:
        if not source or not destination:
            console.print("[red]Source and destination required (or use --pipeline)[/red]")
            raise typer.Exit(1)

        cycle = 0
        try:
            while True:
                cycle += 1
                _run_cycle(source, destination, strategy, filter_config, tool_name)

                if count != 0 and cycle >= count:
                    break

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    app()
