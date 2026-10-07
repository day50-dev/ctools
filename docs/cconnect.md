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

Filters are JSON-RPC 2.0 subprocesses. The filter script reads a request on stdin
and writes a response on stdout. See
[filterlib](ccopy.md#filterlib) for the protocol and examples.

```json
{
  "command": "./my-filter.py",
  "method": "classify",
  "timeout": 30
}
```

## Observability

Every tool supports `--verbose` / `-v` for structured logging via
[structlog](https://github.com/hynek/structlog). Logs go to stderr as JSON lines;
pipe to `jq` for debugging.

```sh
cconnect -v @opencode/ses_abc @claude-code/ses_xyz
LOGLEVEL=DEBUG cconnect @opencode/ses_abc @claude-code/ses_xyz
ccopy -v @opencode/ses_abc concepts/
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
