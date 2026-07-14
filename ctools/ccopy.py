#!/usr/bin/env python3
"""
ccopy - copy concepts between sessions and concept files

Concepts are constraints, goals, preferences, observations, and references
that can be extracted from agent sessions and stored in JSON files.

Usage:
    ccopy @opencode/ses_abc concepts/              # extract to directory (one file per concept)
    ccopy @opencode/ses_abc concepts.json           # extract to single file
    ccopy concepts/ @opencode/ses_abc               # inject all concepts from directory
    ccopy constraints.json @opencode/ses_abc        # inject from file
    ccopy @opencode/ses_abc @claude/ses_xyz         # copy concepts between sessions
    ccopy --strategy my-strategy.json @opencode/ses_abc concepts/  # use custom extraction strategy
"""

import json
import sys
import re
import typer
import hashlib
from pathlib import Path
from typing import List, Optional, Tuple
from rich.console import Console

from ctools.lib import AGENTS, Message, get_formatter
from ctools.strategy import Strategy, DEFAULT_STRATEGY

app = typer.Typer()
console = Console()

CONCEPT_TYPES = ("constraint", "goal", "preference", "observation", "reference")

CONCEPT_PATTERN = re.compile(
    r"Use the following (constraint|goal|preference|observation|reference):\s*(.*)",
    re.IGNORECASE,
)


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
            line = line.strip()
            m = CONCEPT_PATTERN.match(line)
            if m:
                ctype = m.group(1).lower()
                text = m.group(2).strip()
                key = (ctype, text)
                if key not in seen:
                    seen.add(key)
                    concepts.append({
                        "type": ctype,
                        "description": text[:50],
                        "short": text[:250],
                        "medium": text[:1000],
                        "long": text[:2500],
                    })
    return concepts


def concepts_to_messages(concepts: list) -> List[Message]:
    """Convert concept objects to a system message with concept lines."""
    lines = []
    for c in concepts:
        ctype = c.get("type", "preference")
        text = c.get("short") or c.get("medium") or c.get("long") or c.get("description", "")
        lines.append(f"Use the following {ctype}: {text}")
    if not lines:
        return []
    return [Message(role="system", content="\n".join(lines))]


def resolve_session(ref: str) -> Optional[Tuple[str, str]]:
    """Resolve a session reference like 'opencode/ses_abc' to (agent, session_id)."""
    parts = ref.strip("/").split("/", 1)
    agent = parts[0]
    sid = parts[1] if len(parts) > 1 else None

    if agent not in AGENTS:
        console.print(f"[red]Unknown agent: {agent}[/red]")
        console.print(f"[dim]Available: {', '.join(AGENTS.keys())}[/dim]")
        raise typer.Exit(1)

    if not sid:
        console.print(f"[red]No session ID in {ref}[/red]")
        raise typer.Exit(1)

    return agent, sid


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
    
    cid = concept_id(concept)
    filename = f"{concept.get('type', 'unknown')}_{cid}.json"
    filepath = p / filename
    
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
                if isinstance(data, dict):
                    concepts.append(data)
                elif isinstance(data, list):
                    concepts.extend(data)
        except json.JSONDecodeError:
            console.print(f"[yellow]Skipping invalid JSON: {f}[/yellow]")
    
    return concepts


def write_concepts_individual(concepts: list, dir_path: str):
    """Write each concept as an individual JSON file in a directory."""
    for concept in concepts:
        write_concept_individual(concept, dir_path)


def load_strategy(strategy_path: Optional[str] = None) -> Strategy:
    """Load a strategy, or return the default."""
    if strategy_path:
        return Strategy.load(strategy_path)
    return DEFAULT_STRATEGY


def get_session_messages(agent: str, session_id: str) -> List[Message]:
    """Get messages from an agent session (all roles)."""
    agent_info = AGENTS.get(agent)
    if not agent_info or not agent_info.base_path.exists():
        console.print(f"[yellow]Agent {agent} not found at {agent_info.base_path}[/yellow]")
        raise typer.Exit(1)

    if agent_info.storage_format == "sqlite":
        return _read_sqlite_messages(agent_info, session_id)
    elif agent_info.storage_format == "jsonl":
        return _read_jsonl_messages(agent_info, session_id)
    elif agent_info.storage_format == "json":
        return _read_json_messages(agent_info, session_id)

    console.print(f"[red]Unsupported format: {agent_info.storage_format}[/red]")
    raise typer.Exit(1)


def _read_sqlite_messages(agent_info, session_id: str) -> List[Message]:
    """Read all messages from a SQLite-backed session."""
    import sqlite3

    db_path = agent_info.base_path / "opencode.db"
    if not db_path.exists():
        console.print(f"[red]Database not found: {db_path}[/red]")
        raise typer.Exit(1)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    cursor.execute('''
        SELECT m.id, m.data
        FROM message m
        WHERE m.session_id = ?
        ORDER BY m.time_created
    ''', (session_id,))

    messages = []
    for msg_id, msg_data in cursor.fetchall():
        data = json.loads(msg_data)
        role = data.get('role', '')

        cursor.execute('''
            SELECT data FROM part
            WHERE message_id = ?
            ORDER BY time_created
        ''', (msg_id,))

        content_parts = []
        for (part_data,) in cursor.fetchall():
            part = json.loads(part_data)
            if part.get('type') == 'text':
                content_parts.append(part.get('text', ''))

        content = '\n'.join(content_parts) if content_parts else ''
        if content:
            messages.append(Message(role=role, content=content))

    conn.close()
    if not messages:
        console.print(f"[yellow]Session not found: {session_id}[/yellow]")
        raise typer.Exit(1)
    return messages


def _read_jsonl_messages(agent_info, session_id: str) -> List[Message]:
    """Read all messages from a JSONL-backed session."""
    for session_file in agent_info.base_path.glob(agent_info.session_pattern):
        if session_file.stem == session_id:
            messages = []
            with open(session_file, 'r') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        msg_type = data.get('type', '')
                        content = data.get('message', {}).get('content', '')
                        if content:
                            role = 'user' if msg_type == 'human' else msg_type
                            messages.append(Message(role=role, content=content))
                    except (json.JSONDecodeError, KeyError):
                        continue
            if messages:
                return messages

    console.print(f"[yellow]Session not found: {session_id}[/yellow]")
    raise typer.Exit(1)


def _read_json_messages(agent_info, session_id: str) -> List[Message]:
    """Read all messages from a JSON-backed session."""
    for session_file in agent_info.base_path.glob(agent_info.session_pattern):
        if session_file.stem == session_id:
            try:
                with open(session_file) as f:
                    data = json.load(f)
                messages_raw = data if isinstance(data, list) else data.get('messages', [])
                messages = [
                    Message(role=m.get('role', ''), content=m.get('content', ''))
                    for m in messages_raw if m.get('content')
                ]
                if messages:
                    return messages
            except (json.JSONDecodeError, KeyError):
                pass

    console.print(f"[yellow]Session not found: {session_id}[/yellow]")
    raise typer.Exit(1)


def inject_concepts_to_session(agent: str, session_id: str, concepts: list):
    """Inject concepts into an agent session as a system message."""
    agent_info = AGENTS.get(agent)
    if not agent_info or not agent_info.base_path.exists():
        console.print(f"[yellow]Agent {agent} not found[/yellow]")
        raise typer.Exit(1)

    if agent_info.storage_format == "sqlite":
        _inject_sqlite(agent_info, session_id, concepts)
    elif agent_info.storage_format == "jsonl":
        _inject_jsonl(agent_info, session_id, concepts)
    elif agent_info.storage_format == "json":
        _inject_json(agent_info, session_id, concepts)
    else:
        console.print(f"[red]Unsupported format: {agent_info.storage_format}[/red]")
        raise typer.Exit(1)


def _inject_sqlite(agent_info, session_id: str, concepts: list):
    """Inject concepts into a SQLite-backed session."""
    import sqlite3

    db_path = agent_info.base_path / "opencode.db"
    if not db_path.exists():
        console.print(f"[red]Database not found: {db_path}[/red]")
        raise typer.Exit(1)

    content_lines = []
    for c in concepts:
        ctype = c.get("type", "preference")
        text = c.get("short") or c.get("medium") or c.get("description", "")
        content_lines.append(f"Use the following {ctype}: {text}")

    system_content = "\n".join(content_lines)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Find existing gabn'go system message and replace, or insert one
    cursor.execute(
        "SELECT id FROM message WHERE session_id = ? AND data LIKE ?",
        (session_id, '%"role": "system"%'),
    )
    row = cursor.fetchone()

    now_ms = int(__import__("time").time() * 1000)

    if row:
        # Update existing system message
        msg_id = row[0]
        data = json.dumps({"role": "system", "content": system_content})
        cursor.execute(
            "UPDATE message SET data = ?, time_updated = ? WHERE id = ?",
            (data, now_ms, msg_id),
        )
        # Update parts
        cursor.execute(
            "UPDATE part SET data = ? WHERE message_id = ?",
            (json.dumps({"type": "text", "text": system_content}), msg_id),
        )
    else:
        # Insert new system message
        msg_id = f"ccopy_{now_ms}"
        data = json.dumps({"role": "system", "content": system_content})
        cursor.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
            (msg_id, session_id, now_ms, now_ms, data),
        )
        cursor.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (f"part_{msg_id}", msg_id, session_id, now_ms, now_ms,
             json.dumps({"type": "text", "text": system_content})),
        )

    conn.commit()
    conn.close()


def _inject_jsonl(agent_info, session_id: str, concepts: list):
    """Inject concepts into a JSONL-backed session."""
    content_lines = []
    for c in concepts:
        ctype = c.get("type", "preference")
        text = c.get("short") or c.get("medium") or c.get("description", "")
        content_lines.append(f"Use the following {ctype}: {text}")

    system_content = "\n".join(content_lines)

    # Find the session file
    for session_file in agent_info.base_path.glob(agent_info.session_pattern):
        if session_file.stem == session_id:
            # Check if there's already a system message
            lines = session_file.read_text().splitlines()
            replaced = False
            new_lines = []
            for line in lines:
                try:
                    data = json.loads(line)
                    if data.get("type") == "system":
                        data["message"]["content"] = system_content
                        new_lines.append(json.dumps(data))
                        replaced = True
                    else:
                        new_lines.append(line)
                except (json.JSONDecodeError, KeyError):
                    new_lines.append(line)

            if not replaced:
                system_msg = json.dumps({
                    "type": "system",
                    "message": {"role": "system", "content": system_content},
                })
                new_lines.insert(0, system_msg)

            session_file.write_text("\n".join(new_lines) + "\n")
            return

    console.print(f"[red]Session file not found for {session_id}[/red]")
    raise typer.Exit(1)


def _inject_json(agent_info, session_id: str, concepts: list):
    """Inject concepts into a JSON-backed session."""
    content_lines = []
    for c in concepts:
        ctype = c.get("type", "preference")
        text = c.get("short") or c.get("medium") or c.get("description", "")
        content_lines.append(f"Use the following {ctype}: {text}")

    system_content = "\n".join(content_lines)

    for session_file in agent_info.base_path.glob(agent_info.session_pattern):
        if session_file.stem == session_id:
            with open(session_file) as f:
                data = json.load(f)

            messages = data if isinstance(data, list) else data.get("messages", [])

            # Find existing system message and replace, or insert one
            replaced = False
            for msg in messages:
                if msg.get("role") == "system":
                    msg["content"] = system_content
                    replaced = True
                    break

            if not replaced:
                messages.insert(0, {"role": "system", "content": system_content})

            if isinstance(data, list):
                out = messages
            else:
                data["messages"] = messages
                out = data

            with open(session_file, "w") as f:
                json.dump(out, f, indent=2)
                f.write("\n")
            return

    console.print(f"[red]Session file not found for {session_id}[/red]")
    raise typer.Exit(1)


@app.command()
def main(
    args: List[str] = typer.Argument(..., help="Sources and destinations (@ for sessions)"),
    fmt: str = typer.Option("default", "--format", "-f", help="Output format: json, xml, md"),
    strategy: Optional[str] = typer.Option(None, "--strategy", "-s", help="Strategy JSON file for LLM-based extraction"),
):
    """
    Copy concepts between sessions and concept files.

    @ prefix denotes a session (agent/session_id).
    Plain paths are concept JSON files.
    Directories (ending with /) get one file per concept.

    Examples:
        ccopy @opencode/ses_abc concepts.json
        ccopy @opencode/ses_abc concepts/          # one file per concept
        ccopy constraints.json preferences.json @opencode/ses_abc
        ccopy @opencode/ses_abc @claude/ses_xyz
        ccopy --strategy my-strategy.json @opencode/ses_abc concepts.json
    """
    sessions, files = parse_args(args)

    if not sessions:
        console.print("[red]No session references (use @ prefix)[/red]")
        raise typer.Exit(1)

    # Determine operation mode
    has_concept_files = len(files) > 0
    has_sessions = len(sessions) > 0

    # Extract mode: session -> concept file
    if has_sessions and has_concept_files:
        # Check if last arg is a session (destination)
        # If so, all concept files before it are sources
        # Otherwise, extract from sessions to concept files

        # If the last arg is a session and others are files -> inject
        # If the last arg is a file and others are sessions -> extract
        # If mix -> error

        last_is_session = args[-1].startswith("@")
        first_is_session = args[0].startswith("@")

        if last_is_session:
            # Inject: concept files -> session
            dest_session = sessions[-1]
            agent, sid = resolve_session(dest_session)

            all_concepts = []
            for f in files:
                if f.endswith("/") or (Path(f).exists() and Path(f).is_dir()):
                    all_concepts.extend(read_concepts_from_dir(f))
                else:
                    all_concepts.extend(read_concepts_from_file(f))

            if not all_concepts:
                console.print("[yellow]No concepts found in files[/yellow]")
                return

            inject_concepts_to_session(agent, sid, all_concepts)
            console.print(f"[green]Injected {len(all_concepts)} concepts into {agent}/{sid}[/green]")

        elif first_is_session and len(sessions) == 1 and len(files) > 0:
            # Extract: session -> concept files
            agent, sid = resolve_session(sessions[0])
            messages = get_session_messages(agent, sid)
            
            # Use strategy for extraction if provided
            if strategy:
                strat = load_strategy(strategy)
                concepts = strat.extract([{"role": m.role, "content": m.content} for m in messages])
            else:
                concepts = extract_concepts_from_messages(messages)

            if not concepts:
                console.print("[yellow]No concepts found in session[/yellow]")
                return

            # Determine output: directory or file
            target = files[0]
            is_dir = target.endswith("/") or (Path(target).exists() and Path(target).is_dir())
            
            if is_dir:
                # Write each concept as a separate file
                out_dir = target.rstrip("/")
                write_concepts_individual(concepts, out_dir)
                console.print(f"[green]Extracted {len(concepts)} concepts to {out_dir}/ ({len(concepts)} files)[/green]")
            else:
                # Write all concepts to a single file
                write_concepts_to_file(concepts, target)
                console.print(f"[green]Extracted {len(concepts)} concepts to {target}[/green]")

        elif first_is_session and not last_is_session:
            # Session-to-concept extraction
            agent, sid = resolve_session(sessions[0])
            messages = get_session_messages(agent, sid)
            
            # Use strategy for extraction if provided
            if strategy:
                strat = load_strategy(strategy)
                concepts = strat.extract([{"role": m.role, "content": m.content} for m in messages])
            else:
                concepts = extract_concepts_from_messages(messages)

            if not concepts:
                console.print("[yellow]No concepts found in session[/yellow]")
                return

            # Determine output: directory or file
            target = files[0]
            is_dir = target.endswith("/") or (Path(target).exists() and Path(target).is_dir())
            
            if is_dir:
                # Write each concept as a separate file
                out_dir = target.rstrip("/")
                write_concepts_individual(concepts, out_dir)
                console.print(f"[green]Extracted {len(concepts)} concepts to {out_dir}/ ({len(concepts)} files)[/green]")
            else:
                write_concepts_to_file(concepts, target)
                console.print(f"[green]Extracted {len(concepts)} concepts to {target}[/green]")

        else:
            console.print("[red]Ambiguous: mix of sessions and files[/red]")
            raise typer.Exit(1)

    # Session-to-session mode
    elif has_sessions and len(sessions) >= 2 and not has_concept_files:
        # First session is source, rest are destinations
        src_session = sessions[0]
        src_agent, src_sid = resolve_session(src_session)
        messages = get_session_messages(src_agent, src_sid)
        
        # Use strategy for extraction if provided
        if strategy:
            strat = load_strategy(strategy)
            concepts = strat.extract([{"role": m.role, "content": m.content} for m in messages])
        else:
            concepts = extract_concepts_from_messages(messages)

        if not concepts:
            console.print(f"[yellow]No concepts found in {src_session}[/yellow]")
            return

        for dest_ref in sessions[1:]:
            dest_agent, dest_sid = resolve_session(dest_ref)
            inject_concepts_to_session(dest_agent, dest_sid, concepts)
            console.print(f"[green]Copied {len(concepts)} concepts to {dest_agent}/{dest_sid}[/green]")

    else:
        console.print("[red]Invalid arguments[/red]")
        console.print("[dim]Usage: ccopy @agent/sess file.json | ccopy file.json @agent/sess | ccopy @src @dst[/dim]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
