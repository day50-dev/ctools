"""
cconnect - Connect context windows via live concept pipelines.

Exposes concepts from one session as a toolcall in another session's context.
Polls the source session and re-injects concepts on each cycle.
Use --count 1 for a one-shot operation.

Supports one-to-many pipelines via --pipeline:
    cconnect --pipeline pipeline.json
"""

import json
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from ctools.agents import AgentError, REGISTRY as AGENTS
from ctools.ccopy import (
    _filter_concepts,
    concept_text,
    extract_concepts_from_messages,
    load_strategy,
    read_concepts_from_dir,
)
from ctools.cli import parse_ref
from ctools.log import configure_logging, get_logger

app = typer.Typer()
console = Console()
log = get_logger()

__all__ = ['app']


def _apply_filter(concepts: list, filter_config: Optional[str], **log_fields) -> list:
    """Apply a filter JSON config, if one is configured and present."""
    if not filter_config:
        return concepts
    filter_path = Path(filter_config)
    if not filter_path.exists():
        return concepts
    with open(filter_path) as f:
        filter_data = json.load(f)
    before = len(concepts)
    concepts = _filter_concepts(concepts, filter_data)
    log.info("filter_applied", config=filter_config, input_count=before,
             output_count=len(concepts), dropped=before - len(concepts), **log_fields)
    return concepts


def _toolcall_text(concepts: list, source_agent: str, source_session_id: str) -> str:
    """Render concepts as the body of the synthetic tool message."""
    lines = "\n".join(f"- {c.get('type', 'preference')}: {concept_text(c)}" for c in concepts)
    return f"Concepts from {source_agent}/{source_session_id}:\n{lines}"


def _extract_concepts(source: str, strategy: Optional[str]) -> Optional[list]:
    """Extract concepts from a source. Returns None on error.

    A source is ``@agent/session_id`` or ``@agent/session_id/directory``;
    the third part reads pre-extracted concepts off disk instead.
    """
    source_agent, _, remainder = source.lstrip("@").partition("/")
    source_session_id, _, source_directory = remainder.partition("/")

    if not source_session_id:
        log.error("invalid_source", source=source, reason="missing session_id")
        return None

    t0 = time.monotonic()

    if source_directory:
        if not Path(source_directory).exists():
            log.error("source_not_found", path=source_directory)
            return None
        concepts = read_concepts_from_dir(source_directory)
    else:
        agent = AGENTS.get(source_agent)
        if agent is None or not agent.exists():
            log.error("source_agent_not_found", agent=source_agent)
            return None
        try:
            messages = agent.raw_messages(source_session_id)
        except AgentError as exc:
            log.error("source_read_failed", source=source, error=str(exc))
            return None
        if strategy:
            strat = load_strategy(strategy)
            concepts = strat.extract([{"role": m.role, "content": m.content} for m in messages])
        else:
            concepts = extract_concepts_from_messages(messages)

    types = {}
    for c in concepts:
        t = c.get("type", "unknown")
        types[t] = types.get(t, 0) + 1

    log.info("concepts_extracted", source=source, count=len(concepts), types=types,
             elapsed_ms=round((time.monotonic() - t0) * 1000))
    return concepts


def _inject_to_dest(destination: str, concepts: list, source: str,
                    tool_name: str) -> bool:
    """Inject concepts into a destination. Returns True on success."""
    dest_agent_name, dest_session_id = parse_ref(destination)
    if not dest_session_id:
        log.error("invalid_destination", destination=destination, reason="missing session_id")
        return False

    source_agent, source_session_id = parse_ref(source)
    source_session_id = source_session_id or "unknown"

    agent = AGENTS.get(dest_agent_name)
    if agent is None or not agent.exists():
        log.error("destination_agent_not_found", agent=dest_agent_name)
        return False

    t0 = time.monotonic()
    content = _toolcall_text(concepts, source_agent, source_session_id)
    try:
        agent.inject_toolcall(dest_session_id, content, tool_name)
    except AgentError as exc:
        log.error("inject_failed", destination=destination, error=str(exc))
        return False

    log.info("inject_complete", destination=destination, count=len(concepts),
             elapsed_ms=round((time.monotonic() - t0) * 1000))
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

    concepts = _apply_filter(concepts, filter_config)

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

        dest_concepts = _apply_filter(dest_concepts, dest.get("filter"),
                                      destination=session)

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
