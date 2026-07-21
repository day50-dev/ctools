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
from ctools.log import configure_logging, get_logger

app = typer.Typer()
console = Console()
log = get_logger()

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
            log.debug("concept_filtered", reason="type_not_included", type=ctype, short=c.get("short", "")[:60])
            continue
        if exclude_types and ctype in exclude_types:
            log.debug("concept_filtered", reason="type_excluded", type=ctype, short=c.get("short", "")[:60])
            continue
        if prompt:
            description = c.get("description", "").lower()
            short = c.get("short", "").lower()
            if prompt.lower() not in description and prompt.lower() not in short:
                log.debug("concept_filtered", reason="prompt_no_match", prompt=prompt, short=c.get("short", "")[:60])
                continue

        log.debug("concept_passed", type=ctype, short=c.get("short", "")[:60])
        filtered.append(c)

    return filtered


def _inject_toolcall_sqlite(agent_info, session_id: str, source_agent: str,
                            source_session_id: str, concepts: list,
                            tool_name: str = "context_from_source"):
    """Inject a toolcall into a SQLite-backed session."""
    import sqlite3

    db_path = agent_info.base_path / "opencode.db"
    if not db_path.exists():
        log.error("database_not_found", path=str(db_path))
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
        log.debug("toolcall_updated", session=session_id, msg_id=msg_id)
    else:
        msg_id = f"cconnect_{now_ms}"
        data = json.dumps({"role": "tool", "name": tool_name, "content": tool_content})
        cursor.execute("INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)", (msg_id, session_id, now_ms, now_ms, data))
        cursor.execute("INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)", (f"part_{msg_id}", msg_id, session_id, now_ms, now_ms, json.dumps({"type": "text", "text": tool_content})))
        log.debug("toolcall_inserted", session=session_id, msg_id=msg_id)

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
            log.debug("toolcall_written", session=session_id, file=str(session_file))
            return

    log.warning("session_file_not_found", session=session_id)


def _extract_concepts(source: str, strategy: Optional[str]) -> Optional[list]:
    """Extract concepts from a source. Returns None on error."""
    source_clean = source.lstrip("@")
    source_parts = source_clean.split("/", 2)
    source_agent = source_parts[0]
    source_session_id = source_parts[1] if len(source_parts) > 1 else None
    source_directory = source_parts[2] if len(source_parts) > 2 else None

    if not source_session_id:
        log.error("invalid_source", source=source, reason="missing session_id")
        return None

    t0 = time.monotonic()

    if source_directory:
        source_path = Path(source_directory)
        if not source_path.exists():
            log.error("source_not_found", path=str(source_path))
            return None
        concepts = read_concepts_from_dir(str(source_path))
    else:
        messages = get_session_messages(source_agent, source_session_id)
        if strategy:
            strat = load_strategy(strategy)
            concepts = strat.extract([{"role": m.role, "content": m.content} for m in messages])
        else:
            concepts = extract_concepts_from_messages(messages)

    elapsed = time.monotonic() - t0
    types = {}
    for c in concepts:
        t = c.get("type", "unknown")
        types[t] = types.get(t, 0) + 1

    log.info("concepts_extracted", source=source, count=len(concepts), types=types, elapsed_ms=round(elapsed * 1000))
    return concepts


def _inject_to_dest(destination: str, concepts: list, source: str,
                    tool_name: str) -> bool:
    """Inject concepts into a destination. Returns True on success."""
    dest_clean = destination.lstrip("@")
    dest_parts = dest_clean.split("/")
    dest_agent = dest_parts[0]
    dest_session_id = dest_parts[1] if len(dest_parts) > 1 else None

    if not dest_session_id:
        log.error("invalid_destination", destination=destination, reason="missing session_id")
        return False

    source_clean = source.lstrip("@")
    source_parts = source_clean.split("/")
    source_agent = source_parts[0]
    source_session_id = source_parts[1] if len(source_parts) > 1 else "unknown"

    dest_agent_info = AGENTS.get(dest_agent)
    if not dest_agent_info or not dest_agent_info.base_path.exists():
        log.error("destination_agent_not_found", agent=dest_agent)
        return False

    t0 = time.monotonic()

    if dest_agent_info.storage_format == "sqlite":
        _inject_toolcall_sqlite(dest_agent_info, dest_session_id,
                                source_agent, source_session_id,
                                concepts, tool_name)
    elif dest_agent_info.storage_format == "jsonl":
        _inject_toolcall_jsonl(dest_agent_info, dest_session_id,
                               source_agent, source_session_id,
                               concepts, tool_name)
    else:
        log.error("unsupported_format", agent=dest_agent, format=dest_agent_info.storage_format)
        return False

    elapsed = time.monotonic() - t0
    log.info("inject_complete", destination=destination, count=len(concepts), elapsed_ms=round(elapsed * 1000))
    return True


def _run_cycle(source: str, destination: str, strategy: Optional[str],
               filter_config: Optional[str], tool_name: str) -> bool:
    """Run one extract-filter-inject cycle. Returns True on success."""
    concepts = _extract_concepts(source, strategy)
    if concepts is None:
        return False

    if not concepts:
        log.warning("no_concepts", source=source)
        return False

    if filter_config:
        filter_path = Path(filter_config)
        if filter_path.exists():
            with open(filter_path, 'r') as f:
                filter_data = json.load(f)
            before = len(concepts)
            concepts = _filter_concepts(concepts, filter_data)
            log.info("filter_applied", config=filter_config, input_count=before, output_count=len(concepts), dropped=before - len(concepts))

    if not concepts:
        log.warning("all_concepts_filtered", source=source, destination=destination)
        return False

    if _inject_to_dest(destination, concepts, source, tool_name):
        log.info("cycle_complete", source=source, destination=destination, injected=len(concepts))
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
        log.warning("no_concepts", source=source)
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
                before = len(dest_concepts)
                dest_concepts = _filter_concepts(dest_concepts, filter_data)
                log.info("filter_applied", destination=session, config=dest_filter, input_count=before, output_count=len(dest_concepts), dropped=before - len(dest_concepts))

        dest_tool_name = dest.get("tool_name", tool_name)

        if dest_concepts:
            if _inject_to_dest(session, dest_concepts, source, dest_tool_name):
                injected += 1
        else:
            log.warning("all_concepts_filtered", destination=session)

    log.info("pipeline_cycle_complete", source=source, destinations=len(destinations), injected=injected)
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
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
    configure_logging(verbose=verbose)

    if pipeline:
        pipeline_path = Path(pipeline)
        if not pipeline_path.exists():
            log.error("pipeline_not_found", path=pipeline)
            raise typer.Exit(1)
        with open(pipeline_path) as f:
            config = json.load(f)

        count = config.get("count", count)
        poll_interval = config.get("poll_interval", poll_interval)

        log.info("pipeline_started", source=config["source"], destinations=len(config.get("destinations", [])), count=count, poll_interval=poll_interval)

        cycle = 0
        try:
            while True:
                cycle += 1
                log.debug("cycle_start", cycle=cycle)
                _run_pipeline_cycle(config)

                if count != 0 and cycle >= count:
                    break

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            log.info("interrupted", cycle=cycle)
    else:
        if not source or not destination:
            console.print("[red]Source and destination required (or use --pipeline)[/red]")
            raise typer.Exit(1)

        log.info("connect_started", source=source, destination=destination, count=count, poll_interval=poll_interval)

        cycle = 0
        try:
            while True:
                cycle += 1
                log.debug("cycle_start", cycle=cycle)
                _run_cycle(source, destination, strategy, filter_config, tool_name)

                if count != 0 and cycle >= count:
                    break

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            log.info("interrupted", cycle=cycle)


if __name__ == "__main__":
    app()
