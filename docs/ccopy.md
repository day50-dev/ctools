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

## Related

- To extract **concepts** (constraints, preferences, goals) from a session into
  files, use [cextract](cextract.md).
- To search conversation content, use [cgrep](cgrep.md).
- To list sessions, use [cdir](cdir.md).
