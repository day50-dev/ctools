# cdir

`cdir` lists sessions. Think `ls` for your conversation history. Subagents appear
indented under their parent with tree connectors. Run it with no argument to see
which agents are installed and where they store data.

```sh
cdir                        # list all known agents
cdir opencode/              # sessions for opencode (name only)
cdir -l opencode/           # sessions with modified date, size, message count, path
cdir -S opencode/           # sessions biggest first (ls -S); -t sorts by time
cdir -u opencode/           # sessions newest first by creation time (ctime)
cdir -1 opencode/           # one session ID per line, no header
cdir claude-code/           # sessions for claude code
cdir pi/                    # sessions for pi coding agent
cdir -R                     # all agents, recursive
cdir opencode/ses_abc123    # export a session as JSON
cdir opencode/*llcat*       # filter: glob on session id, name, or working dir
```

## Sorting

Sort flags are `ls`-style: `-t` by time (default), `-S` by size, `-u` by creation
time. When you give several, the last one wins (`cdir -St opencode/` is the same
as `cdir -S opencode/`). Add `-r` to reverse.

## Filtering sessions

Put a glob in the session part of the reference to filter instead of export.
The pattern is matched (case-insensitively) against the session **id**, the
session **name** (the one-line description), and the session **working
**directory** — a match on any of them is shown:

```sh
$ cdir opencode/*llcat*
  ID                                 NAME
  ses_08b381916ffe9t5oIgzE6g45vb     Petsitter exportit llcat JSON export
  ses_13263a5edffeFNJ7tidEfj90ze     Add Ollama transport option to llcat.py
```

A reference with no wildcards (`cdir opencode/ses_abc123`) is still exported as
JSON; only a pattern with `*`, `?`, or `[` switches to filtering. Combine with
`-l`, `-S`, `-1`, etc. as usual.

## One-line output

`-1` prints bare session IDs, one per line — shell-script friendly, no header, no
tree:

```sh
$ cdir -1 opencode/
ses_08b4ab356ffeQvmBXnu1oj4Gqe
ses_08b74487fffeTmQzA810dE9WRV
```

## Color

`--color` controls ANSI bold on the header: `auto` (default, on only when stdout
is a terminal), `always`, `never`.

## Long format

`-l` adds a header and shows the modified date, size, message count, and source
path at the end of each row:

```sh
$ cdir -l opencode/
  Source: /home/chris/.local/share/opencode/opencode.db

  ID                                NAME                          MODIFIED              SIZE    MSGS  PATH
  ses_08b4ab356ffeQvmBXnu1oj4Gqe    Add -l option to cdir ...     2026-08-08 16:33   53.7 KB     23  /home/chris/day50/ctools
```

`PATH` is the session's working directory (where the conversation's code lives),
not the storage file.

## Column selection

`-o` selects which columns to print, ps-style. Pass `cdir -o help` for the full
field reference:

```sh
cdir -o help                     # document all available fields
cdir -o id,name,mtime opencode/  # only those columns
cdir -o id,model,path opencode/  # reorder columns any way you like
```

Available fields for `-o`:

| Field | Label | Meaning |
|-------|-------|---------|
| `id` | `ID` | Session identifier |
| `name` | `NAME` | Session title (or ID prefix when no title is set) |
| `ctime` | `CREATED` | Creation / start time |
| `mtime` | `MODIFIED` | Last modification time |
| `size` | `SIZE` | Size: token count for opencode, bytes for file-based agents |
| `msgs` | `MSGS` | Number of messages in the session |
| `model` | `MODEL` | Model used for the session |
| `path` | `PATH` | Session working directory (where the conversation's code lives) |
| `parent` | `PARENT` | Parent session ID (present on subagent sessions) |

Default fields are `id,name`; `-l` expands to `id,name,mtime,size,msgs,path`.
Only the last-modified date is shown — the creation date is suppressed.

## Agent listing

With no path, `cdir` shows Found/Not Found with actual paths:

```
Found:
  AGENT         DESCRIPTION                 PATH
  claude-code   Claude Code CLI             ~/.claude/projects/
  opencode      Opencode CLI                ~/.local/share/opencode/opencode.db
  kilo          Kilo CLI                    ~/.local/share/kilo/kilo.db

Not Found:
  claude        Claude Desktop (Anthropic)  ~/.config/Claude/conversations/
  codex         OpenAI Codex CLI            ~/.codex/sessions/
  pi            Pi Coding Agent             ~/.pi/agent/sessions/
  goose         Goose AI agent              ~/.local/share/goose/sessions/sessions.db
  hermes        Hermes agent                ~/.hermes/state.db
  cline         Cline (cline.bot)           ~/.cline/data/tasks/
  omp           omp (oh-my-pi)              ~/.omp/agent/sessions/
  freebuff      Freebuff (Codebuff)         ~/.config/freebuff/projects/
```
