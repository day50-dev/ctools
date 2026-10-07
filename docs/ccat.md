# ccat

`ccat` shows a conversation, like `cat` shows a file. `cdir` lists your
sessions and `cgrep` finds them; `ccat` prints the actual conversation, one or
more at a time.

```sh
ccat opencode/ses_abc123                 # show one conversation
ccat opencode/ses_a opencode/ses_b       # show several, in order
ccat --json opencode/ses_abc            # machine-readable JSON
ccat ssh://chris@remote/opencode/ses_a  # show a conversation on another host
```

By default each message is a readable transcript: a dimmed role label in a
fixed column, then the content. `--json` prints the raw conversation as a JSON
list of `{"role", "content"}` objects (the same shape `ccopy` uses on the wire).

```sh
$ ccat opencode/ses_abc123
    you        do a git diff to v0.16.10 ... i broke toolcalling somehow
    assistant  Let me look at the broader context around tool calling...
```

## Many sessions

Pass as many `agent/session_id` references as you like; they print in the order
you give them, separated by a blank line. This is what makes `ccat` the end of
a `cgrep -l` pipeline:

```sh
$ cgrep -l llcat opencode | xargs ccat
```

A reference that can't be read (a bad id, an uninstalled agent) is reported and
skipped; the rest still print. `ccat` exits non-zero only when **no** session
could be printed, the same way `cat` does.

## Across hosts

A remote source is addressed as `ssh://[user@]host[:port]/agent/session_id`.
`ccat` fetches it the same way `ccopy` does — it pulls the agent's storage
files over ssh and reads them locally — so the remote host only needs `sshd`
and `tar`, not ctools. The source is never touched.

## Related

- To list sessions, use [cdir](cdir.md).
- To find sessions by content, use [cgrep](cgrep.md).
- To move a conversation to another agent, use [ccopy](ccopy.md).
