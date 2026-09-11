# Troubleshooting

This guide is organized by symptom. ARIA is designed to degrade optional subsystems independently, so a warning about web, memory embeddings, speech, browser, or desktop support does not necessarily mean the core assistant is broken.

## First-response checklist

Before changing multiple things at once:

1. Confirm Python is 3.11.
2. Confirm the virtual environment/dependencies are installed.
3. Confirm `config.yaml` exists and was copied from the current example.
4. Confirm `.env` exists when hosted credentials are required.
5. Check the startup provider/model line.
6. Inspect only `data/logs/aria.log` for application logs.
7. Reproduce with optional services disabled if the cause is unclear.

## ARIA will not start

### Configuration error

Validate that `config.yaml` is valid YAML and that values match the supported schema.

Try the example again:

```bash
cp config.example.yaml config.yaml
```

Do not overwrite a working configuration without backing it up first.

### Python/package error

Check:

```bash
python --version
uv sync --extra dev --extra tui
```

ARIA currently targets Python 3.11 (`>=3.11,<3.12`).

### Provider error

Check the selected provider and model. For Ollama:

```bash
ollama list
```

For hosted providers, check that the environment variable named by the provider's `aria_api_key_env` exists.

## ARIA starts but says the model is unavailable

Runtime state can contain an old provider/model selection. ARIA validates the effective choice and can fall back to a working model.

To bypass saved runtime state once:

```bash
uv run aria --ignore-state
```

To clear it:

```bash
uv run aria --reset-state
```

## Tool calls do not work

Check whether the feature is enabled. Disabled features do not register their tools.

For provider models without reliable native tool calling, confirm the provider's `native_tools` setting and portable tool-call compatibility. Do not add arbitrary fields to OpenAI-compatible assistant messages: many APIs reject unknown message fields.

Enable trace temporarily:

```text
/trace on
```

Trace describes execution events; it is not a private chain-of-thought viewer.

## Memory is degraded

This normally means an embedding backend is unavailable.

Check:

```bash
ollama list
```

Pull the recommended embedding model:

```bash
ollama pull qwen3-embedding:0.6b
```

Restart ARIA after restoring the backend. Structured facts may continue to work while semantic recall is unavailable.

## Memory seems not to remember something

First determine which tier should contain the information:

- exact durable fact → Tier 1 SQLite;
- contextual/session memory → Tier 2 Chroma;
- old conversational detail → Tier 3 history.

Use:

```text
/memory
/memory facts
/memory search <text>
/memory summarize
```

A transient statement does not necessarily become a durable fact. Promotion uses validation and confidence/importance/support thresholds.

## Web search is unavailable

Check SearXNG:

```bash
docker compose -f infrastructure/searxng/docker-compose.yml ps
```

Start it:

```bash
docker compose -f infrastructure/searxng/docker-compose.yml up -d
```

Check that `web.searxng_url` matches the running endpoint.

ARIA should continue starting even when SearXNG is unavailable.

## Webpage cannot be opened

Private/local destinations are blocked by design. If the destination is intentionally internal, review `web.allowed_hosts` rather than bypassing the SSRF protection globally.

Also check page timeout/size/concurrency limits.

## Browser cannot launch

Install the Chromium runtime:

```bash
uv run playwright install chromium
```

If using a system browser, verify `browser.executable_path`.

Do not point `profile_directory` at a browser profile that is currently in use by your normal browser.

## Desktop input does not work

Check host tools:

```bash
which hyprctl
which wtype
which ydotool
which grim
```

On Arch:

```bash
sudo pacman -S hyprland wtype ydotool grim slurp
```

Wayland input utilities can require additional session permissions. If necessary, try the PyAutoGUI backend:

```bash
uv sync --extra desktop
```

and configure `desktop.input_backend: pyautogui`.

## Desktop actions are blocked

Check `desktop.mode` and `desktop.enabled`. Managed mode intentionally restricts the action surface. Some raw dispatch/keybind/shell functionality exists only in unrestricted mode.

Do not switch to unrestricted mode simply to make a single action work; determine which managed capability is missing first.

## Speech does not work

Check the selected engine and optional extra.

Hosted Kokoro:

```bash
uv sync --extra voice
```

Local Kokoro:

```bash
uv sync --extra voice-local
sudo pacman -S espeak-ng portaudio pipewire pipewire-alsa pipewire-pulse
```

Chatterbox:

```bash
uv sync --extra voice-chatterbox
```

Then inspect:

```text
/tts status
```

Speech backends load lazily and can disable themselves after an unavailable dependency/audio device is detected.

## Scheduler jobs do not run

Remember that the scheduler is in-process. Jobs execute only while ARIA is open.

Check:

1. `scheduler.enabled`;
2. `scheduler.analysis_enabled` for analysis jobs;
3. workflow file location;
4. workflow `enabled` flag;
5. cron expression;
6. category allowlist;
7. LifeOS operation allowlist where applicable;
8. scheduler audit records.

Use:

```text
/scheduler
```

## Autonomous action was blocked

Inspect the workflow category and autonomy configuration.

For example:

```yaml
autonomy:
  enabled: true
  allowed_categories:
    - analysis
    - notifications
  lifeos_write_operations: []
```

Adding `lifeos_writes` requires explicit operation names as well.

## Coding agent fails

Check the coder provider/model and credential environment variable. Remember that the coder uses `coder_api_key_env`, not ARIA's key.

If a coding task exhausts its budget, consider whether the task should be split rather than blindly increasing `max_iterations`.

Check the resulting repository state before reporting a large coding task as complete.

## Gmail integration fails

Check:

- `gmail.enabled`;
- MCP access token environment variable;
- direct REST fallback configuration if the requested operation is not supported by the MCP route;
- OAuth token/client-secret file paths;
- required scopes;
- network access to Google's endpoints.

Never paste OAuth tokens into issues or logs.

## Telemetry/logging problems

The application log should be:

```text
data/logs/aria.log
```

If debugging instructions tell you to create additional persistent log directories, update the instructions: the current project policy is to keep runtime logging centralized.

Telemetry is metadata-oriented and can be inspected with:

```text
/telemetry
```

## Last-resort isolation procedure

If the cause remains unclear:

1. Stop ARIA.
2. Back up `config.yaml`.
3. Run with a minimal local Ollama provider.
4. Disable browser, desktop, Gmail, Docker, system, web, scheduler, speech, and periodic screen context.
5. Run the core conversation.
6. Re-enable one subsystem at a time.
7. After each change, reproduce the original symptom.

This isolates optional service failures from the core agent/provider loop.
