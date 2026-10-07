# crm

`crm` removes concepts from sessions. It surgically removes concept-containing
sections from agent sessions. Concept JSON files are NOT deleted — only the
relevant sections from the context.

```sh
crm @opencode/ses_abc concept.json                    # remove concept from session
crm @opencode/ses_abc concept1.json concept2.json     # remove multiple concepts
crm -a sliding --size 3 @opencode/ses_abc concept.json  # sliding window
crm -s my-strategy.json @opencode/ses_abc concept.json  # use strategy for detection
crm -i -v @opencode/ses_abc concept.json              # interactive + verbose
```

## Use case

You used `ccopy` to "pop" concepts out of a session. Now you want to scalpel
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
removed).
