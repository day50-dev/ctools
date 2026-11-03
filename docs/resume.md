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
ctools resume .                      # the most recent session in this directory
ctools resume pi .                   # ... of one agent, in this directory
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
| `.` / `..` / `/path` / `~/proj` | a directory: the most recent session recorded in it |
| `pi .` | the most recent session *of that agent* in the directory |

A bare id resolves only when exactly one installed agent has that session.
If two do, `resume` names them and asks you to pick (`pi/55b9c8b7`); if none
does, it says so rather than guessing. An unknown agent prefix is an error —
it is never treated as a directory or a glob.

### Resuming by directory

A directory in place of a session id selects the most recently modified session
recorded in that directory. It uses the same rule `cdir` and `cgrep` use for a
path argument (a session belongs to a directory when it ran in it or inside it),
so `resume .` picks up exactly the sessions `cdir .` lists — and it scopes to
one agent with the `agent .` two-word form. `resume` then does the normal
hand-off: `cd` into that session's own recorded directory and `exec` its resume
command.

Because a directory names no single session, `resume` prints which one
"most recent" resolved to on stderr before it leaves, so the hand-off is never
a silent guess:

```sh
$ ctools resume .
resuming pi/01a19fc4-… (most recent in .)
```

A directory argument must read unambiguously as a path: it is anchored (`.`,
`..`, `/…`, `~/…`, `./…`) or a slash path whose first component is not an agent
name and that exists on disk. That is why a bare relative name like `mydir` is
still treated as a session id — for that, type `./mydir` or give the absolute
path. If no session was recorded in the directory, `resume` says so and points
you at `cdir <path>`.

## How it works

1. Resolve the agent and session — or, for a directory argument, pick the
   most recent session recorded in it (unknown / not-installed / missing
   session are all reported and exit non-zero).
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
agent is unknown, the agent isn't installed, the session doesn't exist, a
directory has no recorded session, or no resume command is known for the agent.
