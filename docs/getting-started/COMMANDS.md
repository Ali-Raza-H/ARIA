# TUI command reference

ARIA's interactive interface provides slash commands for runtime control. Exact command availability can depend on the enabled feature set.

## Provider and model

```text
/providers
```

Show configured providers and their available model inventory.

```text
/models
```

Inspect models for the current provider.

```text
/provider <name>
```

Switch the active ARIA provider for the current runtime. The selection is persisted as a runtime preference rather than rewriting `config.yaml`.

```text
/model <name>
```

Switch the active model. ARIA validates the selected model against the provider inventory where supported.

## Runtime behavior

```text
/workspace
```

Inspect or change the active workspace using the supported workspace command form.

```text
/iterations
```

Inspect/configure ARIA's model/tool iteration limit using the supported command form.

```text
/agent
```

Inspect the agent/coder-related runtime controls exposed by the current UI.

## Trace

```text
/trace on
/trace off
```

Enable/disable the live execution trace. Trace output describes execution events such as model/tool rounds and tool results; it does **not** expose or claim to expose private model chain-of-thought.

`/cot` is retained as a deprecated alias where supported by the current command implementation.

## Speech

```text
/tts on
/tts off
/tts kokoro_hf
/tts kokoro_local
/tts chatterbox
/tts status
```

Switch the speech backend or inspect its current status. The required optional Python/host dependencies must be installed for the selected engine.

## Memory

```text
/memory
/memory facts
/memory search <text>
/memory summarize
/memory promote
/memory retention
```

These commands inspect and process persistent memory. See [Memory Architecture](../architecture/MEMORY.md).

Destructive operations require an explicit uppercase confirmation:

```text
/memory wipe session DELETE
/memory wipe 1 DELETE
/memory wipe 2 DELETE
/memory wipe 3 DELETE
/memory wipe all DELETE
```

`/clear` clears the current conversation/session state without intentionally wiping durable facts.

## Timers

```text
/timer create <name> <kind> <seconds>
/timer start <id>
/timer pause <id>
/timer resume <id>
/timer restart <id>
/timer finish <id>
/timer list
```

Timer state can be persistent or session-oriented depending on the creation path. Scheduler configuration controls persistent timer storage and notifications.

## Skills

```text
/skills
```

List currently discovered Markdown skills. Skills are re-read every turn, so edits normally take effect without restarting ARIA.

## Attachments and vision

```text
/attach clipboard
/attach /path/to/image.png
/screen
```

These queue an image for the next model request. Images are ephemeral by default. A configured visual-capable model or fallback provider is required for the model to interpret them.

## Scheduler

```text
/scheduler
```

Inspect scheduler state, active configuration, and autonomy/allowlist information.

The scheduler runs in-process. Closing ARIA stops scheduled execution until the application is started again.

## Telemetry

```text
/telemetry
```

Display an aggregate operational telemetry snapshot. Telemetry is designed around metadata rather than conversation-content storage.

## Save/export

```text
/save
```

Export the current conversation transcript using the active UI's save behavior.

## State reset

If runtime preferences become stale, exit ARIA and run:

```bash
uv run aria --ignore-state
```

for a one-run bypass, or:

```bash
uv run aria --reset-state
```

to delete the ignored runtime-state file before launching.

## Command troubleshooting

If a command is unavailable:

1. Check whether the corresponding feature is enabled in `config.yaml`.
2. Confirm the current TUI backend is initialized correctly.
3. Check `data/logs/aria.log` for registration/startup errors.
4. Consult the feature-specific documentation.

Do not infer that an optional feature is broken merely because its command is unavailable: disabled integrations intentionally do not expose their tools/controls.
