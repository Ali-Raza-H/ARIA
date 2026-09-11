# ARIA telemetry

ARIA telemetry is an operational metrics stream, not a second conversation archive.

## What is recorded

The SQLite database configured by `telemetry.database` contains:

- `turns`: turn duration, user/response character counts, iteration count, tool count, and errors.
- `model_calls`: provider, model, agent role, iteration, message count, context character size, configured context window, provider-reported input/output/total/cached tokens, estimated input/output tokens, latency, stream flag, and errors.
- `tool_calls`: tool name, latency, success, argument/result sizes, and a short argument hash. Arguments and results are not stored.
- `events`: live lifecycle events for dashboards and UI listeners.

The recorder is thread-safe and uses SQLite WAL mode because ARIA's worker threads and scheduler may emit events concurrently.

## Token accounting

OpenAI-compatible streaming adapters collect usage from the final stream chunk. Ollama adapters collect its `prompt_eval_count` and `eval_count` counters. Provider-specific counters remain in `metadata_json`. When an endpoint supplies no usage, telemetry can record a rough estimate using approximately four characters per token. Estimates are labelled separately and must not be treated as billing-grade values.

## Live events

`TelemetryRecorder.add_listener()` subscribes to `TelemetryEvent` objects. Events include `turn_started`, `model_started`, `model_finished`, `tool_finished`, and `turn_finished`. Listener failures are isolated so telemetry cannot break an assistant turn. The active UI exposes an aggregate view through `/telemetry`; its top bar updates live with total tokens and the latest context usage. Configure context windows per model under `providers.<provider>.context_windows`; the display shows current context characters against that configured window when available.

## Privacy and retention

Telemetry does not store prompt, response, email, web, or tool contents. It stores sizes and operational metadata only. API keys are never passed to the recorder. `retention_days` prunes old rows at startup; set it to `0` to retain indefinitely. The database is local and should be treated as sensitive operational data. Keep it under the ignored `data/` directory and do not commit it.

## Configuration

```yaml
telemetry:
  enabled: true
  database: data/telemetry/telemetry.sqlite3
  live: true
  retention_days: 90
  record_estimates: true
```

`ARIA_TELEMETRY_ENABLED`, `ARIA_TELEMETRY_LIVE`, and `ARIA_TELEMETRY_RETENTION_DAYS` can override these fields through the normal configuration environment mechanism.
