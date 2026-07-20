<p align="center">
<img width="500" alt="ctools" src="https://github.com/user-attachments/assets/ee781bbe-6364-44ed-be2d-72114f1e6d8a" /><br/>
<a href=https://pypi.org/project/ctxttools><img src=https://badge.fury.io/py/ctxttools.svg/></a>
</p>

Memory tools for LLM conversations. Extracted from [Gab n' Go](https://github.com/day50-dev/gabngo). Named after [GNU mtools](https://www.gnu.org/software/mtools/), which does the same thing for DOS floppies because your context window is about the size of a DOS-floppy. Maybe we can use that for inspiration.

Even without that complication `cdir` is a game-changer alone. Because of that we document it up-front.

```shell
$ cdir opencode
  Source: /home/chris/.local/share/opencode/opencode.db

  ses_08b4ab356ffeQvmBXnu1oj4Gqe  Add -l option to cdir for date and size display
  ┗━ ses_08b4a7d32ffeEyXT42LTDsIg7s  Explore cdir implementation (@explore subagent)
  ses_08b74487fffeTmQzA810dE9WRV  Add -f option to override SSL errors
```

Now I can easily resume those sessions. 

### cdir

Lists sessions (endpoints). Think `ls` for your conversation history. Subagents appear indented under their parent with tree connectors.

```sh
cdir                        # list all known agents
cdir opencode/              # sessions for opencode (name only)
cdir -l opencode/           # sessions with dates, size, message count
cdir claude-code/           # sessions for claude code
cdir -R                     # all agents, recursive
cdir opencode/ses_abc123    # export a session as JSON
```

Output shows Found/Not Found with actual paths:

```
Found:
  Claude Code  Claude Code CLI             ~/.claude/projects/
  Opencode     Opencode CLI                ~/.local/share/opencode/opencode.db

Not Found:
  Claude       Claude Desktop (Anthropic)  ~/.config/Claude/conversations/
  Codex        OpenAI Codex CLI            ~/.codex/sessions/
```

That's the pedestrian use and that alone should be convincing.

However, this is much more than that! Continue on!


## The Architecture

ctools is a substrate for moving memory between context windows. Cross-platform, agent-agnostic, designed like a bus.

A context is a collection of concepts extracted from a conversation under a given strategy. Constraints, preferences, goals, observations - these are the packets. Each packet has filterable headers: type, description, and content at multiple granularities (short, medium, long). The concept directory is the bus - each concept file is a packet that can be filtered, merged, edited, versioned, and transferred between any two endpoints.

Strategies are filters. They define how conversations are parsed into packets. Different strategies produce different ontologies because context is contestable. The bus doesn't care. Packets move regardless.

Context windows are endpoints. opencode, Claude Code, Codex - they all speak different protocols but they all consume the same packets. That's the point.

```mermaid
graph TB
    subgraph Endpoints
        OC[opencode]
        CC[Claude Code]
        CX[Codex]
    end

    subgraph "Concept Directory (Bus)"
        P1["pkt 1<br/>constraint"]
        P2["pkt 2<br/>preference"]
        P3["pkt 3<br/>goal"]
    end

    subgraph Strategies
        SA["Strategy A"]
        SB["Strategy B"]
    end

    OC -->|"extract"| SA
    CC -->|"extract"| SB
    SA -->|"packets"| P1
    SA -->|"packets"| P2
    SB -->|"packets"| P3
    P1 -->|"inject"| CC
    P2 -->|"inject"| CX
    P3 -->|"inject"| OC
```

## The Problem

You talk to LLMs all day. Over weeks, you build up a set of constraints, preferences, and goals. These live in your conversations as system messages. They are valuable. They are also trapped.

Say you have been working with opencode for a month. You have refined your coding style through dozens of sessions. Now you start a new Claude Code project and you want those same preferences. You could copy them by hand. Or you could use ctools.

```sh
ccopy @opencode/ses_abc123 concepts/
ccopy concepts/ @claude-code/ses_xyz
```

Or skip the bus entirely:

```sh
ccopy @opencode/ses_abc123 @claude-code/ses_xyz
```

Your memory travels with you.

| GNU mtools | ctools | Does what |
|------------|--------|-----------|
| `mdir` | `cdir` | List sessions |
| `mcopy` | `ccopy` | Copy concepts |
| `mdu` | `cdu` | Token usage |
| `mtype` | `cgrep` | Search content |
| - | `cconnect` | Live pipelines |
| `mdel` | `crm` | Concept remove |

## Tools

### ccopy

Move packets between endpoints. The `@` prefix marks a session (endpoint). Plain paths are concept directories (the bus).

```sh
ccopy @opencode/ses_abc123 concepts/              # extract packets to bus
ccopy concepts/ @opencode/ses_abc123               # inject packets from bus
ccopy @opencode/ses_abc123 @claude-code/ses_xyz   # endpoint to endpoint
ccopy -s my-strategy.json @opencode/ses_abc123 concepts/  # custom extraction
ccopy -f my-filter.json @opencode/ses_abc123 concepts/    # filter concepts
```

Each concept file is a packet with filterable headers:

```json
{
  "type": "constraint",
  "description": "C coding standard",
  "short": "Use C17 standard",
  "medium": "Always compile with -std=c17 and enforce strict pointer checking",
  "long": "All C code must target the C17 standard. Use -std=c17 -Wall -Wextra..."
}
```

Strategies define how conversations are parsed into packets. Ontology is contestable, so different strategies produce different chunkings:

```json
{
  "host": "http://localhost:11434",
  "model": "qwen2.5:3b",
  "api_key": null,
  "prompt": "Extract the key concepts from this conversation..."
}
```

Filters select which packets move through the bus. Filters are JSON-RPC 2.0 subprocesses - you write a script, ctools calls it. See [filterlib](#filterlib).

### Strategies

Strategies are named configurations stored in `~/.config/ctools/strategies/`. Each file is a strategy:

```sh
~/.config/ctools/strategies/
├── default.json
├── gemma4.json
└── project-xyz.json
```

Lookup order when you pass `-s name`:
1. If name contains `/` or starts with `.`, use as file path
2. Check current directory for `name.json`
3. Check `~/.config/ctools/strategies/name.json`

Current directory has precedence. Project-specific strategies can live alongside your code.

```json
{
  "host": "http://localhost:11434",
  "model": "qwen2.5:3b",
  "api_key": null,
  "prompt": "Extract the key concepts from this conversation..."
}
```


### cconnect

Connect context windows via live concept pipelines. Exposes concepts from one session as a toolcall in another session's context. Polls the source and re-injects concepts on each cycle.

```sh
cconnect @opencode/ses_abc @claude-code/ses_xyz           # live pipeline (5s default)
cconnect -p 2 @opencode/ses_abc @claude-code/ses_xyz     # poll every 2s
cconnect -c 1 @opencode/ses_abc @claude-code/ses_xyz     # one-shot
cconnect -c 10 -p 1 @opencode/ses_abc @claude-code/ses_xyz  # 10 cycles, 1s apart
cconnect -s my-strategy.json @opencode/ses_abc @claude-code/ses_xyz  # custom extraction
cconnect -f my-filter.json @opencode/ses_abc @claude-code/ses_xyz    # filter concepts
```

Flags: `-c/--count` number of cycles (0=infinity, default), `-p/--poll-interval` seconds between cycles (default 5.0).

Filter configuration:

Filters are JSON-RPC 2.0 subprocesses. The filter script reads a request on stdin and writes a response on stdout.

```json
{
  "command": "./my-filter.py",
  "method": "classify",
  "timeout": 30
}
```

See [filterlib](#filterlib) below.

### cgrep

Searches packet content across the bus. Regex supported. Works across all endpoints.

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

### crm

Remove concepts from sessions. Surgically removes concept-containing sections from agent sessions. Concept JSON files are NOT deleted - only the relevant sections from the context.

```sh
crm @opencode/ses_abc concept.json                    # remove concept from session
crm @opencode/ses_abc concept1.json concept2.json     # remove multiple concepts
crm -a sliding --size 3 @opencode/ses_abc concept.json  # sliding window
crm -s my-strategy.json @opencode/ses_abc concept.json  # use strategy for detection
crm -i -v @opencode/ses_abc concept.json              # interactive + verbose
```

Use case: You used `ccopy` to "pop" concepts out of a session. Now you want to scalpel remove them from the original context because they're throwing off the session. The concept JSON stays intact - you may want to execute it with a different strategy or on a different session.

Algorithms:

- `divide` (default): Divide and conquer. Checks the whole context, then halves recursively until finding the smallest unit containing the concept. Removes that unit.
- `sliding`: Sliding window. Moves linearly through the conversation and snips out places where the concept exists. `--size` controls window width (default 5).

Detection: Without `--strategy`, uses simple string matching. With `--strategy`, uses the LLM to determine if a message contains the concept.

Flags: `-i` interactive (confirm each removal), `-v` verbose (show what's being removed).

### filterlib

Binary classifier for filtering concepts. `filter(in_str) -> True` passes through, `False` filters out. Default on error is `True`.

Filters are JSON-RPC 2.0 subprocesses. You write a script in any language, ctools calls it over stdio.

**Protocol:**

```
stdin:  {"jsonrpc":"2.0","id":1,"method":"classify","params":{"content":"..."}}
stdout: {"jsonrpc":"2.0","id":1,"result":true}
```

`result: true` = pass through. `result: false` = filter out.

**Example filter script (Python):**

```python
#!/usr/bin/env python3
import json, sys

req = json.loads(sys.stdin.readline())
content = req["params"]["content"].lower()

# Filter out anything that looks like a password
has_secret = any(w in content for w in ["password", "secret", "token", "api_key"])
print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": not has_secret}))
```

**Config file:**

```json
{"command": "./my-filter.py", "method": "classify", "timeout": 30}
```

**Python API:**

```python
from ctools.filterlib import JSONRPCFilter, load_filter

f = JSONRPCFilter("./my-filter.py")
f.filter("password: secret123")  # False
f.filter("hello world")          # True

# Load from config file
f = load_filter("filter.json")
```

The filter script can be anything that speaks JSON-RPC on stdio - a regex script, an LLM classifier, a network call, whatever. The subprocess is the abstraction.

## Supported Endpoints

| Agent | Storage |
|-------|---------|
| claude | JSON |
| claude-code | JSONL |
| opencode | SQLite |
| codex | JSONL |

Run `cdir` to see which endpoints are found on your system and where they store data.

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
pip install ctxttools
```

For MCP server support:

```sh
pip install ctxttools[mcp]
```

## Library

Works as a Python library too.

```python
from ctools.lib import AGENTS, get_formatter
from ctools.cdir import get_opencode_sessions
from ctools.cgrep import grep_session
from ctools.ccopy import extract_concepts_from_messages, inject_concepts_to_session
from ctools.cdu import count_tokens, get_session_tokens
from ctools.filterlib import JSONRPCFilter, load_filter
```
