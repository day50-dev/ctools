# ccopy → Claude Code: why the copy wouldn't resume (and the fix)

Investigation notes for the "STILL BROKEN" report where
`claude --resume <id>` printed:

```
No conversation found with session ID: a293cac2-e5c8-4f83-a16d-18f2148ed953
```

Bottom line: **it was a location/cwd mismatch, not a file-format bug.** The
copy was being written into the *invocation* directory's Claude project folder
instead of the *source session's* working directory's project folder. Claude's
`--resume` only searches the project dir that matches the cwd you run it from,
so resuming from the source project found nothing. Fixed in `9c24df9`.

---

## Symptom

`ccopy pi/<sid> claude-code` produced a session id. Running
`claude --resume <id>` from the directory the conversation actually lived in
reported "No conversation found".

## Root cause (one line)

The new session file landed in `~/.claude/projects/<encoded-invoke-cwd>/`, but
`claude --resume` run from the source project looks in
`~/.claude/projects/<encoded-source-cwd>/`. Different dir ⇒ not found.

Claude Code files sessions under `~/.claude/projects/<encoded-cwd>/<id>.jsonl`
where `<encoded-cwd>` is the working directory with every non-alphanumeric
character turned into `-` (e.g. `/home/u/proj` → `-home-u-proj`). `--resume`
is scoped to that per-cwd project dir. `ccopy` was seeding the session for
`os.getcwd()` (where ccopy was run), not the source session's cwd, so the file
and the lookup dir disagreed.

## Evidence

Session `a293cac2-…` after the bug:

- File: `~/.claude/projects/-home-chris-day50-ctools/a293cac2-….jsonl`
- Every entry's `cwd` field: `/home/chris/day50/ctools`

| Command | cwd | Result |
|---|---|---|
| `claude --resume a293cac2` | `/home/chris/day50/ctools` | **loads** (emits "No deferred tool marker found …") |
| `claude --resume a293cac2` | `/home/chris` | "No conversation found with session ID: …" |

Same file, same id: resuming from the directory the file actually belongs to
loads it; resuming from anywhere else says "No conversation found". That is a
location problem, not a parsing problem.

**"No deferred tool marker found in the resumed session"** is the *success*
signal (the transcript loaded; it then notes there's no pending deferred tool
to continue). It is not "No conversation found".

## The red herring

The ccopy-written assistant messages used `content: "a string"` and lacked
`type: "message"` (a real Claude session uses `message.type == "message"` with
`content` as a list of blocks). That **does not** cause "No conversation
found": resuming the ccopy file from the correct cwd loads it fine. The reader
accepts string content and a bare `message` object. Chasing this was a dead
end.

## Binary loader (Claude Code 2.1.302)

Extracted from `~/.local/share/claude/versions/2.1.302`:

- `--resume <id>` calls `ixe(sessionId, …)` →
  `S = await Nxe(id, undefined, storageV5, gt) ?? await s4o(…) ?? await i4o(…)`
  — if all three loaders return null, the CLI prints "No conversation found".
- `Nxe` (primary transcript loader) calls `h0e` (resolves the file path under
  the cwd's project dir) then parses the JSONL.
- `wXs` (conversation reconstruction) needs a non-empty messages map plus a
  selectable leaf; the `last-prompt` entry's `leafUuid` feeds the `leafUuids`
  set, and if it's absent a fallback leaf is chosen.

(Note: a regex for the "parser" surfaced a function named `OZ` that is actually
a CEL schema validator — a minifier name collision, not the JSONL parser.)

## Minimal resumable format (bisected against real `claude`)

Empirically minimal set that loads (float-vs-ISO bisection, tmux against
2.1.302):

Required:

- `type`: `user` / `assistant`
- `uuid`
- `parentUuid` chain (the root entry is `null`)
- `timestamp`: **ISO-8601 UTC** — a float epoch timestamp makes the loader
  reject the whole file with "No conversation found"
- `sessionId`
- `message`: `{role, content}` (string content is fine)

Included, not proven required:

- `isSidechain: false`, `userType`, `entrypoint`, `cwd`

Not required for `--resume`:

- `last-prompt`, `attachment`, `queue-operation`, `cost-state`

## The fix (commit `9c24df9`)

Thread the **source session's cwd** through to the destination so the copy
lands in the matching project dir:

- `Agent.to_common()` records the source `cwd` (from the session's `path`,
  rejecting values that are the session's own storage *file*).
- `Agent.from_common()` / `create_session(cwd=…)` apply it. Claude Code, pi,
  opencode, goose, and hermes all honour `cwd` (Claude Code files it under
  `~/.claude/projects/<encoded-cwd>/` and records it on every entry; pi under
  `--<cwd>--`; the sqlite agents store it in their session row).
- `ccopy` now defaults the destination to the source cwd, with overrides:
  - `--here` — seed for the invocation directory (old behavior)
  - `--cwd DIR` — seed for an explicit directory
- Remote destinations carry the cwd over the wire (`--import-json … --cwd`)
  and through the tar mirror path. The webui batch Copy shells out to `ccopy`,
  so it inherits the fix.

## Verify

```sh
ccopy pi/<sid> claude-code
# session id printed; file now under ~/.claude/projects/<encoded-source-cwd>/
cd <source-cwd> && claude --resume <new-id>
# → loads. You may see "No deferred tool marker found" (success) or a later
#   API/auth error, but NOT "No conversation found".
```

Tests: `test_claude_code_create_session_seeds_source_cwd`,
`test_claude_code_create_session_defaults_to_cwd` (agents), and
`test_copy_seeds_destination_for_source_cwd` (ccopy).