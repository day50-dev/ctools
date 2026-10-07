# ccopy

`ccopy` copies concepts between sessions and concept files. The `@` prefix marks a
session (endpoint); plain paths are concept files or directories (the bus).

```sh
ccopy @opencode/ses_abc                        # dump to stdout (JSON)
ccopy @opencode/ses_abc concepts/              # extract concepts to a directory
ccopy concepts/ @opencode/ses_abc              # inject concepts from a directory
ccopy @opencode/ses_abc @claude-code/ses_xyz   # session to session
ccopy --into claude-code @opencode/ses_abc     # whole conversation -> NEW claude-code session
ccopy -s my-strategy.json @opencode/ses_abc concepts/  # custom extraction
ccopy -F my-filter.json @opencode/ses_abc concepts/    # filter concepts
ccopy -v @opencode/ses_abc concepts/            # verbose logging
```

## Copying a whole conversation

`--into AGENT` copies a session's **entire conversation** into a brand-new session
in another agent. The source is never touched, so it's a true copy (not a move):

```sh
$ ccopy --into codex @opencode/ses_abc
Copied 42 message(s) from opencode/ses_abc to a new codex session: 1a2b3c
Resume it with:
  codex resume 1a2b3c
```

Only agents that support session creation work as destinations (claude-code,
opencode, kilo, codex, pi). The others refuse with a clean error.

## Concept files

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

Concept types: `constraint`, `goal`, `preference`, `observation`, `reference`.
The default strategy extracts them with a regex; a custom strategy (see below) can
use an LLM instead.

## Strategies

Strategies define how conversations are parsed into concepts. Ontology is
contestable, so different strategies produce different chunkings from the same
conversation.

Strategies are named configurations stored in `~/.config/ctools/strategies/`. Each
file is a strategy:

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

Current directory has precedence. Project-specific strategies can live alongside
your code.

A strategy points at an LLM:

```json
{
  "host": "http://localhost:11434",
  "model": "qwen2.5:3b",
  "api_key": null,
  "prompt": "Extract the key concepts from this conversation..."
}
```

## Filters

Filters select which concepts move through the bus. You write a script in any
language, ctools calls it over stdio as a JSON-RPC 2.0 subprocess. See
[filterlib](#filterlib).

## filterlib

A binary classifier for filtering concepts. `filter(in_str) -> True` passes
through, `False` filters out. Default on error is `True`.

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

The filter script can be anything that speaks JSON-RPC on stdio: a regex script,
an LLM classifier, a network call, whatever. The subprocess is the abstraction.
