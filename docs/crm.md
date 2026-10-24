# crm

`crm` removes concepts from sessions. It surgically removes concept-containing
sections from agent sessions. Concept JSON files are NOT deleted — only the
relevant sections from the context.

`crm` is the short name for `ctools rm`: the same program with the same flags.
The examples use `crm`; `ctools rm …` works anywhere they do.

```sh
crm claude-code/01a14b0d...                     # no concepts: pick messages to remove by hand
crm --backup claude-code/01a14b0d...            # same, but snapshot to gettmp()/ctools/<agent>/<uuid> first
crm @opencode/ses_abc concept.json              # remove concept from session
crm @opencode/ses_abc concept1.json concept2.json  # remove multiple concepts
crm -a sliding --size 3 @opencode/ses_abc concept.json  # sliding window
crm -s my-strategy.json @opencode/ses_abc concept.json  # use strategy for detection
crm -i -v @opencode/ses_abc concept.json        # interactive + verbose
```

## By hand (no concepts)

`crm <agent>/<session>` with no concept files is the simple path: every message
is listed with an index and a preview, you type the indices to remove
(space-separated, `a-b` ranges allowed, e.g. `3 7 12-15`), it shows what will go,
asks once, and removes. Empty answer or EOF aborts — removal is never done
blindly. No heuristics, no concept files: the user is the strategy.

Use it on the things the heuristics can't reach — e.g. a session forked or
resumed by the agent itself, carrying a copied wall of log output that no
concept file describes.

### Backing up first (`--backup`)

`crm --backup <agent>/<session>` breaks ranks with `rm`: before touching the
session it snapshots its storage to `gettmp()/ctools/<agent>/<uuid>`, where
`gettmp()` is the durable tmp base (`$CTOOLS_TMP`, else the XDG data home,
default `~/.local/share` — unlike `tempfile.gettempdir()`, it survives reboots
and janitors). File agents get their session file copied; sqlite agents
(opencode) get a consistent `VACUUM INTO` snapshot of the database. An existing
backup is never clobbered — a second run writes `<uuid>-1`, so every removal
keeps its own pre-state.

## Use case

You used `cextract` to "pop" concepts out of a session. Now you want to scalpel
remove them from the original context because they're throwing off the session.
The concept JSON stays intact, so you can run it with a different strategy or on
a different session later.

## Algorithms

- `divide` (default): Divide and conquer. Checks the whole context, then halves
  recursively until finding the smallest unit containing the concept. Removes
  that unit.
- `sliding`: Sliding window. Moves linearly through the conversation and snips
  out places where the concept exists. `--size` controls window width
  (default 5).

## Detection

Without `--strategy`, uses simple string matching. With `--strategy`, uses the
LLM to determine if a message contains the concept.

Flags: `-i` interactive (confirm each removal), `-v` verbose (show what's being
removed). Note: agents whose storage is a branch tree (pi) refuse removal —
crm reports this rather than rewriting the graph.
