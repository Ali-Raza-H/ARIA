# ARIA startup and early-exit bug report

**Reviewed:** 2026-09-09  
**Scope:** Current working tree and its active local configuration. No application source files were changed for this report.

## Summary

ARIA has two confirmed startup blockers in the active setup, plus one code path that can make it appear to stop immediately. The most immediate blockers are an unavailable model selected by persisted runtime state and the mandatory SearXNG check.

## Findings

### BR-1 — Web search makes startup depend on SearXNG and Docker

**Severity:** Critical for a normal local launch  
**Status:** Reproduced

`web.enabled` is true in the active configuration. During startup, `main()` calls `_ensure_web_backend()` and exits with status 1 if SearXNG is not reachable. Before that exit it tries `docker compose ... up -d` itself and then polls for up to 30 seconds.

In this environment, running `uv run aria` produced:

```text
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
Startup: docker compose failed: ... returned non-zero exit status 1
```

The code then continues polling before reporting that the backend is unreachable and returning. This presents as a startup hang followed by ARIA stopping.

**Locations:** `src/aria/__main__.py:107-126`, `src/aria/__main__.py:305-340`, `config.yaml:191-193`.

**Why this is a defect:** Web research is an optional feature, but it prevents all use of the assistant when its optional local service is down. It also performs an unexpected Docker state-changing operation from the normal application startup path.

**Suggested remediation:** Make the failed health check disable only web tools and continue launching. Keep compose startup as an explicit command or opt-in configuration, and surface a non-fatal warning with the recovery command.

### BR-2 — Persisted runtime state silently overrides `config.yaml` and selects an unavailable model

**Severity:** Critical for the current setup  
**Status:** Confirmed from startup logs and configuration loading order

The active YAML specifies an Ollama model, but the most recent startup log records ARIA starting with `model=test`. The same log history records the resulting error:

```text
OllamaProvider: chat failed: ResponseError: model 'test' not found (status code: 404)
Repl: turn failed: ResponseError: model 'test' not found (status code: 404)
```

`load_config()` applies `data/aria-state.yaml` after reading `config.yaml`; that file is allowed to overwrite the provider and model. Because it is ignored by Git and its overrides are not shown in normal startup output, a stale or experimental model survives configuration edits and makes ARIA look broken immediately after launch.

**Locations:** `src/aria/config.py:278-319`, `src/aria/config.py:415-416`; evidence in `data/logs/info.log` at the 2026-09-09 startup entries.

**Suggested remediation:** Validate the restored provider/model against the configured provider's model list or the local Ollama inventory before entering the UI. Print the effective config and the override source at startup, and add a command-line option to ignore/reset runtime state.

### BR-3 — An urwid TUI crash is isolated in a daemon thread and becomes a clean application exit

**Severity:** High  
**Status:** Confirmed by code inspection

The default `urwid` interface runs `loop.run()` on a daemon thread. The thread catches only `KeyboardInterrupt`. Any other error raised by urwid, terminal setup, the event loop, or `async_loop.close()` terminates the UI thread. The main thread merely joins it and then proceeds through normal cleanup, logs `ARIA shut down cleanly`, and returns exit code 0.

Consequently, a UI initialization/runtime failure can look exactly like “it starts and then immediately stops,” without a useful error in the UI or a non-zero exit status for supervisors.

**Locations:** `src/aria/ui/urwid_tui.py:631-659`; the successful-shutdown path is `src/aria/__main__.py:278-302`.

**Suggested remediation:** Catch and record all exceptions in the UI thread, propagate them to `run()`, and make `main()` print the exception and return non-zero. A controlled fallback to the Rich UI would be preferable when possible.

### BR-4 — Persistent memory is enabled even when no embedding backend is usable

**Severity:** Medium  
**Status:** Confirmed from logs

Memory initializes successfully but every semantic-memory operation fails when the configured Ollama embedding models are absent/unreachable. The logs contain repeated instances of:

```text
MemoryManager: remember failed: No embedding backend is available: None
MemoryManager: historical memory skipped (embeddings unavailable?): No embedding backend is available: None
```

This does not terminate the process, but it leaves a default-enabled advertised feature non-functional and repeatedly emits errors during normal interaction.

**Locations:** `config.yaml:260-273`, `src/aria/memory_pkg/embeddings.py:79-139`.

**Suggested remediation:** Probe and report memory health once at startup; if no backend is available, enter an explicit SQLite/session-only degraded mode and avoid repeated embedding attempts until a retry interval expires.

## Verification performed

- `uv run aria --help` succeeded.
- `pytest -q` completed with **166 passed** and **9 errors**. The errors are all LifeOS tests that create a localhost HTTP server; this sandbox denies socket creation (`PermissionError: [Errno 1] Operation not permitted`), so they do not identify a project failure.
- `pyright` could not run because the `pyright` executable is not installed in the resolved environment.
- `git diff --check` found pre-existing trailing whitespace in `src/aria/__main__.py:260`; it is not related to startup behavior.

## Recommended triage order

1. Remove or bypass the stale runtime model override and validate the effective Ollama model.
2. Decouple application startup from SearXNG/Docker availability.
3. Propagate urwid thread failures instead of treating them as clean shutdowns.
4. Make missing embedding support a visible, low-noise degraded mode.
