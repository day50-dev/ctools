# ccopy

`ccopy` copies a conversation from one agent to a new session in another. The
source can be `agent/session_id`, a conversation JSON **file** (or `/dev/fd/N`,
or `-` for stdin), or a remote `ssh://...` reference; the destination is an
agent name, or `ssh://host/agent` on another machine. A fresh session is
created in the destination holding the copied conversation, and ccopy prints
how to resume it. The source is never touched, so it's a true copy (not a move).

`ccopy` is the short name for `ctools copy`: the same program with the same flags.
The examples use `ccopy`; `ctools copy …` works anywhere they do.

```sh
ccopy opencode/ses_abc claude-code     # opencode session -> new claude-code session
ccopy claude-code/ses_123 codex        # claude-code -> new codex session
ccopy opencode/ses_abc opencode        # duplicate within the same agent
ccopy opencode/ses_abc codex --dry-run # preview without writing
ccopy conv.json codex                  # seed a new session from a JSON file
ccat opencode/ses_abc | ccopy - codex  # seed a new session from a pipe
ccopy opencode/ses_abc ssh://chris@remote/codex     # local -> new session on a remote host
ccopy ssh://chris@remote/opencode/ses_abc codex     # pull a remote conversation
```

```sh
$ ccopy opencode/ses_abc claude-code
Copied 42 message(s) from opencode/ses_abc to a new claude-code session: 1a2b3c
Resume it with:
  claude-code --resume 1a2b3c
```

## Destinations

Only agents that support session creation work as destinations (claude-code,
opencode, kilo, codex, pi). The others refuse with a clean error. `--dry-run`
shows what would happen without writing anything.

## Where the new session lands (working directory)

Agents that file sessions under a per-working-directory project folder (notably
Claude Code, under `~/.claude/projects/<encoded-cwd>/`) need the copy seeded for
the **right directory**, or `claude --resume` can't find it from where you run
it. By default ccopy seeds the new session for the **source session's own
cwd** — so a pi session that ran in `/home/u/proj` copies into
`~/.claude/projects/-home-u-proj/`, and `cd /home/u/proj && claude --resume <id>`
works. When the source has no recorded cwd (e.g. a bare JSON file), ccopy falls
back to the directory you run it from.

Override the default:

```sh
ccopy pi/ses_abc claude-code --here        # seed for the current directory (old behavior)
ccopy pi/ses_abc claude-code --cwd /x/y    # seed for an explicit directory
```

See [ccopy-claude-resume-notes.md](ccopy-claude-resume-notes.md) for why this
matters (the "No conversation found" on resume bug and how it was fixed).

## Across hosts

There are two ways to get a conversation across a machine boundary, and you
pick based on what the remote has.

**The remote already runs ctools** — pipe it, the Unix way. `ccat` on the remote
prints the conversation as JSON; feed that into a local `ccopy`. The remote only
runs `ccat`, and the source is read, never written:

```sh
ccopy <(ssh chris@remote ccat opencode/ses_abc) codex   # process substitution
ssh chris@remote ccat opencode/ses_abc | ccopy - codex  # or an explicit pipe
```

**The remote does not need ctools** — let ccopy do the ssh. Address it as
`ssh://[user@]host[:port]/agent[/session_id]` and ccopy runs over your normal ssh
setup (keys, `~/.ssh/config`, jump hosts). The remote host only needs `sshd` and
a POSIX `tar`:

```sh
ccopy opencode/ses_abc ssh://chris@remote/codex       # push a conversation out
ccopy opencode/ses_abc ssh://chris@remote/            # ... into the same agent (opencode) there
ccopy ssh://chris@remote/opencode/ses_abc codex       # pull a conversation in
ccopy ssh://chris@one/opencode/ses_abc ssh://chris@two/codex
```

Leave the agent off a remote destination (`ssh://host/`) and ccopy uses the
source's agent on the far side. (A conversation-file source has no agent, so
there you still have to name one.)

The source is read, the destination is written into a brand-new session — so a
cross-host copy is still a copy: nothing on either host is modified. After a
cross-host copy, ccopy tells you the new session id and the resume command to run
**on the remote host**.

### How it works over the wire

ccopy works in **two tiers**. First it asks the remote which ctools it runs
(`ccopy --version`, with `~/.local/bin` and `~/bin` put on PATH, since a
non-interactive ssh leaves them out). If it runs **ctools 0.4.0 or newer**, it
takes the **cheap path** — the far side runs ctools itself and only the
conversation JSON crosses the wire, in one ssh round trip per side: the source
is read with `ccat --raw AGENT/SESSION` (the lossless envelope) and the
destination is seeded with `ccopy --import-json AGENT`. If something
goes wrong on that path (no such session there, say), ccopy reports what the far
side said; it doesn't fall back to copying storage. If the remote has no ctools,
or one older than 0.4.0 (ccopy says so and suggests upgrading), it takes the
**expensive path** — it moves the agent's *storage
files* instead: it pulls the remote storage (its sessions directory, or its
database) over ssh with a plain `tar` pipe, unpacks it into a local scratch
directory, reads or writes the conversation with the normal local agent code, and
pushes any changed storage back the same way. Nothing ctools-specific runs on the
far side in that path, so a bare remote still works.

```sh
# cheap (remote has ctools):  ssh host 'ccat --raw agent/ses'        -> parse envelope
#                            ssh host 'ccopy --import-json agent' < records
# expensive (remote has only tar):
#   pull:  ssh host 'tar -cf - -C ~ <agent storage>'   |  untar locally
#   push:  tar -cf - -C <mirror> <agent storage>       |  ssh host 'tar -xf - -C ~'
```

The storage location is anchored at the remote's `$HOME` (the agent's default
install root, e.g. `~/.local/share/opencode`, `~/.pi/agent`), so the remote just
needs whatever agent it already runs — no extra tooling.

`ccat` prints a conversation in exactly this JSON form, so the pipe form is just
`ccat … | ccopy - …`. ccopy also exposes the raw wire modes directly (they never
touch ssh):

```sh
ccopy --export-json AGENT/SESSION   # print the conversation as a JSON list of {"role", "content"}
ccopy --import-json AGENT           # read that JSON from stdin, create a new session, print the new id
```

So locally you can reduce it to:

```sh
ccopy --export-json opencode/ses_abc | ccopy --import-json codex
# equivalently, with ccat producing the JSON:
ccat opencode/ses_abc | ccopy - codex
```

## Safety: automatic backups

A local `ccopy` writes a new session into the destination agent's **live**
storage. For the SQLite-backed agents that means inserting rows into the
database that holds *all* of that agent's sessions — a write that fails or is
interrupted part-way could take the whole sessions database down with it.

So `ccopy` guards every local write:

1. **Before** the write it snapshots the destination agent's storage (for
   SQLite agents via `VACUUM INTO`, so the copy is consistent even mid-write).
2. **After** the write it verifies the database is still intact *and* that the
   session count did not decrease — a write that corrupts the DB or wipes it
   is caught even when `PRAGMA integrity_check` would pass on the wreckage.
3. If either check fails, the snapshot is **restored automatically** and
   ccopy reports what happened instead of leaving a broken database behind.

On success ccopy prints where the snapshot went:

```
Backup of opencode storage saved before this write:
  ~/.local/share/ctools/backups/opencode/20261008T201349-405
Restore it with: crecover restore opencode ~/.local/share/ctools/backups/opencode/20261008T201349-405
```

Snapshots are retained (20 newest per agent) so you can recover out-of-band
failures later — see [crecover](crecover.md). A *remote* destination writes on
the other host, so there is no local storage to back up in that case.

## Related

- To extract **concepts** (constraints, preferences, goals) from a session into
  files, use [cextract](cextract.md).
- To read a conversation (locally or on a remote host) without copying it,
  use [ccat](ccat.md).
- To search conversation content, use [cgrep](cgrep.md).
- To list sessions, use [cdir](cdir.md).
