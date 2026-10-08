# ctools Interchange Format

How ctools moves a conversation **between different agents** (and between hosts),
and how that correctness is tested.

## The common format

Every agent that supports it exposes two functions against a single
**common format** — the `ccat --raw` envelope:

```json
{
  "context":  [ {"role": "user", "content": "..."}, {"role": "assistant", "content": "..."} ],
  "source":   "pi/91d1-...",
  "model":    {"provider": "anthropic", "model": "claude-...", "api": "anthropic-messages"},
  "created":  "2026-...Z",
  "modified": "2026-...Z",
  "raw":      [ ...the agent's verbatim records... ]
}
```

- **`context`** — the portable spine: bare `{role, content}` (plus `reasoning` /
  `tool_calls` where an agent supports them). Always present.
- **`raw`** — the agent's *verbatim* storage records, included losslessly when the
  agent can introspect them (opencode, pi). Optional; omitted when it can't.
- The rest — session facts, once at the top. No per-record decoration.

## The 2N property

A cross-agent copy **A → B** is just the composition:

```
to_common(A)  ->  from_common(B)
```

| Agent function | Role |
|---|---|
| `Agent.to_common(session_id) -> dict` | Export this session to the common format. |
| `Agent.from_common(doc) -> new_id`    | Import a common-format envelope as a new session. |

If every agent's `to_common` is faithful and every agent's `from_common`
reconstructs a *loadable* session, then **every** A → B pair works — without
ever testing N² pairs.

`from_common` uses the lossless `raw` records when the destination is the
*same* agent (verbatim round-trip), and falls back to the `context` spine
otherwise (cross-agent). That is exactly why `ccopy` goes through the common
format rather than raw message lists.

## Capability-based degradation

The common format is opt-in and degrades by capability, not by agent name:

- **Seeding agents** (opencode, pi, ...) — `to_common` + `from_common`.
- **Read-only agents** — expose `raw_records` so `ccat --raw` is lossless for
  them; they refuse `from_common` (no `create_session`).
- **Bare agents** — still move via the `context` spine through `ccopy` (the
  minimum every agent provides).

## Oracles

Correctness of `from_common` is proven per-agent with the strongest oracle the
agent has, **independently** of other agents:

| Agent | Oracle |
|---|---|
| opencode | the real `opencode export <id>` re-reads the session we wrote (message count, roles, text). The TUI loads the same rows. |
| pi       | `pi --session <id>` exits 0 (the real runtime loads the session we wrote). |
| others   | structural read-back through ctools' reader (no CLI to act as an oracle). |

Live oracles write to the agent's **real** storage (the CLI looks there, not in
a temp dir) and clean up after themselves. They are skipped when the CLI is not
installed.

## Tests

See `ctools/tests/test_interchange.py`:

- `to_common` shape (context + lossless raw + session facts) per agent.
- `from_common` round-trip (same agent, verbatim) per agent.
- Live oracles per agent (opencode `export`, pi `--session`), when the CLI exists.
- Cross-agent composition (opencode→pi, pi→opencode) — the N² that falls out of 2N.
- Capability degradation (non-seeding agents refuse `from_common`).

## Wiring

`ccopy`'s local copy path is `export_common(source) -> import_common(destination)`
(i.e. `to_common -> from_common`). File/stdin/remote sources are wrapped into an
envelope so the same path handles every source form.
