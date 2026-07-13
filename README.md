# ctools

Context tools for LLM conversations. Extracted from [Gab n' Go](https://github.com/day50-dev/gabngo). Named after [GNU mtools](https://www.gnu.org/software/mtools/), which does the same thing for DOS floppies because your context window is about the size of a DOS-floppy. Maybe we can use that for inspiration.

## The Problem

You talk to LLMs all day. Over weeks, you build up a set of constraints, preferences, and goals. "Use C17 standard." "Prefer snake_case." "Always check for null returns." These things live in your conversations as system messages. They are valuable. They are also trapped.

Say you have been working with opencode for a month. You have refined your coding style through dozens of sessions. Now you start a new Claude Code project and you want those same preferences. You could copy them by hand. Or you could use ctools.

```sh
ccopy @opencode/ses_abc123 preferences.json
ccopy preferences.json @claude-code/ses_xyz
```

Or skip the file entirely:

```sh
ccopy @opencode/ses_abc123 @claude-code/ses_xyz
```

The concepts are embedded in your conversations as "Use the following <type>: <text>" messages. ctools reads and writes these. Your context travels with you.

| GNU mtools | ctools | Does what |
|------------|--------|-----------|
| `mdir` | `cdir` | List sessions |
| `mcopy` | `ccopy` | Copy concepts |
| `mdu` | `cdu` | Token usage |
| `mtype` | `cgrep` | Search content |

## Tools

### ccopy

Extract, inject, and copy concepts between sessions and files. The `@` prefix means "this is a session reference." Plain paths are files.

```sh
ccopy @opencode/ses_abc123 constraints.json     # session to file
ccopy constraints.json @opencode/ses_abc123     # file to session
ccopy @opencode/ses_abc123 @claude-code/ses_xyz # session to session
ccopy {a,b}.json @opencode/ses_abc123           # shell expansion works
```

### cdir

Lists sessions. Think `ls` for your conversation history.

```sh
cdir --agents                  # what agents do we know about
cdir opencode/                 # sessions for opencode
cdir claude-code/              # sessions for claude code
cdir -R                        # all agents, recursive
cdir opencode/ses_abc123       # export a session as JSON
```

Sort by time (`-t`), size (`-s`), reverse (`-r`). Output as json, xml, or markdown with `-f`.

### cgrep

Searches conversation content. Regex supported. Works across agents.

```sh
cgrep "pattern" "opencode/*"
cgrep -i "error" "claude-code/"
cgrep -c "def " "opencode/"              # count per session
cgrep -C 2 "exception" "claude-code/"    # context lines
cgrep "TODO" "opencode/" "claude-code/"  # multiple agents
```

Flags: `-l` list files, `-c` count, `-v` invert, `-i` case-insensitive, `-A/-B/-C` context.

### cdu

Token usage. Like `du` but for context windows. Uses tiktoken for accurate counts.

```sh
cdu                           # total across all agents
cdu opencode/                 # sessions by token count
cdu opencode/ses_abc123       # breakdown by role
cdu --json opencode/          # machine-readable
```

For opencode, it reads actual input/output tokens from the database. For other agents, it counts with tiktoken from the conversation content.

## Supported Agents

| Agent | Format | Storage |
|-------|--------|---------|
| claude | JSON | `~/Library/Application Support/Claude-3p/` |
| claude-code | JSONL | `~/.claude/` |
| opencode | SQLite | `~/.local/share/opencode/` |
| codex | JSONL | `~/.codex/` |

## MCP Server

There is an MCP server for use from Claude, opencode, Cursor, or anything else that speaks MCP.

Tools: `list_agents`, `list_sessions`, `search_sessions`, `export_session`, `extract_concepts`, `copy_concepts`, `get_session_concepts`.

Add to your MCP config:

```json
{
  "mcpServers": {
    "ctools": {
      "command": "python",
      "args": ["/ABSOLUTE/PATH/TO/ctools/ctools_mcp.py"]
    }
  }
}
```

## Installation

```sh
pip install -r requirements.txt
```

## Library

Works as a Python library too.

```python
from ctools.lib import AGENTS, get_formatter
from ctools.cdir import get_opencode_sessions
from ctools.cgrep import grep_session
from ctools.ccopy import extract_concepts_from_messages, inject_concepts_to_session
from ctools.cdu import count_tokens, get_session_tokens
```
