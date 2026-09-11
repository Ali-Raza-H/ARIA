# Editing ARIA behavior

## Prompts

The public prompt constants are in `src/aria/prompts.py`; `src/aria/config/behaviour.py` re-exports them with a single customization surface. Edit the prompt text, then restart ARIA. Skills are better for per-install behavior because Markdown files are re-read every turn.

## Desktop routes

Configure named native application routes in `config.yaml`:

```yaml
desktop:
  enabled: true
  launchers:
    browser: [firefox]
    terminal: [kitty]
    editor: [code]
```

ARIA should call `desktop_launch` with `command: browser`, `terminal`, or `editor`. Routes are argv arrays, not shell strings. The shell tool refuses commands whose executable is a configured route, preventing the assistant from bypassing the route policy.

## Key chords

Use the desktop tool schema:

```json
{
  "action": "key",
  "key": "c",
  "modifiers": ["CTRL", "SHIFT"]
}
```

A plain key uses `wtype -k`. A chord is sent in one invocation with held modifiers and matching releases. This is different from typing text, which uses `wtype` text input.

## Skills

Create `skills/my-behavior.md` with optional YAML front matter:

```markdown
---
name: Concise answers
priority: 20
---
Prefer short direct answers. Ask before destructive actions.
```

## Workflows

Create YAML under `workflows/` with a five-field cron, category, and action. Read-only analysis is controlled by `scheduler.analysis_enabled`; writes and computer control require explicit autonomy allowlists. See `workflows/README.md`.

## Configuration inventory

The canonical schema is in `docs/configuration/CONFIGURATION.md`. Keep `config.example.yaml`, `.env.example`, and any local `config.yaml` synchronized with loader fields. Do not store runtime credentials in Python, YAML, or skills.
