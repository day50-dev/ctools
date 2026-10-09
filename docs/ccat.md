# ccat

`ccat` shows a conversation, like `cat` shows a file. `cdir` lists your
sessions and `cgrep` finds them; `ccat` prints the actual conversation, one or
more at a time.

```sh
ccat opencode/ses_abc123                 # print one conversation as JSON
ccat opencode/ses_a opencode/ses_b       # several, each a JSON array
ccat --text opencode/ses_abc            # a human-readable transcript
ccat --raw opencode/ses_abc > session.json   # the lossless envelope
ccat opencode/ses_abc | jq .            # it is JSON, so pipe it anywhere
ccat ssh://chris@remote/opencode/ses_a  # a conversation on another host
```

By default `ccat` prints the conversation as a JSON list of `{"role", "content"}`
objects (one array per session) — the same shape `ccopy` uses on the wire. JSON is
the first transportable format for a conversation, so it is what `ccat` hands you
and what you pipe onward: to `jq`, to another tool, or straight back into a
session with `ccopy --import-json`. Use `--text` for a readable transcript instead
(a dimmed role label in a fixed column, then the content):

```sh
$ ccat opencode/ses_abc123
[
  {"role": "user", "content": "do a git diff to v0.16.10 ... i broke toolcalling"}
]

$ ccat --text opencode/ses_abc123
    you        do a git diff to v0.16.10 ... i broke toolcalling somehow
    assistant  Let me look at the broader context around tool calling...
```

## --raw: the lossless envelope

The default JSON is the *portable* conversation — as much as every agent can
carry. `--raw` prints the *lossless* form instead: the same `context` array,
plus the source agent's verbatim records and the session metadata, so a copy
back into the *same* agent loses nothing.

```sh
$ ccat --raw opencode/ses_abc123
{
  "context": [ {"role": "user", "content": "do a git diff ..."}, ... ],
  "source":  "opencode/ses_abc123",
  "model":   {...},
  "created": "2025-...",
  "modified": "2025-...",
  "raw":     [ { /* the agent's verbatim per-message records */ } ]
}
```

- **`context`** — the `llcat`/OpenAI spine, a bare list of `{"role", "content"}`
  (plus `tool_calls` / `reasoning` where the agent keeps them). This is what you
  feed `jq`, `llcat`, or a different agent.
- **`source`**, **`model`**, **`created`**, **`modified`** — session-level facts,
  carried *once* at the top rather than bolted onto every record.
- **`raw`** — the source agent's verbatim storage records, parallel to the
  conversation. It is what `ccopy` uses to replay a session into the *same*
  agent without flattening. Agents whose storage we can't introspect simply omit
  it (the honest "we can't" answer), and `context` still carries the conversation.

Because it's JSON with a top-level `context` key, the lossless payload is
jq-addressable without ever polluting the everyday pipe:

```sh
ccat --raw opencode/ses_abc | jq .context[0]     # just the first turn
ccat --raw opencode/ses_abc | jq .model          # the session's model
```

## Many sessions

Pass as many `agent/session_id` references as you like; they print in the order
you give them, separated by a blank line. This is what makes `ccat` the end of
a `cgrep -l` pipeline:

```sh
$ cgrep -l llcat opencode | xargs ccat            # every match, as JSON
$ cgrep -l llcat opencode | xargs ccat --text     # or as a readable transcript
```

A reference that can't be read (a bad id, an uninstalled agent) is reported and
skipped; the rest still print. `ccat` exits non-zero only when **no** session
could be printed, the same way `cat` does.

## Across hosts

A remote source is addressed as `ssh://[user@]host[:port]/agent/session_id`.
`ccat` fetches it in two tiers, the same way `ccopy` does: if the remote runs
ctools, its own `ccat`-equivalent export streams just the session's JSON over one
ssh; otherwise `ccat` pulls the agent's storage files over ssh and reads them
locally, so a bare remote needs only `sshd` and `tar`, not ctools. The source is
never touched.

## Related

- To list sessions, use [cdir](cdir.md).
- To find sessions by content, use [cgrep](cgrep.md).
- To move a conversation to another agent, use [ccopy](ccopy.md).
