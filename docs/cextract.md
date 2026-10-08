# cextract

`cextract` extracts concepts from sessions into files, and injects them back. The `@` prefix marks a
session (endpoint); plain paths are concept files or directories (the bus).

```sh
cextract @opencode/ses_abc                        # dump to stdout (JSON)
cextract @opencode/ses_abc concepts/              # extract concepts to a directory
cextract concepts/ @opencode/ses_abc              # inject concepts from a directory
cextract @opencode/ses_abc @claude-code/ses_xyz   # session to session
cextract -s my-strategy.json @opencode/ses_abc concepts/  # custom extraction
cextract -F my-filter.json @opencode/ses_abc concepts/    # filter concepts
cextract -v @opencode/ses_abc concepts/            # verbose logging
```

## Copying a whole conversation

To copy a session's **entire conversation** into a new session in another agent,
use [ccopy](ccopy.md). The source is never touched, so it's a true copy (not a
move).

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

Filters select which concepts move through the bus. Two kinds:

- **Built-in** (type/prompt matching) — the default, no configuration server.
- **Decision model** — a small, local classifier (Ollama) that scores each
  concept and passes it through only when confident.

See [filterlib](#filterlib) for the protocol and the Python API.

### Decision-model filters

A **decision model** is a small, fast classifier that scores a typed question
against some text and returns calibrated probabilities — no free text to parse,
nothing to hallucinate. It makes a cheap, local, *semantic* relevance filter:
instead of matching a substring, you ask "is this concept about X?" and gate on
the model's answer.

A decision model can be asked through two endpoints (the `endpoint` field,
selectable per config):

* `systemone` (default) — Ollama's native typed endpoint, `POST
  /v1/systemone`. The request carries the state and typed questions; the
  response carries a calibrated probability per option. This is the home of the
  decision-dedicated models (Modelfile `CAPABILITY decision`): no free text is
  generated and nothing is parsed. Requires a server that exposes the typed
  endpoint (e.g. Ollama 0.40.x).
* `chat` — the portable OpenAI-compatible endpoint, `POST
  /v1/chat/completions`. Works on any server that can run the model (vLLM,
  older Ollama, any OpenAI-compatible gateway, or a non-Ollama host). The typed
  question is rendered into the model's lettered decision-task prompt and the
  reply is parsed for the chosen letter. There are no probabilities, so
  `noul` mode passes on the yes-letter and `threshold` is not used. Pick a
  model that follows instructions; thinking models that burn their token budget
  on reasoning before emitting the letter will time out and pass through
  (the safe default).

```json
{
  "type": "systemone",
  "host": "http://localhost:11434",
  "model": "tev1",
  "endpoint": "systemone",
  "instructions": "Is this concept about cryptography, secrets, or security?",
  "threshold": 0.6
}
```

```sh
cextract -F security.json @opencode/ses_abc concepts/
```

In `noul` mode each concept is sent as the model's `state`; the model answers
the question (`instructions`) and the concept passes when the answer is yes
(probability >= `threshold` on `systemone`, the yes-letter on `chat`).

**Routing (choice mode).** A multi-way question routes concepts to the
right destination — a coding agent gets coding concepts, a security agent gets
security constraints, all from the same source:

```json
{
  "type": "systemone",
  "model": "nimble",
  "mode": "choice",
  "question_name": "category",
  "instructions": "Which category does this concept belong to?",
  "criteria": { "coding": "language or style rules", "security": "secrets, crypto, auth" },
  "allowed": ["coding", "security"]
}
```

In `choice` mode the model picks a category from `criteria`; the concept passes
only when the chosen category is in `allowed`. On `chat` the category is the
letter the model returns.

Extra fields: `question_name` (the answer key, default `relevant`),
`max_tokens` (chat endpoint, default `2048` — raise it for thinking models),
`api_key` (chat endpoint, sent as `Authorization: Bearer` when set), and
`timeout` seconds (default `30`).

A config with a `model` and no `command` is treated as a decision-model filter
even without an explicit `type`. On any error (no server, missing model, bad
response) a decision filter passes the concept through, matching the rest of
filterlib.

### Built-in filters

A plain filter config does type and substring matching, no server needed:

```json
{ "types": ["constraint", "goal"], "exclude_types": ["reference"], "prompt": "ssl" }
```

- `types` — only these concept types pass.
- `exclude_types` — drop these concept types.
- `prompt` — only concepts whose text contains this substring pass.

## filterlib

A binary classifier for filtering concepts. `filter(in_str) -> True` passes
through, `False` filters out. Default on error is `True`.

Two backends implement the same interface:

### `SystemOneFilter` (decision model)

Talks to a decision model over either endpoint: `endpoint="systemone"`
(default, Ollama's typed `/v1/systemone`) or `endpoint="chat"` (any
OpenAI-compatible `/v1/chat/completions`). The primary filter for the
decision-model mode above.

```python
from ctools.filterlib import SystemOneFilter

# yes/no with a confidence threshold (Ollama's typed endpoint)
f = SystemOneFilter(host="http://localhost:11434", model="tev1",
                    instructions="Is this about cryptography or secrets?")
f.filter("use AES-256, never MD5")   # True if the model is >= 0.6 confident

# the same question over any OpenAI-compatible server
f = SystemOneFilter(host="http://vllm:8000", model="some-instruct-model",
                    endpoint="chat", instructions="Is this about security?")
f.filter("use AES-256, never MD5")   # True if the model replies "A"/yes

# multi-way routing
r = SystemOneFilter(model="nimble", mode="choice",
                    instructions="Which category?",
                    allowed=["coding", "security"],
                    criteria={"coding": "style rules", "security": "secrets"})
```

`noul` mode uses `threshold` on `systemone`; on `chat` it passes when the
model's letter corresponds to the yes option. `choice` mode uses `allowed`
on both endpoints.

### `JSONRPCFilter` (subprocess)

Spawns a subprocess that speaks JSON-RPC 2.0 over stdio. The contract:

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
from ctools.filterlib import JSONRPCFilter, SystemOneFilter, load_filter

f = JSONRPCFilter("./my-filter.py")
 f.filter("password: secret123")  # False
f.filter("hello world")          # True

# Load from a config file; load_filter picks the backend from the shape.
f = load_filter("filter.json")   # {"type":"systemone",...} -> SystemOneFilter
                                  # {"command":...}          -> JSONRPCFilter
```

The filter script can be anything that speaks JSON-RPC on stdio: a regex script,
an LLM classifier, a network call, whatever. The subprocess is the abstraction.
A decision model, on the other hand, is a drop-in filter with no script at all —
just point it at a model.
