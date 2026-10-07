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
setup — keys, `~/.ssh/config`, jump hosts, whatever you already use. The remote
host needs ctxttools installed (`pip install ctxttools`).

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

The transport is plain ssh plus JSON on stdin/stdout. The two raw modes below are
the wire format: ccopy runs them on the far side, and they work in a local pipe
just as well.

```sh
ccopy --export-json AGENT/SESSION   # print the conversation as a JSON list of {"role", "content"}
ccopy --import-json AGENT           # read that JSON from stdin, create a new session, print the new id
```

So the whole thing reduces to:

```sh
ccopy --export-json opencode/ses_abc | ccopy --import-json codex
```

and, across a host boundary:

```sh
ssh chris@remote 'ccopy --export-json opencode/ses_abc' | ccopy --import-json codex
```

Because non-interactive ssh often has a minimal PATH, ccopy retries a failed
`ccopy` lookup as `python -m ctools.cli run ccopy ...` run from the checkout
directory (`$CTOOLS_DIR`, or the current directory). So the remote only needs
the package importable by some `python` reachable over ssh — a venv or a
checkout — not necessarily a `ccopy` on the ssh PATH.

## Related

- To extract **concepts** (constraints, preferences, goals) from a session into
  files, use [cextract](cextract.md).
- To search conversation content, use [cgrep](cgrep.md).
- To list sessions, use [cdir](cdir.md).
