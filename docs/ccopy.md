# ccopy

`ccopy` copies a conversation from one agent to a new session in another. The
source is `agent/session_id` (no `@` prefix); the destination is a bare agent
name. A fresh session is created in the destination holding the copied
conversation, and ccopy prints how to resume it. The source is never touched, so
it's a true copy (not a move).

```sh
ccopy opencode/ses_abc claude-code     # opencode session -> new claude-code session
ccopy claude-code/ses_123 codex        # claude-code -> new codex session
ccopy opencode/ses_abc opencode        # duplicate within the same agent
ccopy opencode/ses_abc codex --dry-run # preview without writing
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

## Across hosts

Either side may live on another machine. Address it as
`ssh://[user@]host[:port]/agent[/session_id]` and ccopy runs over your normal ssh
setup — keys, `~/.ssh/config`, jump hosts, whatever you already use. **The remote
host does not need ctxttools installed** — it only needs `sshd` and a POSIX `tar`.

```sh
ccopy opencode/ses_abc ssh://chris@remote/codex       # push a conversation out
ccopy ssh://chris@remote/opencode/ses_abc codex       # pull a conversation in
ccopy ssh://chris@one/opencode/ses_abc ssh://chris@two/codex
```

The source is read, the destination is written into a brand-new session — so a
cross-host copy is still a copy: nothing on either host is modified. After a
cross-host copy, ccopy tells you the new session id and the resume command to run
**on the remote host**.

### How it works over the wire

The transport moves the agent's *storage files*, not ctools. ccopy pulls the
remote agent's storage (its sessions directory, or its database) over ssh with a
plain `tar` pipe, unpacks it into a local scratch directory, reads or writes the
conversation with the normal local agent code, and pushes any changed storage
back the same way. Nothing ctools-specific ever runs on the far side:

```sh
# pull:  ssh host 'tar -cf - -C ~ <agent storage>'   |  untar locally
# push:  tar -cf - -C <mirror> <agent storage>       |  ssh host 'tar -xf - -C ~'
```

The storage location is anchored at the remote's `$HOME` (the agent's default
install root, e.g. `~/.local/share/opencode`, `~/.pi/agent`), so the remote just
needs whatever agent it already runs — no extra tooling.

The two raw modes below are a local pipe convenience (they never touch ssh):

```sh
ccopy --export-json AGENT/SESSION   # print the conversation as a JSON list of {"role", "content"}
ccopy --import-json AGENT           # read that JSON from stdin, create a new session, print the new id
```

So locally you can reduce it to:

```sh
ccopy --export-json opencode/ses_abc | ccopy --import-json codex
```

## Related

- To extract **concepts** (constraints, preferences, goals) from a session into
  files, use [cextract](cextract.md).
- To read a conversation (locally or on a remote host) without copying it,
  use [ccat](ccat.md).
- To search conversation content, use [cgrep](cgrep.md).
- To list sessions, use [cdir](cdir.md).
