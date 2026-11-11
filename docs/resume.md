# resume

`ctools resume` hands a session back to the tool that owns it. It computes the
agent's resume command, `cd`s into the directory the session was recorded in,
and `exec`s the command - replacing the ctools process. It is the one verb
that "leaves ctools" (the `xdg-open` analogy).

There is no standalone `cresume` shortcut; `resume` is a `ctools` verb only:

```sh
ctools resume claude-code/55b9c8b7-788b-4f9e-a8b7-cfa0461b357b  # a cdir row, pasted
ctools resume claude-code a293cac2   # cd + exec `claude --resume a293cac2`
ctools resume pi <id>                # `pi --session <id>`
ctools resume .                      # the most recent session in this directory
ctools resume pi .                   # ... of one agent, in this directory
ctools resume opencode <id>          # `opencode -s <id>`
ctools resume ssh://me@box/code      # the most recent session in /code on box,
                                     #   pulled here first, then resumed here
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
| `ssh://[user@]host[:port]/agent[/session_id]` | a session on a remote host (see below) |
| `ssh://me@box/code` | the most recent session in `/code` **on box**, pulled and resumed here |

A bare id resolves only when exactly one installed agent has that session.
If two do, `resume` names them and asks you to pick (`pi/55b9c8b7`); if none
does, it says so rather than guessing. An unknown agent prefix is an error -
it is never treated as a directory or a glob.

### Resuming by directory

A directory in place of a session id selects the most recently modified session
recorded in that directory. It uses the same rule `cdir` and `cgrep` use for a
path argument (a session belongs to a directory when it ran in it or inside it),
so `resume .` picks up exactly the sessions `cdir .` lists - and it scopes to
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
still treated as a session id - for that, type `./mydir` or give the absolute
path. If no session was recorded in the directory, `resume` says so and points
you at `cdir <path>`.

## Resuming a remote session

`ctools resume ssh://host/…` does not exec on the remote - it brings the
session home first, so you resume it in your own tooling with your own model
and credentials, exactly as if you had run `ccopy` and then `resume`. The path
is interpreted **on the remote host**, which is the whole point: `ssh://box/`
`code` picks the most recent session in `/code` *there*, where that directory
means something, and the answer comes back over ssh rather than a local guess.

```sh
ctools resume ssh://me@box/opencode/ses_abc   # pull one session, resume it here
ctools resume ssh://me@box/opencode           # pull the most recent opencode session on box
ctools resume ssh://me@box/code               # the most recent session recorded in /code on box
```

Steps:

1. **Resolve on the remote.** `ctools resume --resolve` runs *on the host*
   (`ssh box 'ctools resume --resolve <ref>'`) and prints `agent/session_id`.
   A remote without ctools falls back to listing its sessions the way `cdir`
   does and matching against the path you named.
2. **Pull it.** The resolved session is copied to this machine with the same
   machinery as `ccopy` - the common interchange when the remote runs ctools,
   a tar of the agent's storage otherwise. You now have a local copy you can
   inspect, and the original on box is untouched.
3. **Hand off.** `cd` into the copied session's recorded directory and `exec`
   its resume command, as a local resume does. The note on stderr says the
   copy was pulled from where.

The same `ssh://` syntax `cdir` and `ccopy` take, so `cdir ssh://box/opencode`
and `ctools resume ssh://box/opencode/ses_abc` name the same sessions.

### Print it without exec-ing

`--resolve` is the resolution step by itself: it prints the
`agent/session_id` the remote settled on and exits, without pulling or
exec-ing. Use it to see what `resume` would do before spending the transfer:

```sh
$ ctools resume --resolve ssh://me@box/code
opencode/ses_abc123
```

It also works locally, which is handy for scripting:

```sh
$ ctools resume --resolve .
pi/01a19fc4-…
```

## How it works

1. Resolve the agent and session - or, for a directory argument, pick the
   most recent session recorded in it (unknown / not-installed / missing
   session are all reported and exit non-zero). For `ssh://` references, the
   resolution happens on the remote host (see above), the session is pulled
   over ssh, and the steps that follow run on the local copy.
2. Compute the resume command from the agent backend - the same table the
   `cdir` resume hint uses, overridable per machine in
   `~/.config/ctools/resume-commands.json` (see below).
3. `cd` into the session's recorded working directory.
4. `exec` the command via `sh -c`, so a spaced argument is never word-split
   by a bare `exec $(…)`.

The `cd` in step 3 is what makes it work: `claude --resume <id>` only finds a
session from the directory it was recorded in. Resuming from the wrong
directory is the "No conversation found with session ID" root cause - see
[ccopy-claude-resume-notes.md](ccopy-claude-resume-notes.md). If the recorded
directory no longer exists, ctools warns and resumes from the current
directory instead.

## Print it without exec-ing

`cdir --resume-command AGENT/SESSION` prints the same command (with the `cd`
prefix) and exits, without running it. It takes exactly the shapes `resume`
takes. Same code path as `resume`, minus the `exec` - use it to copy the
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
