# ctools

CLI tools for browsing and searching LLM agent conversations. Extracted from [Gab n' Go](https://github.com/day50-dev/gabngo). Inspired by [GNU mtools](https://www.gnu.org/software/mtools/) — same idea, but for LLM context windows instead of DOS floppies.

## Why ctools?

Your conversations with LLMs contain valuable constraints, preferences, and goals you've established over time. ctools lets you **extract those concepts** from any session and **reuse them** across agents and sessions. For example:

1. You've been working with opencode for weeks, refining your coding style preferences
2. Run `ccopy @opencode/ses_abc123 my_preferences.json` to extract those preferences
3. Start a new Claude Code session and inject them: `ccopy my_preferences.json @claude-code/ses_xyz`
4. Or copy directly between sessions: `ccopy @opencode/ses_abc123 @claude-code/ses_xyz`

The concepts (constraints, goals, preferences, observations, references) are embedded in your conversations as "Use the following" system messages. ctools reads and writes these, so your hard-won context travels with you.

| GNU mtools | ctools | What it does |
|------------|--------|--------------|
| `mdir` | `cdir` | List directory/sessions |
| `mcopy` | `ccopy` | Copy files/concepts |
| `mdu` | `cdu` | Disk usage/token count |
| `mtype` | `cgrep` | View/search content |

## Tools

### ccopy — copy concepts between sessions

Extract, inject, and copy concepts between agent sessions and concept files. Uses `@` prefix for session references.

```sh
# Extract concepts from a session to a file
ccopy @opencode/ses_abc123 constraints.json

# Inject concepts from files into a session
ccopy constraints.json preferences.json @opencode/ses_abc123

# Copy concepts between sessions
ccopy @opencode/ses_abc123 @claude-code/ses_xyz

# Shell expansion works
ccopy {constraints,preferences}.json @opencode/ses_abc123
```

### cdir — ls for LLM context windows

Lists agents and their conversation sessions, showing metadata like creation time, modification time, size, and message count. You can also export individual sessions as JSON in the llcat conversation format.

```sh
cdir --agents                  # List all known agents
cdir opencode/                 # List sessions for opencode
cdir claude-code/              # List sessions for Claude Code
cdir codex/                    # List sessions for Codex

cdir opencode/ -t              # Sort by time
cdir opencode/ -s              # Sort by size
cdir opencode/ -t -r           # Reverse sort order

cdir -R                        # List all agents' sessions with agent name
cdir -a                        # List supported agents

cdir opencode/ses_abc123       # Export a specific session as JSON
```

Options: `-t` (sort by time), `-s` (sort by size), `-r` (reverse), `-R` (recursive), `-a` (list agents), `-f json|xml|md` (output format).

### cgrep — grep for LLM context windows

Searches conversation content across agents. Reads the actual session data from each agent's storage format and applies PCRE regex patterns.

```sh
cgrep "pattern" "opencode/*"                # Search all opencode sessions
cgrep "import os" "opencode/"               # Search for imports
cgrep -l -i "error" "claude-code/"          # List files with matches (case-insensitive)
cgrep -c "def " "opencode/"                 # Count matches per session
cgrep -v "test" "opencode/"                 # Invert match (exclude pattern)
cgrep -C 2 "exception" "claude-code/"       # Show 2 lines of context around matches
cgrep "TODO" "opencode/" "claude-code/"     # Search multiple agents
cgrep -B2 -A2 "FIXME" "opencode/ses_abc123" # Context around specific session matches
```

Flags:
- `-l` / `-L` — list files with/without matches
- `-c` — count matches per file
- `-v` — invert match
- `-i` — case-insensitive
- `-A N` — show N lines after match
- `-B N` — show N lines before match
- `-C N` — show N lines before and after
- `-f json|xml|md` — output format

### cdu — context disk usage

Shows token length of conversations. Uses tiktoken for accurate counts (cl100k_base encoding), with automatic fallback to character-based estimation.

```sh
cdu                             # Total token usage across all agents
cdu opencode/                   # Sessions sorted by token count
cdu opencode/ses_abc123         # Token breakdown (input/output)
cdu claude-code/                # Sessions with estimated tokens
cdu --json opencode/            # JSON output
```

For opencode sessions, cdu reads actual `tokens_input` and `tokens_output` from the database. For other agents, it counts tokens using tiktoken from the conversation content, broken down by role (user/assistant/system).

## Supported Agents

| Agent | Description | Format | Storage |
|-------|-------------|--------|---------|
| claude | Claude Desktop (Anthropic) | JSON | `~/Library/Application Support/Claude-3p/` |
| claude-code | Claude Code CLI | JSONL | `~/.claude/` |
| opencode | opencode CLI | SQLite | `~/.local/share/opencode/` |
| codex | OpenAI Codex CLI | JSONL | `~/.codex/` |

## MCP Server

ctools includes an MCP server for searching and managing conversations from any MCP-compatible client (Claude, opencode, Cursor, etc.).

### Tools

| Tool | Description |
|------|-------------|
| `list_agents` | List all supported agents and installation status |
| `list_sessions` | List sessions for an agent (sort by time or size) |
| `search_sessions` | Search conversation content with regex across all agents |
| `export_session` | Export messages from a session |
| `extract_concepts` | Extract concepts from a session |
| `copy_concepts` | Copy concepts between sessions or concept files |
| `get_session_concepts` | Get concepts with optional type filtering |

### Setup

Add to your MCP client config (e.g. `~/.config/opencode/config.json` or Claude Desktop config):

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

### Usage from CLI

```sh
# Test the server directly
echo '{"jsonrpc":"2.0","method":"tools/list","id":1}' | python ctools_mcp.py
```

## Installation

```sh
pip install -r requirements.txt
```

## Library Usage

Importable as a Python library:

```python
from ctools.lib import AGENTS, get_formatter
from ctools.cdir import get_opencode_sessions
from ctools.cgrep import grep_session
from ctools.ccopy import extract_concepts_from_messages, inject_concepts_to_session
from ctools.cdu import count_tokens, get_session_tokens
```
