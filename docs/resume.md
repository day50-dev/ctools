# resume

`ctools resume` hands a session back to the tool that owns it. It computes the
agent's resume command, `cd`s into the directory the session was recorded in,
and `exec`s the command — replacing the ctools process. It is the one verb
that "leaves ctools" (the `xdg-open` analogy).

There is no standalone `cresume` shortcut; `resume` is a `ctools` verb only:

```sh
ctools resume claude-code/55b9c8b7-788b-4f9e-a8b7-cfa0461b357b  # a cdir row, pasted
ctools resume claude-code a293cac2   # cd + exec `claude --resume a293cac2`
ctools resume pi <id>                # `pi --session <id>`
ctools resume opencode <id>          # `opencode -s <id>`
```

## Naming the session

Whatever `cdir` printed, paste straight back:

| You type | Meaning |
|---|---|
| `claude-code/55b9c8b7` | the `agent/session` shape a cross-agent listing prints |
| `@claude-code/55b9c8b7` | the same, with the `@` marker `cextract` uses |
| `claude-code 55b9c8b7` | the two-word form |
| `55b9c8b7` | a bare id (what `cdir -1` prints), looked up in the installed agents |

A bare id resolves only when exactly one installed agent has that session.
If two do, `resume` names them and asks you to pick (`pi/55b9c8b7`); if none
does, it says so rather than guessing. An unknown agent prefix is an error —
it is never treated as a directory or a glob.

## How it works

1. Resolve the agent (unknown / not-installed / missing session are all
   reported and exit non-zero).
2. Compute the resume command from the agent backend — the same table the
   `cdir` resume hint uses, overridable per machine in
   `~/.config/ctools/resume-commands.json` (see below).
3. `cd` into the session's recorded working directory.
4. `exec` the command via `sh -c`, so a spaced argument is never word-split
   by a bare `exec $(…)`.

The `cd` in step 3 is what makes it work: `claude --resume <id>` only finds a
session from the directory it was recorded in. Resuming from the wrong
directory is the "No conversation found with session ID" root cause — see
[ccopy-claude-resume-notes.md](ccopy-claude-resume-notes.md). If the recorded
directory no longer exists, ctools warns and resumes from the current
directory instead.

## Print it without exec-ing

`cdir --resume-command AGENT/SESSION` prints the same command (with the `cd`
prefix) and exits, without running it. It takes exactly the shapes `resume`
takes. Same code path as `resume`, minus the `exec` — use it to copy the
command, hand it to another tool, or pipe it:

```sh
$ ctools dir --resume-command pi/<id>
cd /home/u/proj && pi --session <id>
```

## Per-machine resume commands

The built-in resume commands are the flags each tool documents. Override any of
them in `~/.config/ctools/resume-commands.json`; the override wins over the
built-in table:

```json
{
  "claude-code": "claude --resume {id}",
  "opencode": "opencode -s {id}",
  "pi": "pi --session {id}"
}
```

`{id}` is filled with the session id. If an agent has no entry in either the
built-in table or your override, `resume` reports `no resume command known for
<agent>` and exits non-zero.

## Errors

`resume` (and `cdir --resume-command`) exit non-zero with a reason when the
agent is unknown, the agent isn't installed, the session doesn't exist, or no
resume command is known for the agent.
