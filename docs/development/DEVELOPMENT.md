# ARIA development guide

## Setup

Use Python 3.11. Preferred install:

```bash
uv sync --extra dev --extra tui
```

Optional features:

```bash
uv sync --extra voice
uv sync --extra voice-local
uv sync --extra voice-chatterbox
uv sync --extra desktop
uv run playwright install chromium
```

Run ARIA with `uv run aria` or `python -m aria`.

## Repository map

- `src/aria/config`: schema, defaults, YAML/.env loading, runtime state.
- `src/aria/prompts.py`: editable ARIA, coder, memory, proactive, vision, and profile prompt constants.
- `src/aria/core/agent`: model/tool loop and conversational agent.
- `src/aria/llm`: provider adapters and provider/model factory.
- `src/aria/tools`: tool contracts, registry, router, shell, filesystem, browser, desktop, web, LifeOS, memory, and scheduler tools.
- `src/aria/services`: speech, vision, notifications, proactive, scheduler, and coder services.
- `src/aria/ui`: Rich and urwid frontends.
- `skills/`: live Markdown behavior extensions.
- `workflows/`: YAML cron workflows.
- `tests/`: unit and integration tests.

## Changing behavior without code

- Personality and agent prompts: edit constants in `src/aria/prompts.py`.
- Per-install behavior: edit `config.yaml`.
- Native application routes: edit `desktop.launchers`; use route names with `desktop_launch`.
- Keyboard chords: call `desktop_input` with `action: key`, a `key`, and optional `modifiers`. Do not add ad-hoc shell key commands.
- Reusable behavior: add a Markdown skill under `skills/`; skills are re-read every turn.
- Scheduled behavior: add a workflow under `workflows/`; validate its five-field cron and category allowlist.

## Adding a tool

1. Implement a small service method that validates arguments and returns a string.
2. Catch service errors at the registration boundary and return `ToolResult(..., is_error=True)`.
3. Register a provider-neutral JSON schema in the relevant `register_*_tools` function.
4. Add a unit test for validation and the successful path; avoid real desktop, network, or model calls.
5. Keep private credentials out of results and logs.

## Provider compatibility

Providers normalize to `AssistantMessage` and `ToolCall`. The custom router exists for models without native function calling. Keep standard OpenAI message roles and fields only. In particular, do not add arbitrary fields to assistant messages: OpenAI-compatible APIs reject them.

## Memory extractor

The extractor must tolerate direct JSON, fenced/prose-wrapped JSON, provider failures, and invalid candidate fields. Preserve the rule fallback. If changing the extraction prompt, update the shared prompt constant and add parser coverage.

## UI and scheduled chat

The scheduler can run on its own worker thread. `NotificationService` calls the configured desktop notification backend and the active UI's `receive_notification(title, body)` callback. UI implementations must marshal updates to their UI thread; the urwid UI uses its event loop, while the Rich UI receives callbacks in the process and redraws on the next frame.

## Tests and checks

Per project policy, run tests only after implementation work is complete:

```bash
uv run pytest
uv run pyright
```

Do not include `.venv/`, `data/`, cache files, or generated logs in investigations. If runtime logging is necessary, use only `data/logs/aria.log`.
