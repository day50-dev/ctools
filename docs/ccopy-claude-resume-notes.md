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

---

# ccopy → Claude Code: the copy resumed but the edits "failed"

A second, independent failure, reported after the cwd fix landed: the copy
**loaded and resumed fine**, but the model then said it had made edits that
didn't exist — "thought it did edits, they all failed".

Bottom line: **the copy never carried the tool calls.** The common format is a
portable `{role, content}` text spine, so every tool call and its result is
dropped on the way across. The imported transcript therefore reads as an
assistant that edits files by *describing* the edit ("let me validate", "valid,
now reload and test") with nothing to show it ever did. Resumed, Claude matched
that style and kept narrating work it never performs — the transcript of the
resumed turn contains **zero** `tool_use` blocks. Nothing failed at the tool
layer; the tools were never called at all.

## Evidence

- The copied `…-ghub-notion/<id>.jsonl`'s 422-entry seeded prefix is text-only:
  no `tool_use`, no `tool_result`, every assistant message a narration of a
  change ("I cleaned up unused vars", "Let me validate", "Valid").
- The resumed turn: content blocks are `text` and `thinking` only; the model
  claims "the file now has MOVE_MODE = 1" and "File created and verified" while
  the file doesn't change — because no `tool_use` was ever emitted.
- opencode stores tool activity in `part` rows of type `tool`; the text
  projection (`_rows`) keeps only `type == 'text'`, so a cross-agent copy loses
  every call by construction.

## The fix

A copy that arrives without its tool history now says so, in the transcript
itself, so the resumed model cannot mistake narration for completed work:

- `Agent.import_notice(doc)` builds a closing user message, tagged
  `<ctools-import-notice>`, that names the source session, states that the
  conversation's **text only** came across, says **how many** tool calls and
  their results were left behind (when the envelope records them), and tells the
  model to re-read files with its own tools and make real tool calls instead of
  trusting the copied narration.
- It is **appended, not prepended**: on a long imported transcript the first
  message is the first thing context truncation drops, and the last message is
  the one a resuming model is reading from.
- It fires only when there is evidence something was dropped — the envelope
  names a session in a *different known* agent, or it records `tool_calls` a
  spine can't carry. A bare `{role, content}` pipe (`ccopy - …`) names no
  session, so it isn't annotated.
- A copy of an already-imported session keeps exactly one notice (the tag is
  detected and not re-stacked), and an empty envelope stays empty.
- opencode's dropped calls are now recoverable for the count: `OpencodeAgent
  .tool_calls()` reads the `tool` parts and reports each call's name and a
  one-line summary, so the notice can say *"N tool call(s) … left behind"*.
- `--no-import-notice` suppresses it, for the rare case you want the bare spine.

```sh
ccopy opencode/<ses> claude-code
# …
# Copied text only — the source's tool calls and their results are not in the
# new session, so it opens with a notice telling the model to re-read files
# rather than trust that narration (--no-import-notice to suppress).
#   Resume it with:
#     ctools resume claude-code/<new-id>
cd <source-cwd> && claude --resume <new-id>
# → loads, then re-reads the files for real instead of narrating the edits.
```

## Verify

```sh
ccopy opencode/<ses> claude-code
cd <source-cwd> && claude --resume <new-id>
# last message of the seeded transcript is the <ctools-import-notice>; the model
# re-reads files and issues real tool calls rather than claiming edits.
```

Tests: `test_cross_agent_copy_ends_with_import_notice`,
`test_notice_counts_the_tool_calls_left_behind`,
`test_same_agent_copy_annotated_only_when_tools_were_dropped`,
`test_notice_not_stacked_when_a_copy_is_copied`,
`test_notice_can_be_suppressed`, `test_piped_records_are_not_annotated`,
`test_opencode_tool_calls_are_exported`,
`test_opencode_tool_count_reaches_the_import_notice` (interchange).