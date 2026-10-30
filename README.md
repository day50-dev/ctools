# ctools

<p align="center">
<img width="500" alt="ctools" src="https://github.com/user-attachments/assets/ee781bbe-6364-44ed-be2d-72114f1e6d8a" /><br/>
<a href=https://pypi.org/project/ctxttools><img src=https://badge.fury.io/py/ctxttools.svg/></a>
<br/><br/><b>Try it now</b><br/>
<code>uvx --from ctxttools cdir</code><br/>
<code>uvx --from ctxttools ctools-web</code>
<br/><br/><b>Then install it</b><br/>
<code>uv tool install ctxttools</code>
</p>

------
**ctools** is a suite of simple tools for navigating through the sessions of the most popular LLM agents and coding tools. It allows you to search your history, transfer conversations, filter them, and even share them among machines and users.

It supports [Claude Desktop](https://claude.com/download), [Claude Code](https://github.com/anthropics/claude-code), [Codex](https://github.com/openai/codex), [Pi](https://github.com/earendil-works/pi), [Hermes](https://github.com/NousResearch/hermes-agent), [Goose](https://github.com/aaif-goose/goose), [Kilo](https://github.com/Kilo-Org/kilocode), [FreeBuff](https://github.com/CodebuffAI/freebuff), [Cline](https://github.com/cline/cline), [omp](https://github.com/can1357/oh-my-pi), and [OpenClaw](https://openclaw.ai/).

You can use it from the terminal, from a small web dashboard, or from any AI coding agent (via an MCP server).

Here's an example:

> I look for mentions of snake game in opencode using cgrep
<pre>
$ <b>cgrep</b> "snake game" opencode
opencode/ses_a1d1a5a4df984ee698cab4a7:1:user: make a simple snake game in python
opencode/ses_a1d1a5a4df984ee698cab4a7:2:assistant: I'll make a terminal-based snake game using `curses` (standard library, no dependencies).
opencode/ses_a1d1a5a4df984ee698cab4a7:3:assistant: Done — `snake.py` is a terminal snake game using Python's built-in `curses` module (no dependencies).
</pre>

> cool let's look at the status of that session using cdir
<pre>
$ <b>cdir</b> -l opencode/ses_a1d1a5a4df984ee698cab4a7
  ID                            NAME                                MODIFIED            SIZE  MSGS  PATH
  ses_a1d1a5a4df984ee698cab4a7  make a simple snake game in python  2026-10-07 16:56  2.7 KB     3  /home/chris/.local/share/opencode

  1 session(s)
</pre>

> Alright let's copy that session from opencode into pi!
<pre>
$ <b>ccopy</b> opencode/ses_a1d1a5a4df984ee698cab4a7 pi
Copied 3 message(s) from opencode/ses_a1d1a5a4df984ee698cab4a7 to a new pi session: e357f435-f813-463a-b313-f97f1051f3cb
Resume it with:
  pi --session e357f435-f813-463a-b313-f97f1051f3cb
</pre>

As you can see, these are unix-friendly tools using simple paradigms that you are already familiar with.

## What it can do

| Capability | What you get |
|---|---|
| **Discover** | See every agent installed on your machine and how many sessions each holds. |
| **Find** | Search every session at once with grep-style regex, narrowed by agent, session glob, or role. |
| **Read** | Print a conversation as JSON (pipe it anywhere) or as a readable transcript. |
| **Continue** | Get the exact command to resume a session in its own tool, or open it in the web UI. |
| **Copy** | Copy a conversation into a new session in another tool, or the same tool on another machine. |
| **Measure** | See token usage per agent, per session, and per role. |
| **Concepts** | Pull out the *intent* of a session: its goal, its constraints, and what was decided. |
| **Recover** | List and restore the storage backups ctools takes before every local write. |

---

## A quick tour

**The overview**: every tool ctools can see, with session counts and token
totals, plus quick links into the most common actions:

![Overview](docs/screens/overview.png)

**Sessions**: browse every conversation from any tool, sorted by recency.
Click one to open it.

![Sessions](docs/screens/sessions.png)

**A conversation**: the full message stream, with the original tool's
resume command and copy-to-another-tool options.

![Conversation](docs/screens/conversation.png)

**Search**: type a phrase and see every matching message across all tools.

![Search](docs/screens/search.png)

**Concepts**: ask a session what it's *about*. ctools extracts the intent,
constraints, and decisions so you can pick up where you left off without
re-reading everything.

![Concepts](docs/screens/concepts.png)

---

## Install

**Python 3.10+** is required.

```bash
uv tool install ctxttools        # or: pip install ctxttools
```

That gives you the `ctools` command, a short command for each of its verbs,
and the dashboard and MCP server:

| Command | Purpose |
|---|---|
| `ctools` | The front door: `ctools <verb> [args...]`. Run `ctools --help` for the list. |
| `cdir`, `cgrep`, `ccat`, `cdu`, `ccopy`, `cextract`, `cconnect`, `crm`, `crecover` | Shortcuts for the verbs (`cgrep` is `ctools grep`). |
| `ctools-web` | The dashboard, same as `ctools web`. |
| `ctools-mcp` | The MCP server, same as `ctools mcp`. Needs the `mcp` extra. |

The MCP server's dependency is optional:

```bash
uv tool install "ctxttools[mcp]"   # or: pip install "ctxttools[mcp]"
```

From a local checkout, `uv` keeps it editable:

```bash
uv tool install --editable ".[mcp]"
```

> **Heads up:** `ctools-mcp` needs `mcp<2` (see
> [mcp dependency](#mcp-dependency) below).

---

## The CLI

`ctools` has one verb per job. Each verb is also installed as its own short
command, named `c` plus the verb, so `ctools grep` and `cgrep` are the same
program with the same flags. Use whichever you prefer: the long form is easier
to discover, and the short form is quicker to type.

```text
$ ctools --help
usage: ctools <command> [args...]

query:
  dir       list agents and sessions           (cdir)
  grep      search session content             (cgrep)
  cat       print a session                    (ccat)
  du        context usage per session          (cdu)

transform:
  copy      copy a session to another agent    (ccopy)
  extract   extract concepts from a session    (cextract)
  connect   pipe concepts between sessions     (cconnect)
  rm        cut sections out of a session      (crm)
  recover   restore from a backup              (crecover)

long-running:
  web       run the web dashboard              (ctools-web)
  mcp       run the MCP server                 (ctools-mcp)

Run 'ctools <command> --help' for a command's options.
```

Each verb has its own page in [`docs/`](docs/): [dir](docs/cdir.md),
[grep](docs/cgrep.md), [cat](docs/ccat.md), [du](docs/cdu.md),
[copy](docs/ccopy.md), [extract](docs/cextract.md),
[connect](docs/cconnect.md), [rm](docs/crm.md), and
[recover](docs/crecover.md). Sessions are addressed as `agent/session_id`
throughout.

### Discover your agents and sessions

```bash
ctools dir                      # every known agent, and whether it's installed
ctools dir opencode             # that agent's sessions, and the command that resumes one
ctools dir -l opencode          # with modified time, size, message count, path
ctools dir .                    # sessions that ran in this directory, across all agents
ctools dir opencode/ses_abc     # one session's row
```

### Find a session

`grep` works the way `grep` does, over conversation content instead of files.

```bash
ctools grep "bug report"                    # every installed agent
ctools grep -i "deploy" claude-code         # one agent
ctools grep -C 2 "flaky test" opencode      # with context
ctools grep -l "parser" pi                  # just the session ids
ctools grep --user "refactor"               # only what you typed
ctools grep --hosts "mcp config"            # also the hosts in ctools.toml
```

### Read a session

```bash
ctools cat opencode/ses_abc           # JSON list of {role, content}
ctools cat --text opencode/ses_abc    # a readable transcript
ctools cat ssh://me@box/pi/<id>       # a session on another machine
```

### Copy a session to another tool

`copy` creates a **new** session in the destination. The source is never
touched, and ctools prints the command that resumes the copy.

```bash
ctools copy opencode/ses_abc claude-code          # into another tool
ctools copy opencode/ses_abc codex --dry-run      # preview first
ctools copy opencode/ses_abc ssh://me@box/codex   # into a tool on another machine
ctools copy opencode/ses_abc ssh://me@box/        # into opencode on that machine
ctools copy ssh://me@box/pi/<id> claude-code      # pull one in from another machine
```

The remote side doesn't need ctools installed; plain `ssh` and `tar` will do.
See [docs/ccopy.md](docs/ccopy.md) for how that works and where the new
session lands.

### Token usage

```bash
ctools du                     # total across all agents
ctools du opencode            # sessions by token count
ctools du opencode/ses_abc    # breakdown by role
```

---

## The web dashboard

```bash
ctools web                    # opens at http://127.0.0.1:8765
ctools web --port 9000        # a different port
```

`ctools-web` is the same command. The dashboard is a read-mostly surface over
the same data the CLI uses. You can:

- Browse and search sessions (the "Conversation" view mirrors the CLI).
- Copy a session to another tool right from the UI.
- Export/download a session.
- Run **concept extraction** on a session (with optional LLM-backed
  strategies and filters).

It binds to `127.0.0.1` only by default, so it's not exposed to your
network. Use `--host` to change that deliberately.

---

## Concepts: extract the intent of a session

A raw session is thousands of tokens. **Concepts** distills it into the few
things that matter for picking it back up: the goal, the constraints, and
what was decided.

```bash
ctools extract @<agent>/<session-id>      # or: cextract @<agent>/<session-id>
```

Concepts are extracted one of three ways. Regex is the default; `-s` and `-F`
select the others:

1. **Regex (default)**: built-in patterns for goal, decision, constraint,
   and preference. No setup.
2. **LLM strategy**: you define a chunking strategy that an LLM applies to
   the session. Best for richer, contextual extraction.
3. **Filter**: narrow the regex/LLM output with a rule (drop boilerplate,
   keep only certain types, etc.).

Strategies and filters are stored as JSON config files that ctools reads at
runtime, in the config dir (see below). Create them in the dashboard (the
**Concepts** tab) or by hand.

**Where ctools keeps its config.** Config and data live in a per-user
directory resolved via `platformdirs` (no hand-rolled platform checks):

- Linux: `~/.config/ctools/` (config), `~/.local/share/ctools/` (data)
- macOS: `~/Library/Application Support/ctools/` (the macOS convention)
- Windows: `%APPDATA%\ctools\` (config), `%LOCALAPPDATA%\ctools\` (data)

Set `$CTOOLS_CONFIG` (and `$CTOOLS_DATA`) to relocate. This holds
`resume-commands.json`, `strategies/`, `filters/`, and the general
**`ctools.toml`** config — one file, multiple sections.  The first section,
`[hosts]`, lists the machines `ctools grep --hosts` searches too:

```toml
# ~/.config/ctools/ctools.toml
[hosts]
fatty  = "ssh://bobby@fatty.local"
workie = "ssh://10.0.0.4"
```

Each value is an `ssh://[user@]host[:port]` reference (the same form `ccat`
and `ccopy` use for a remote side), so the host may be a `~/.ssh/config`
alias.  A missing/malformed config never breaks a search — it just means
"local only".

The dashboard shows the full extraction pipeline (conversation → extraction
→ filter → concepts) so you can see exactly what was kept and why.

---

## MCP server

The MCP server exposes the same capabilities to any MCP-capable coding agent
(Pi, Claude, OpenCode, etc.). It needs the `mcp` extra (see [Install](#install)).

```bash
ctools mcp          # run the server on stdio (same as ctools-mcp)
```

### Install and register with Pi

```bash
ctools-mcp install            # global: ~/.pi/agent/mcp.json
ctools-mcp install --project  # project: .pi/mcp.json in the current directory
```

If no working `ctxttools[mcp]` install is found, this installs one with
`uv tool`. It then registers the server in `~/.pi/agent/mcp.json` (or
`.pi/mcp.json` with `--project`), always with a `python -m ctools.ctools_mcp`
entry that does not depend on the MCP host's `PATH`. Verify with:

```bash
pi mcp list        # "ctools-mcp … connected"
ctools-mcp check   # also: verifies the command exists and starts, prints the fix
```

`ctools-mcp uninstall` removes the entry again (`--all` also removes it from
Claude Code's and opencode's configs).

### Manual registration (any host)

If you'd rather wire it up yourself, `print-config` emits the `mcpServers` JSON
snippet:

```bash
ctools-mcp print-config
```

Add that to your host's MCP config. The server is started with
`python -m ctools.ctools_mcp` from the install environment.

### Tools exposed

Session tools take a single `session` reference: `agent/session_id` (the
`@` prefix also works), or a bare `session_id` plus the `agent` argument.

| Tool | What it does |
|---|---|
| `list_agents` | Agents available and whether they're installed. |
| `list_sessions` | Recent sessions for one agent. |
| `search_sessions` | Find sessions by pattern or phrase. |
| `summarize_session` | Quick orientation: title, date, size, first user and last assistant message. |
| `export_session` | Get the messages of a session. |
| `extract_concepts` | Extract 'Use the following …' concept blocks from a session. |
| `get_session_concepts` | Get a session's concepts, optionally of one type. |
| `copy_concepts` | Copy extracted concepts into a destination session. |

---

## MCP dependency

`ctools-mcp` pins `mcp>=1.2,<2.0`. The `mcp` package 2.x renamed
`FastMCP` to `MCPServer`, which breaks the v1 API ctools uses. If a fresh
install pulls `mcp>=2` and `ctools-mcp` won't start, pin the major version
(`pip install "mcp<2"`); the released package already does this.

---

## The agents ctools knows about

| Name | Tool | Storage |
|---|---|---|
| `claude` | Claude Desktop | JSON |
| `claude-code` | Claude Code CLI | JSONL |
| `codex` | OpenAI Codex CLI | JSONL |
| `pi` | Pi coding agent | JSONL |
| `omp` | omp (oh-my-pi) | JSONL |
| `opencode` | OpenCode CLI | SQLite |
| `kilo` | Kilo CLI | SQLite |
| `goose` | Goose AI agent | SQLite |
| `hermes` | Hermes agent | SQLite |
| `openclaw` | OpenClaw | SQLite |
| `cline` | Cline | JSON |
| `freebuff` | Freebuff | JSON |

An agent is "installed" if ctools can find its session directory (or DB
file); `ctools dir` shows which are found and where. Only claude-code,
opencode, kilo, codex, and pi can be a `copy` destination.

---

## How the data is handled

- **Read-only by default.** `dir`, `grep`, `cat`, `du`, the MCP read tools,
  and the dashboard's read paths never modify your session files.
- **Writes are explicit.** `copy` writes a *new* session and never touches the
  source. `extract` and `connect` write only when you name a session as the
  destination, and `rm` only to the session you name.
- **Backed up first.** Before a local `copy` writes, it snapshots the
  destination agent's storage and restores it automatically if the write goes
  wrong. `ctools recover` lists and restores those snapshots.
- **Local unless you say otherwise.** The dashboard binds to `127.0.0.1`, and
  the MCP server reads your local storage. Data only crosses the network when you
  name an `ssh://` host or pass `grep --hosts`, and then only over your own ssh.

---

## Development

```bash
git clone https://github.com/day50-dev/ctools
cd ctools
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

The code is deliberately simple: each agent adapter is a class in
`ctools/agents.py`, each verb is one module (`ctools/cgrep.py`, …), and
`ctools/dispatch.py` routes `ctools <verb>` to them. The CLI, dashboard, and
MCP layers are thin over the shared `ctools.agents` registry.

---

## License

[MIT](LICENSE)
