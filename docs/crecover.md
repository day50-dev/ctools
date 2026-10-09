# crecover

`crecover` lists, verifies, and restores the storage backups that ctools takes
automatically before every local write (see [ccopy's safety
section](ccopy.md#safety-automatic-backups)).

The big failure it guards against: a write that lands in an agent's **live**
SQLite database (opencode, goose, kilo, hermes, …) and corrupts or empties it.
Losing every session in one agent is the worst thing that can happen to your
history, so ctools snapshots the storage first and can put it back.

```sh
crecover                      # list all retained backups (== crecover list)
crecover list                 # same
crecover list opencode        # one agent's backups, newest first
crecover verify               # integrity-check every installed agent's storage
crecover verify opencode      # check one agent
crecover restore opencode 20261008T201349-405
crecover restore opencode <path-to-snapshot-dir> -y
```

## How it works

- **Snapshots** live under `~/.local/share/ctools/backups/<agent>/<timestamp>/`
  and are retained (the 20 newest per agent by default). For SQLite agents the
  snapshot is made with `VACUUM INTO`, so it is a byte-consistent copy of the
  database even if the original is mid-write; for file-based agents it's a copy
  of the storage tree.
- **verify** checks the *live* storage. For SQLite agents that means the DB
  opens, `PRAGMA integrity_check` passes, and the session table still exists —
  a database that is structurally fine but has been *emptied of its tables* is
  reported as FAIL.
- **restore** overwrites the current storage with a snapshot. Because a restore
  is itself a write, it first takes a fresh snapshot of what it is about to
  clobber, so **a restore is always undoable** (just restore the snapshot it
  printed at the end). After restoring, it re-runs the integrity check on the
  restored storage.

You normally never call this by hand — `ccopy` (and the web dashboard's Copy)
restore automatically when a post-write check fails. `crecover` is for the
out-of-band case: you notice days later that an agent's history is gone or
corrupt.

## Exit codes

- `0` — success / all agents intact
- `1` — one or more agents failed integrity, or a restore/list error
