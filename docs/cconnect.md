# cconnect

`cconnect` connects context windows via live concept pipelines. It exposes
concepts from one session as a toolcall in another session's context. It polls the
source and re-injects concepts on each cycle.

Strategy extracts, filter selects per destination:

```sh
cconnect @opencode/ses_abc @claude-code/ses_xyz           # live pipeline (5s default)
cconnect -p 2 @opencode/ses_abc @claude-code/ses_xyz     # poll every 2s
cconnect -c 1 @opencode/ses_abc @claude-code/ses_xyz     # one-shot
cconnect -c 10 -p 1 @opencode/ses_abc @claude-code/ses_xyz  # 10 cycles, 1s apart
cconnect -s my-strategy.json @opencode/ses_abc @claude-code/ses_xyz  # custom extraction
cconnect -f my-filter.json @opencode/ses_abc @claude-code/ses_xyz    # filter for destination
```

Flags: `-c/--count` number of cycles (0=infinity, default), `-p/--poll-interval`
seconds between cycles (default 5.0), `-v/--verbose` structured logging.

## One-to-many pipeline

A pipeline file fans one source out to many destinations, each with its own
filter:

```json
{
  "source": "@opencode/ses_abc",
  "strategy": "strategy.json",
  "tool_name": "context_from_source",
  "count": 0,
  "poll_interval": 5,
  "destinations": [
    { "session": "@claude-code/ses_xyz", "filter": "coding.json" },
    { "session": "@opencode/ses_123", "filter": "security.json" },
    { "session": "@codex/ses_456", "filter": "goals.json" }
  ]
}
```

```sh
cconnect --pipeline pipeline.json
```

## Filter configuration

A filter selects which extracted concepts reach a destination. Two backends:

**Decision model** — a small local classifier that scores each concept and
passes it through when confident. The `endpoint` field selects how it is asked:
`systemone` (default, Ollama's typed `/v1/systemone`, returns probabilities)
or `chat` (any OpenAI-compatible `/v1/chat/completions` server, parses the
model's lettered reply). This is the natural fit for per-destination routing:
a coding destination gets a `coding` filter, a security destination gets a
`security` filter, all from the same source.

```json
{
  "type": "systemone",
  "model": "nimble",
  "mode": "choice",
  "question_name": "category",
  "instructions": "Which category does this concept belong to?",
  "criteria": { "coding": "style rules", "security": "secrets, crypto, auth" },
  "allowed": ["coding", "security"]
}
```

**Subprocess** — a JSON-RPC 2.0 filter script (any language) that reads a
request on stdin and writes a response on stdout. See
[filterlib](cextract.md#filterlib) for the protocol and examples.

```json
{
  "command": "./my-filter.py",
  "method": "classify",
  "timeout": 30
}
```

Both work in one-to-one pipelines (`-f`) and in pipeline destinations
(`"filter": ...`). On any error a filter passes the concept through.

## Observability

Every tool supports `--verbose` / `-v` for structured logging via
[structlog](https://github.com/hynek/structlog). Logs go to stderr as JSON lines;
pipe to `jq` for debugging.

```sh
cconnect -v @opencode/ses_abc @claude-code/ses_xyz
LOGLEVEL=DEBUG cconnect @opencode/ses_abc @claude-code/ses_xyz
cextract -v @opencode/ses_abc concepts/
```

Verbose output shows every pipeline stage:

```
{"event": "concepts_extracted", "source": "@opencode/ses_abc", "count": 12, "types": {"preference": 5, "constraint": 3, "goal": 4}, "elapsed_ms": 42}
{"event": "concept_filtered", "reason": "type_excluded", "type": "observation", "short": " noticed the build is slow"}
{"event": "filter_applied", "config": "coding.json", "input_count": 12, "output_count": 8, "dropped": 4}
{"event": "inject_complete", "destination": "@claude-code/ses_xyz", "count": 8, "elapsed_ms": 15}
{"event": "cycle_complete", "source": "@opencode/ses_abc", "destination": "@claude-code/ses_xyz", "injected": 8}
```

Without `-v`, tools are quiet. Only errors and final results print to the
terminal. The `LOGLEVEL` env var overrides: `DEBUG`, `INFO`, `WARNING`, `ERROR`.

For filter debugging, filterlib logs every subprocess call and result:

```sh
LOGLEVEL=DEBUG cconnect -f my-filter.json @opencode/ses_abc @claude-code/ses_xyz 2>&1 | jq '.event == "filter_timeout"'
```
