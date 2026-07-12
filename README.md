# ctools

CLI tools for browsing and searching LLM agent conversations. Extracted from [Gab n' Go](https://github.com/day50-dev/gabngo).

## Tools

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

## Supported Agents

| Agent | Description | Format | Storage |
|-------|-------------|--------|---------|
| claude | Claude Desktop (Anthropic) | JSON | `~/Library/Application Support/Claude-3p/` |
| claude-code | Claude Code CLI | JSONL | `~/.claude/` |
| opencode | opencode CLI | SQLite | `~/.local/share/opencode/` |
| codex | OpenAI Codex CLI | JSONL | `~/.codex/` |

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
```
