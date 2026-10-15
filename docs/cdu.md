# cdu

`cdu` shows token usage. Like `du` but for context windows. Uses tiktoken for
accurate counts.

```sh
cdu                           # total across all agents
cdu opencode/                 # sessions by token count
cdu opencode/ses_abc123       # breakdown by role
cdu --json opencode/          # machine-readable
cdu -q opencode/              # quiet: plain rows, no table/section/total (scrape)
```

For opencode, it reads actual input/output tokens from the database. For other
agents, it counts with tiktoken from the conversation content.

## Quiet output (`-q`)

`-q` (or `--quiet`) replaces the rich table with plain rows — no borders, no
title, no section break, no TOTAL row — for piping and screen-scraping. Same
fields and same token-descending order, but with raw token counts (not the
`1.2k` shorthand) and, for `cdu AGENT/`, all sessions (the table view caps at
50 for display):

```sh
$ cdu -q opencode/ | head
ses_abc123  Petsitter exportit llcat JSON export  41851709  668

$ cdu -q opencode/ses_abc123
total  41851709
input  38102233
output  3749476
source  (actual)
```

`--json` remains the structured alternative.
