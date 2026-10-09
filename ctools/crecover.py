"""``crecover`` — list, verify, and restore agent-storage backups.

``ctools`` takes a snapshot of an agent's storage before every write
(``ctools.backup``). ``crecover`` is the tool you reach for afterwards:

  * ``crecover`` / ``crecover list``        show the retained snapshots
  * ``crecover list AGENT``                 show one agent's snapshots
  * ``crecover verify``                     check the live storage of every
                                            installed agent is intact
  * ``crecover verify AGENT``               check one agent's live storage
  * ``crecover restore AGENT BACKUP``       restore a snapshot (BACKUP is a
                                            timestamp dir or a full path)

Restore is itself a write, so it takes a fresh snapshot of the current state
first (undoable) and verifies the restored storage is clean before it
declares success.
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from ctools import __version__
from ctools.agents import REGISTRY, AgentError, get_agent
from ctools.backup import (Backup, Backups, BackupError, restore_backup,
                           verify_storage, session_count)
from ctools.cli import handle_version, version_option
from ctools.log import configure_logging
from rich.console import Console

console = Console()
err_console = Console(stderr=True)

__all__ = ["app", "main"]


def _installed() -> list:
    out = []
    for name in REGISTRY:
        try:
            a = get_agent(name)
            if a is not None and a.exists():
                out.append(a)
        except AgentError:
            continue
    return out


def _resolve_backup(agent_name: str, ref: str) -> Backup:
    """Find a backup by timestamp (suffix match) or by full path."""
    backups = Backups().for_agent(agent_name)
    # Exact timestamp / name.
    for b in backups:
        if b.when == ref or b.path.name == ref:
            return b
    # A timestamp passed as just the directory name or a trailing component.
    for b in backups:
        if b.path.name.endswith(ref) or b.when.endswith(ref):
            return b
    # A full (or relative) path.
    p = Path(ref)
    if p.exists() and (p / ".backup.json").exists():
        return Backup.load(p)
    raise BackupError(
        f"no backup matching {ref!r} for {agent_name}. "
        f"List them with `crecover list {agent_name}`.")


# --- commands --------------------------------------------------------------

def cmd_list(args) -> int:
    store = Backups()
    if args.agent:
        agent = get_agent(args.agent)
        if agent is None:
            err_console.print(f"[red]Unknown agent: {args.agent}[/red]")
            return 1
        backups = store.for_agent(args.agent)
        if not backups:
            console.print(f"[dim]No backups for {args.agent}.[/dim]")
            return 0
        _print_backups(args.agent, backups)
        return 0
    all_b = store.all()
    if not all_b:
        console.print("[dim]No backups yet. ccopy takes one before each "
                      "local write.[/dim]")
        return 0
    for agent_name in sorted({b.agent for b in all_b}):
        console.print(f"\n[bold]{agent_name}[/bold]")
        _print_backups(agent_name, [b for b in all_b if b.agent == agent_name])
    return 0


def _print_backups(agent_name: str, backups: List[Backup]) -> None:
    for i, b in enumerate(backups):
        marker = " [green](latest)[/green]" if i == 0 else ""
        size = _human(b.bytes)
        console.print(f"  {b.when}  {size:>9}  {b.note or '-'}{marker}  "
                      f"[dim]{b.path}[/dim]")


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def cmd_verify(args) -> int:
    targets = ([get_agent(args.agent)] if args.agent else _installed())
    if args.agent and (targets[0] is None if targets else True):
        err_console.print(f"[red]Unknown agent: {args.agent}[/red]")
        return 1
    if not targets:
        console.print("[dim]No installed agents to check.[/dim]")
        return 0
    bad = 0
    for a in targets:
        ok = verify_storage(a)
        count = session_count(a)
        cnt = f" ({count} sessions)" if count is not None else ""
        if ok:
            console.print(f"[green]OK   [/green]{a.name}{cnt}")
        else:
            console.print(f"[red]FAIL [/red]{a.name}{cnt}  [dim]{a.storage_path}[/dim]")
            bad += 1
    if bad:
        console.print(f"\n[red]{bad} agent(s) failed integrity checks. "
                      f"Restore a snapshot with "
                      f"[bold]crecover restore AGENT BACKUP[/bold].[/red]")
        return 1
    console.print("\n[dim]All checked agents are intact.[/dim]")
    return 0


def cmd_restore(args) -> int:
    agent = get_agent(args.agent)
    if agent is None:
        err_console.print(f"[red]Unknown agent: {args.agent}[/red]")
        return 1
    if not agent.exists():
        err_console.print(f"[yellow]Agent path not found: {agent.base_path}[/yellow]")
        err_console.print("[dim]Nothing to restore into.[/dim]")
        return 1
    try:
        backup = _resolve_backup(args.agent, args.backup)
    except BackupError as e:
        err_console.print(f"[red]{e}[/red]")
        return 1

    if not args.yes:
        console.print(f"About to restore [bold]{args.agent}[/bold] storage from:\n"
                      f"  {backup.path}  (taken {backup.when}, {backup.note or 'no note'})")
        console.print("[yellow]This overwrites the current storage (a fresh "
                      "snapshot of it is taken first, so it is undoable).[/yellow]")
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            console.print("[dim]Aborted.[/dim]")
            return 1

    try:
        pre = restore_backup(agent, backup,
                             pre_restore_note=f"pre-restore {args.agent}")
    except BackupError as e:
        err_console.print(f"[red]Restore failed: {e}[/red]")
        return 1

    console.print(f"[green]Restored {args.agent} from {backup.path}.[/green]")
    console.print(f"[dim]The state you were just overwriting is saved as: "
                  f"{pre.path}[/dim]")
    if not verify_storage(agent):
        console.print("[yellow]Warning: restored storage failed the integrity "
                      "check.[/yellow]")
        return 1
    return 0


# --- wiring ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crecover",
        description="List, verify, and restore agent-storage backups taken by "
                    "ctools before write operations.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose logging")
    version_option(parser, "crecover")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="Show retained backups")
    p_list.add_argument("agent", nargs="?", help="Agent name (default: all)")
    p_list.set_defaults(func=cmd_list)

    p_verify = sub.add_parser("verify", help="Check live storage integrity")
    p_verify.add_argument("agent", nargs="?", help="Agent name (default: all installed)")
    p_verify.set_defaults(func=cmd_verify)

    p_restore = sub.add_parser("restore", help="Restore a snapshot")
    p_restore.add_argument("agent", help="Agent name")
    p_restore.add_argument("backup", help="Backup timestamp or path")
    p_restore.add_argument("-y", "--yes", action="store_true",
                           help="Do not prompt for confirmation")
    p_restore.set_defaults(func=cmd_restore)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if (v := handle_version(args)) is not None:
        return v
    configure_logging(verbose=args.verbose)
    # Default command: list everything (GNU-ish `crecover` == `crecover list`).
    if not getattr(args, "func", None):
        args.func = cmd_list
        args.agent = None
    try:
        return args.func(args)
    except BackupError as e:
        err_console.print(f"[red]{e}[/red]")
        return 1


app = main


if __name__ == "__main__":
    raise SystemExit(main())
