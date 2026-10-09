"""Back up and restore agent storage before write operations.

Why this exists
---------------
``ccopy`` (and any future writer) seeds a *new* session into an agent's
storage. For the SQLite-backed agents that means writing rows into the
agent's **live** database (``opencode.db``, ``sessions.db`` for goose, ...).
A write that is interrupted, or that fails part-way, can leave a
``-journal``/``-wal`` side file in a bad state and take the whole sessions
database down with it. Losing every session in one agent is a catastrophe,
so:

  * **before** a write we take a snapshot of the agent's storage,
  * **after** the write we verify the database is still intact, and
  * if verification (or the write itself) fails we **restore** the snapshot.

Snapshots live under ``~/.local/share/ctools/backups/<agent>/<timestamp>/``
and are retained (bounded) so ``crecover`` can list and restore them later
when a failure is discovered out-of-band.

Only the standard library is used (``shutil``, ``sqlite3``, ``pathlib``).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from ctools.agents import Agent, AgentError

__all__ = [
    "BackupError", "Backup", "Backups",
    "backup_root", "take_backup", "verify_storage", "restore_backup",
    "guard_write",
]


class BackupError(Exception):
    """Raised when a backup, verification, or restore cannot be completed."""


# --- where backups live ----------------------------------------------------

def _home_share() -> Path:
    """XDG data home, defaulting to ``~/.local/share`` (Windows-safe)."""
    try:
        from ctools.log import xdg_data_home  # type: ignore
        return Path(xdg_data_home())
    except Exception:
        import os
        base = os.environ.get("XDG_DATA_HOME", "").strip()
        if base:
            return Path(base)
        return Path.home() / ".local" / "share"


def backup_root() -> Path:
    """Root directory holding per-agent backup snapshots."""
    return _home_share() / "ctools" / "backups"


def _agent_backup_dir(agent_name: str) -> Path:
    # A name is a short identifier, but keep this safe against path tricks
    # even if an agent name were ever to contain a separator.
    safe = agent_name.replace("/", "-").replace("..", "_")
    return backup_root() / safe


# --- a single backup -------------------------------------------------------

@dataclass
class Backup:
    """One snapshot of an agent's storage."""
    agent: str
    when: str                 # ISO-8601 local time (directory name)
    path: Path                # snapshot directory
    storage: str              # absolute path of the storage that was snapshotted
    kind: str                 # "sqlite" or "files"
    bytes: int = 0            # size of the snapshotted storage
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "agent": self.agent, "when": self.when,
            "path": str(self.path), "storage": self.storage,
            "kind": self.kind, "bytes": self.bytes, "note": self.note,
        }

    @classmethod
    def load(cls, path: Path) -> "Backup":
        """Read the ``.backup.json`` manifest inside a snapshot directory."""
        manifest = json.loads((path / ".backup.json").read_text(encoding="utf-8"))
        return cls(
            agent=manifest["agent"], when=manifest["when"], path=path,
            storage=manifest["storage"], kind=manifest["kind"],
            bytes=manifest.get("bytes", 0), note=manifest.get("note", ""))


def _kind_for(agent: Agent) -> str:
    """SQLite agents store one DB file; everything else is a directory tree."""
    return "sqlite" if getattr(type(agent), "db_name", None) else "files"


def _storage_size(storage: Path) -> int:
    if storage.is_file():
        return storage.stat().st_size
    total = 0
    for p in storage.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


# --- taking a backup -------------------------------------------------------

def take_backup(agent: Agent, note: str = "") -> Backup:
    """Snapshot ``agent.storage_path`` into a fresh, timestamped directory.

    For SQLite agents we use ``VACUUM INTO`` so the snapshot is a
    byte-consistent copy of the database (a plain file copy of a live DB can
    capture it mid-write). For directory agents we copy the tree.

    Raises :class:`BackupError` if the snapshot cannot be taken.
    """
    storage = agent.storage_path
    if not storage.exists():
        raise BackupError(f"cannot back up {agent.name}: storage not found at {storage}")
    kind = _kind_for(agent)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    dest = _agent_backup_dir(agent.name) / stamp
    # Never clobber an existing snapshot (same-ms collision).
    n = 1
    while dest.exists():
        dest = _agent_backup_dir(agent.name) / f"{stamp}-{n}"
        n += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        tmp.mkdir(parents=True)
        if kind == "sqlite":
            if not storage.is_file():
                raise BackupError(f"expected a SQLite file at {storage}, got a directory")
            if not storage.stat().st_size:
                # An empty (0-byte) DB is not openable by VACUUM INTO; just copy it.
                shutil.copy2(str(storage), str(tmp / storage.name))
            else:
                _vacuum_into(storage, tmp / storage.name)
        else:
            shutil.copytree(str(storage), str(tmp / "tree"),
                            symlinks=True, dirs_exist_ok=False)
        size = _storage_size(storage)
        manifest = Backup(
            agent=agent.name, when=datetime.now().isoformat(timespec="seconds"),
            path=dest, storage=str(storage), kind=kind, bytes=size, note=note)
        (tmp / ".backup.json").write_text(
            json.dumps(manifest.as_dict(), indent=2) + "\n", encoding="utf-8")
        tmp.rename(dest)
        return Backup.load(dest)
    except sqlite3.Error as e:
        _rmrf(tmp)
        raise BackupError(f"backup of {agent.name} failed: {e}") from e
    except (OSError, shutil.Error) as e:
        _rmrf(tmp)
        raise BackupError(f"backup of {agent.name} failed: {e}") from e


def _vacuum_into(src: Path, dst: Path) -> None:
    """Produce a consistent copy of a live SQLite DB via ``VACUUM INTO``."""
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        conn.execute(f"VACUUM INTO {sqlite3_quote(str(dst))}")
    finally:
        conn.close()


def sqlite3_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# --- verifying a live storage ---------------------------------------------

def _session_table(agent: Agent) -> Optional[str]:
    """The agent's session table name, or None if it cannot be determined."""
    for name in ("session", "sessions"):
        try:
            conn = sqlite3.connect(str(agent.storage_path))
            try:
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (name,)).fetchone()
                return name
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return None


def session_count(agent: Agent) -> Optional[int]:
    """Number of rows in the agent's session table (SQLite agents), or None
    if the table is absent/unreadable. Used as the guard's sentinel so a
    write that *empties* the database is caught, not just one that corrupts
    it structurally."""
    table = _session_table(agent)
    if table is None:
        return None
    try:
        conn = sqlite3.connect(str(agent.storage_path))
        try:
            return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def verify_storage(agent: Agent) -> bool:
    """Check the agent's storage is still usable *now*.

    SQLite agents are where a bad write can actually destroy data, so they
    get the strict check: the DB must open, pass ``PRAGMA integrity_check``
    (structural corruption), and still expose its session table (logical
    destruction). A 0-byte DB is a valid "installed, no sessions yet" state,
    not corruption.

    File/directory agents get a lighter check: our writes create *new* files
    rather than mutating a shared structure, so we only require that the
    storage still exists (and, for a single-file store, that it is either
    empty or still valid JSON/JSONL). Recursively parsing every file in a
    directory is deliberately avoided — it yields false positives on
    placeholder/lock files and files other tools own.
    """
    storage = agent.storage_path
    try:
        if _kind_for(agent) == "sqlite":
            if not storage.exists() or not storage.is_file():
                return False
            if storage.stat().st_size == 0:
                return True  # empty DB: nothing to be corrupt
            conn = sqlite3.connect(str(storage))
            try:
                rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
                if rows != ["ok"]:
                    return False
                return _session_table(agent) is not None
            finally:
                conn.close()
        else:
            if not storage.exists():
                return False
            if storage.is_file():
                if storage.stat().st_size == 0:
                    return True  # empty placeholder file
                _parse_json_or_jsonl(storage)
                return True
            return storage.is_dir()
    except Exception:
        return False


def _parse_json_or_jsonl(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="strict")
    if path.suffix == ".jsonl":
        for line in text.splitlines():
            if line.strip():
                json.loads(line)
    else:
        json.loads(text)


# --- restoring -------------------------------------------------------------

def restore_backup(agent: Agent, backup: Backup,
                   pre_restore_note: str = "pre-restore") -> Backup:
    """Restore ``backup`` over ``agent.storage_path``.

    This is itself a write, so it first takes a fresh backup of the *current*
    state (so a restore can be undone) and refuses to proceed if the current
    storage can't be backed up. Returns the pre-restore Backup so the caller
    can undo the restore if needed.
    """
    if not backup.path.exists():
        raise BackupError(f"backup not found: {backup.path}")
    current = take_backup(agent, note=pre_restore_note)
    storage = agent.storage_path
    stored = backup.path / (storage.name if backup.kind == "sqlite" else "tree")
    if not stored.exists():
        raise BackupError(f"snapshot payload missing in {backup.path}")
    # Remove the current storage, then copy the snapshot in its place.
    try:
        if storage.is_file():
            storage.unlink()
        elif storage.is_dir():
            shutil.rmtree(str(storage))
        if backup.kind == "sqlite":
            storage.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(stored), str(storage))
        else:
            shutil.copytree(str(stored), str(storage), symlinks=True,
                            dirs_exist_ok=True)
    except (OSError, shutil.Error) as e:
        # Best effort: if we removed the current storage but failed to put the
        # snapshot in place, restore the pre-restore snapshot immediately.
        try:
            restore_backup(agent, current, pre_restore_note="rollback")
        except BackupError:
            pass
        raise BackupError(f"restore of {agent.name} failed: {e}") from e
    if not verify_storage(agent):
        # The restored snapshot itself is not clean; roll back to `current`.
        restore_backup(agent, current, pre_restore_note="rollback-after-bad-restore")
        raise BackupError(
            f"restored snapshot for {agent.name} failed integrity; rolled back")
    return current


# --- listing + retention ---------------------------------------------------

class Backups:
    """Convenience accessor over the on-disk backup tree."""

    def __init__(self, root: Optional[Path] = None):
        self.root = root or backup_root()

    def for_agent(self, agent_name: str) -> List[Backup]:
        d = _agent_backup_dir(agent_name)
        if not d.exists():
            return []
        out: List[Backup] = []
        for child in d.iterdir():
            if child.is_dir() and (child / ".backup.json").exists():
                try:
                    out.append(Backup.load(child))
                except (OSError, json.JSONDecodeError, KeyError):
                    continue  # skip a torn/corrupt manifest
        out.sort(key=lambda b: b.when, reverse=True)
        return out

    def all(self) -> List[Backup]:
        out: List[Backup] = []
        if self.root.exists():
            for child in self.root.iterdir():
                if child.is_dir():
                    out.extend(self.for_agent(child.name))
        out.sort(key=lambda b: b.when, reverse=True)
        return out

    def prune(self, agent_name: str, keep: int) -> int:
        """Keep only the newest ``keep`` backups for an agent; drop the rest.

        Returns the number of snapshots removed.
        """
        backups = self.for_agent(agent_name)
        removed = 0
        for old in backups[keep:]:
            try:
                _rmrf(old.path)
                removed += 1
            except OSError:
                pass
        return removed


def _rmrf(p: Path) -> None:
    if not p.exists():
        return
    if p.is_dir():
        shutil.rmtree(str(p), ignore_errors=True)
    else:
        try:
            p.unlink()
        except OSError:
            pass


# --- the high-level guard ccopy uses --------------------------------------

@dataclass
class WriteGuard:
    """Context object returned by :func:`guard_write`.

    Use it like::

        guard = guard_write(agent, note="ccopy")
        try:
            new_id = agent.create_session(messages)   # the write
            guard.finish()                            # verify; restore on fail
            return new_id
        except Exception:
            guard.rollback()
            raise
    """
    agent: Agent
    backup: Optional[Backup]
    sessions_before: Optional[int] = None
    _done: bool = False

    def finish(self) -> bool:
        """Verify the storage after the write. On failure, restore the backup
        and raise :class:`BackupError` (the write is considered aborted).
        Returns ``True`` if the storage verified clean.

        The check is two-part: the storage must still be structurally sound
        (:func:`verify_storage`) **and**, for SQLite agents, the session count
        must not have *decreased* — a write that empties the database is a
        corruption even though ``PRAGMA integrity_check`` would pass on it.
        """
        if self._done:
            return True
        self._done = True
        clean = verify_storage(self.agent)
        if self.backup is not None:
            after = session_count(self.agent)
            if self.sessions_before is not None and after is not None \
                    and after < self.sessions_before:
                clean = False
        if clean:
            return True
        restore_backup(self.agent, self.backup, pre_restore_note="auto-restore")
        raise BackupError(
            f"post-write integrity check failed for {self.agent.name}; "
            f"restored backup {self.backup.when}")

    def rollback(self) -> None:
        """Restore the backup (write failed). Does not raise if there is no
        backup to restore."""
        if self._done:
            return
        self._done = True
        if self.backup is None:
            return
        restore_backup(self.agent, self.backup, pre_restore_note="rollback")


def guard_write(agent: Agent, note: str = "write",
                keep_backups: int = 20) -> WriteGuard:
    """Back up ``agent``'s storage and return a :class:`WriteGuard`.

    The guard snapshots the storage *now*; the caller performs the write,
    then calls :meth:`WriteGuard.finish` (verify + auto-restore) or
    :meth:`WriteGuard.rollback`. Backups are pruned to ``keep_backups``.
    """
    b = take_backup(agent, note=note)
    Backups().prune(agent.name, keep_backups)
    return WriteGuard(agent=agent, backup=b, sessions_before=session_count(agent))
