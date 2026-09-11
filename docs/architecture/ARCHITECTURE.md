# ARIA architecture

ARIA is a local-first Python 3.11 terminal assistant. The process is assembled by `src/aria/__main__.py`; runtime state, credentials, logs, memory, and scheduler data are separate from static source configuration.

## Runtime graph

```text
CLI
 └─ config loader (.yaml + .env + optional runtime state)
    ├─ provider manager → ARIA provider / coder provider / optional background and vision providers
    ├─ tool registry → filesystem, shell, browser, desktop, LifeOS, web, vision, memory, timers
    ├─ MemoryManager → SQLite facts + Chroma semantic/history collections
    ├─ SchedulerService → SQLite jobs/timers/audit + proactive actions + notifications
    ├─ SpeechController → Kokoro hosted/local or Chatterbox
    └─ UI factory → urwid (default) or Rich
```

## Startup

1. `load_config()` reads YAML, then `.env`, applies whitelisted runtime preferences, and validates every section.
2. Logging is configured to the single rotating `data/logs/aria.log` file. Secrets are redacted by the logging layer.
3. `ProviderManager` creates the active ARIA provider. Hosted adapters use the provider's ARIA key; coder credentials remain separate.
4. The registry is populated. GUI launch commands are also reserved from the shell tool so configured routes are used instead of ad-hoc program launches.
5. Optional memory, web, browser, desktop, proactive, speech, and scheduler services are initialized independently. Failure of an optional service should degrade that service rather than prevent chat startup.
6. The selected UI is created. Scheduler results are sent to both the notification backend and the live chat sink.

## Conversation loop

`BaseAgent.run()` adds the user message, prepares memory context, and repeats model/tool rounds up to `max_iterations`:

1. Build a bounded provider message list.
2. Attach queued images only to the first request.
3. Call the provider, optionally streaming text.
4. Parse the custom `<tool_call>{...}</tool_call>` protocol and merge it with native calls.
5. Execute tools through `ToolRegistry`, emitting trace events.
6. Return native tool results using standard `tool` messages. Custom protocol results use a user-role envelope.
7. Finish when there are no calls/errors, then queue memory extraction.

Assistant messages must contain only fields accepted by the provider API. Internal custom-call bookkeeping is never serialized as `custom_tool_calls`; this avoids OpenAI-compatible HTTP 422 responses.

## Tools and routing

Tool schemas are generated from `Tool` objects and injected into the system prompt. `desktop.launchers` is a named route map. `desktop_launch` accepts a route name and executes its argument array without a shell. The shell tool rejects the first token of a configured launcher command and tells the model to use `desktop_launch` instead.

Desktop control has two modes:

- `managed`: inspection, allow-listed Hyprland dispatchers, named launchers, input, and screenshots.
- `unrestricted`: additionally enables raw dispatch, marked keybind edits, configured desktop commands, and arbitrary desktop shell.

Keyboard text uses `wtype`. Chords use one `wtype` invocation with `-M` modifier holds and `-m` releases; this avoids releasing modifiers before the key is sent.

## Memory pipeline

After a turn, `MemoryManager` queues extraction in a background worker. `MemoryExtractor` asks the active model for JSON, accepts direct JSON and balanced embedded JSON, validates every candidate, and falls back to deterministic classification when the model response is malformed or unavailable. SQLite stores structured facts and registry metadata; Chroma stores semantic memories and cross-session history. Embeddings may degrade independently.

## Images

Images are ephemeral attachments. `/attach clipboard` reads a clipboard image through Wayland/X11 helpers; `/attach path` reads a supported image file; `/screen` captures a temporary screenshot. Native vision models receive image parts. Text-only models require a configured visual fallback. A normal terminal cannot carry binary image data through Ctrl+V; direct image paste therefore reports an explicit limitation rather than corrupting the prompt.

## Scheduling and briefings

`SchedulerService` polls cron jobs in-process and persists jobs, timers, and immutable audit entries in SQLite. Briefing/analysis jobs are read-only and controlled by `scheduler.analysis_enabled`; consequential categories additionally require the autonomy allowlist. Each result is passed to `NotificationService`, which sends the desktop notification/TTS and appends the same result to the active chat transcript.

## Speech

`SpeechController` strips Markdown and normalizes ISO dates and clock values before synthesis. For example, `2026-09-10 17:45:16` is spoken as “September 10, 2026, 5:45:16 PM.” Backends load lazily and disable themselves after an unavailable dependency/audio device is detected; `/tts status` exposes the last error.

## Browser UI

ARIA uses Playwright persistent context only when `browser.enabled` is true. It uses the configured executable when supplied, otherwise Playwright's bundled Chromium. A missing browser runtime should produce a clear installation message. Native desktop browser launch remains a separate configured desktop route; it is not inferred from the browser automation setting.

## Data boundaries

- Static config: `config.yaml`, `config.example.yaml`.
- Secrets: `.env`, never logged or committed.
- Runtime preferences: ignored `data/state/aria-state.yaml`.
- Logs: `data/logs/aria.log` only.
- Memory: `data/memory/`.
- Scheduler: `data/scheduler/`.
- Browser profile: `data/browser-profile/`.
- Skills/workflows: editable Markdown/YAML under `skills/` and `workflows/`.

Do not read or place generated reports in `docs/reports/`; that directory is intentionally outside the maintained documentation set.
