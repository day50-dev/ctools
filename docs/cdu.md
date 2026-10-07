# cdu

`cdu` shows token usage. Like `du` but for context windows. Uses tiktoken for
accurate counts.

```sh
cdu                           # total across all agents
cdu opencode/                 # sessions by token count
cdu opencode/ses_abc123       # breakdown by role
cdu --json opencode/          # machine-readable
```

For opencode, it reads actual input/output tokens from the database. For other
agents, it counts with tiktoken from the conversation content.
