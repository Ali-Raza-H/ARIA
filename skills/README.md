---
name: Example Skill
priority: 0
---

# How to use this folder

Drop `*.md` files here to teach ARIA how to behave. Files are re-read every
turn, so edits apply to the next message without restarting ARIA.

Front-matter is optional:

```yaml
---
name: Deep Research        # display name (defaults to the file name)
priority: 10               # higher = loaded earlier in the system prompt
---
```

Everything below the front-matter is the skill body — plain instructions.

Ideas for skills:

- `research.md` — how ARIA should search, verify, and cite.
- `coding-style.md` — language, framework, and commit conventions.
- `personality.md` — tone, formality, humour, response length.
- `workflow.md` — step-by-step routines for recurring task types.

This file itself is a live example: ARIA is reading it right now.
Edit or delete it freely.
