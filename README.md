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

Extract, inject, and copy concepts between sessions and files. The `@` prefix means "this is a session reference." Plain paths are files. Directories get one file per concept.

```sh
ccopy @opencode/ses_abc123 concepts/              # extract to directory (one file per concept)
ccopy @opencode/ses_abc123 constraints.json       # extract to single file
ccopy concepts/ @opencode/ses_abc123               # inject all concepts from directory
ccopy constraints.json @opencode/ses_abc123       # inject from file
ccopy @opencode/ses_abc123 @claude-code/ses_xyz   # session to session
ccopy --strategy my-strategy.json @opencode/ses_abc123 concepts/  # custom extraction
```

When you extract to a directory, each concept becomes its own file. This is the core abstraction: the directory *is* the concept set. rm a file to exclude it. cp files in to merge. Edit the json to modify. `git add .` to share.

```sh
ls concepts/
constraint_0aa712d89fbb067a.json
preference_a1b2c3d4e5f6g7h8.json
observation_x9y8z7w6v5u4t3s2.json
```

Strategies let you define how concepts are extracted using an LLM. Ontology is contestable, so different strategies produce different chunkings:

```json
{
  "host": "http://localhost:11434",
  "model": "qwen2.5:3b",
  "api_key": null,
  "prompt": "Extract the key concepts from this conversation..."
}
```

### cdir

Lists sessions. Think `ls` for your conversation history.

```sh
cdir                        # list all known agents
cdir opencode/              # sessions for opencode
cdir claude-code/           # sessions for claude code
cdir -R                     # all agents, recursive
cdir opencode/ses_abc123    # export a session as JSON
```

Output shows Found/Not Found with actual files when available:

```
Found:
  Claude Code  Claude Code CLI             ~/.claude                [jsonl]
  Opencode     Opencode CLI                ~/.local/share/opencode  account.json, auth.json, opencode.db

Not Found:
  Claude       Claude Desktop (Anthropic)  ~/.config/Claude         [json]
  Codex        OpenAI Codex CLI            ~/.codex                 [jsonl]
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
