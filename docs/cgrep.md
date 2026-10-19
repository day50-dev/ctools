# cgrep

`cgrep` searches conversation content across every session you have. Regex in,
matches out. Works across all agents — this is the tool the whole suite is named
for.

With no path argument (or `*`, or `-a`) it searches **every installed agent** at
once. Name a path to narrow to one agent, one session, or a glob:

```sh
cgrep "pattern"                           # every installed agent
cgrep -a "error"                          # same, explicit flag
cgrep -i "error" "claude-code/"           # only one agent
cgrep "def " "opencode/" "claude-code/"   # multiple named agents
cgrep -c "import" "opencode/"             # count per session
cgrep -C 2 "exception" "claude-code/"     # context lines
cgrep -h "TODO" "opencode/ses_abc123"     # drop the session path prefix
cgrep -o -w "foo" "opencode/"             # whole-word hits, matched text only
cgrep -q "needle" "opencode/"             # exit code only, like grep -q
cgrep -m 3 "retry" "opencode/"            # stop after 3 hits per session
cgrep -F "[ERROR]" "opencode/"            # fixed string, no regex
cgrep --include "ses_abc*" "err" "opencode/"  # only matching session IDs
```

Matches stream to the terminal one line at a time, as each session is searched —
like `grep` over a slow directory, not a buffered dump.

## Flags

`-l` list files, `-L` sessions without matches, `-c` count, `-Z`/`--null` NUL
byte after each session ID (feeds `xargs -0`; works with `-l`/`-L`/`-c`),
`-v` invert, `-i`
case-insensitive, `-A/-B/-C` context, `-h`/`-H` filename prefix, `-q` quiet (exit
code only), `-m N` max matches per session, `-o` only the matched text, `-w`
whole word, `-x` whole line, `-E` extended/POSIX regex (default), `-F` fixed
string, `--include`/`--exclude` session ID globs, `--exclude-from FILE`
same globs from a file (one per line, `#` comments ignored), `-a`/`--all`
search every installed agent.

Count and list modes (`-c`, `-l`, `-L`, `--format`) still buffer, since they need
the whole picture — same as real `grep`.

## Roles

By default cgrep searches the **conversation** — the `user` and `assistant`
turns, which is what `ccat` and the web UI show. To search a specific message
role, or to reach content the conversation hides (tool traffic, the system
prompt), narrow by role:

```sh
cgrep --tool "Traceback" "pi/*"      # only tool results
cgrep --system "You are" "claude-code/*"   # the system prompt
cgrep --user "refactor" "*"          # only what the user said
cgrep --role user,assistant "x"      # same as the default conversation
cgrep --user --assistant "x"         # the sugar flags can be combined
```

`--user` / `--assistant` / `--system` / `--tool` are sugar for `--role`. Pass
`--role` repeatedly (or comma-separated) to search several roles at once. With
no role flag, cgrep searches the conversation (`user` + `assistant`), exactly as
before — the role machinery is only engaged when you ask for it.

A role filter switches the search to the agent's **full** message stream (all
roles), so line numbers in `--tool` / `--system` output count every line and may
differ from the default. Coverage varies by agent: pi exposes its tool results
(via `--tool`), and Claude Code's tool calls, tool results and system entries are
all reachable; agents whose storage doesn't record a role as a message simply
yield nothing for that role.

## Exit status

grep's: `0` if a match was found, `1` if none, `2` on error (bad pattern, missing
agent) — errors beat matches. `-q` prints nothing and still reports the status.

## Output format

Output is grep-shaped: every line is prefixed with the session it came from, so
hits stay locatable:

```sh
$ cgrep "import" "opencode/*"
opencode/ses_abc123:17:user: from pathlib import Path
opencode/ses_abc123-18-assistant: That should work.
--
opencode/ses_def456:4:user: import sqlite3
```

`-h` drops the prefix (single-session look), `-H` forces it back on. Context lines
use grep's `-` separator instead of `:`. Sessions are separated by `--`, as in
grep.

## Piping

`-l` prints one `agent/session_id` per line, which is exactly what `cdir` and
`ccat` accept as arguments. Pipe it to list the matches or dump them:

```sh
cgrep -l ctool opencode | xargs cdir -l   # the matching sessions, as a table
cgrep -l ssl  opencode   | xargs ccat      # the matching conversations, full
```

Both `cdir` and `ccat` take many session references at once, so the pipeline works
regardless of how many sessions match.

## Related

- To list your sessions without searching, use [cdir](cdir.md).
- To read a conversation, use [ccat](ccat.md).
- To move a conversation to another agent, use [ccopy](ccopy.md).
