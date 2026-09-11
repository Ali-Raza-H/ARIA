# ARIA configuration reference

Copy `config.example.yaml` to `config.yaml`. Keep secrets in `.env`. Relative paths resolve from the directory where ARIA is launched.

## Precedence

1. Built-in defaults in `src/aria/config/loader.py`.
2. `config.yaml`.
3. Whitelisted `data/state/aria-state.yaml` preferences written by the UI.
4. `ARIA_*` environment overrides.

Runtime state can change provider, model, workspace, iteration limits, speech engine/enabled state, coder iterations, and UI trace settings. It cannot change credentials or security-sensitive tool configuration. Use `--ignore-state` or `--reset-state` to ignore it.

## Top level

- `provider`: active provider name: `cerebras`, `gemini`, `groq`, `nvidia`, `openrouter`, `openai`, `mistral`, `zai`, or `ollama`.
- `model`: active model ID.
- `persona`: persona selector. The current built-in persona is `jarvis`; prompts are editable in `src/aria/prompts.py`.
- `workspace`: root used by filesystem and shell tools.
- `max_iterations`: maximum ARIA model/tool rounds.
- `command_timeout_seconds`: shell timeout, or `null` for the default safety timeout.
- `max_command_output_chars`: output cap, or `null` for no configured cap.

## providers

Each provider is a mapping. Hosted providers require `base_url`, `aria_api_key_env`, `coder_api_key_env`, and `models`. Ollama uses `host`, `models`, and `native_tools`. `native_tools: false` uses ARIA's portable text tool protocol; enable native tools only for a verified model. Optional lists `image_input_models` and `image_generation_models` declare multimodal capabilities.

## coder

`provider` and `model` are optional fallbacks to ARIA's active choice. `max_iterations` controls coder rounds. `max_output_chars` caps command output passed to the coder.

## browser

- `enabled`: expose Playwright tools.
- `headless`: hide/show the browser.
- `profile_directory`: persistent cookies and login state; never share with an already running daily browser.
- `executable_path`: optional Chromium-family executable path. Empty uses Playwright's bundled browser.
- `timeout_ms`, `max_text_chars`: action timeout and page text limit.
- `wait_until`: `commit`, `domcontentloaded`, `load`, or `networkidle`.
- `viewport_width`, `viewport_height`: browser viewport.
- `launch_args`: extra Chromium arguments.

The Playwright browser and the native desktop `browser` launcher are different routes. Configure both explicitly if you want both capabilities.

## desktop

- `enabled`: expose Hyprland/desktop tools.
- `mode`: `managed` or `unrestricted`. Managed is recommended.
- `input_backend`: `arch` for `wtype`/`ydotool` or `pyautogui`.
- `hyprctl_command`, `wtype_command`, `ydotool_command`, `screenshot_command`: executable names or paths.
- `hyprland_config_path`: required for marked keybind edits in unrestricted mode.
- `command_timeout_seconds`: subprocess timeout.
- `launchers`: route map from a name to an argv list, e.g. `browser: [firefox]`, `terminal: [kitty]`. These are the only supported native app launch routes.

## gmail

`enabled` registers explicit Gmail tools. The official Developer Preview MCP endpoint is configured with `mcp_url` and uses the OAuth access token named by `access_token_env`; it currently covers search, message/thread reads, drafts, labels, and unlabel operations. The direct REST fallback is controlled by `direct_api_enabled` and uses `direct_api_access_token_env` for send, reply, forward, trash, permanent delete, labels, filters, settings, contacts, and attachment downloads. `api_base_url` and `people_api_base_url` select the Google REST APIs. `oauth_client_secrets_path`, `oauth_token_path`, and `oauth_scopes` document/store local OAuth material; ARIA reads an existing token file but does not print credentials. Direct write/destructive operations ask for confirmation unless the current conversational request clearly names the action and target. Google’s official MCP setup uses `gmail.readonly` and `gmail.compose`; the broader fallback scopes are listed in the example config.

## media, system, and docker

Each section has an `enabled` switch and an executable command path. `media.players` maps named routes such as `yt`, `spotify`, and `termusic` to argv arrays. `system` exposes bounded monitoring, `systemctl`, and journal logs. `docker` uses the Docker CLI only and exposes read-only inspection plus start/stop/restart, Compose, pull, prune, removal, and exec operations. Disruptive operations require confirmation unless the user's conversational request directly specifies the action and target. Keep Docker CLI permissions narrow at the OS level.

## web

Controls local SearXNG: `enabled`, `start_backend`, `searxng_url`, request timeouts, retry count, result/page/call limits, cache TTLs, concurrency, `allowed_hosts`, and `user_agent`. `start_backend` is opt-in and may run Docker Compose.

## lifeos

`enabled`, `base_url`, `api_key_env`, `timeout_seconds`, `max_retries`, and `retry_backoff_seconds`. Empty `base_url` makes the integration inert.

## background

`enabled`, optional `provider`/`model`, optional `api_key_env`, `profile_path`, and `max_output_chars`. Background analysis is disabled when no provider/model is selected, while deterministic data collection can still run.

## autonomy

- `enabled`: permit consequential autonomous actions.
- `allowed_categories`: `analysis`, `lifeos_writes`, `notifications`, `computer_control`, `timers`.
- `lifeos_write_operations`: explicit LifeOS write operation names.

Read-only analysis still requires `scheduler.analysis_enabled`, but not the autonomy write allowlist.

## scheduler

`enabled`, `analysis_enabled`, SQLite `database`, `workflow_directory`, `poll_seconds`, `default_workflows`, `timezone` (currently `system`), `default_misfire_policy` (`skip` or `run_once`), three `briefing_times`, check intervals, and `briefing_web_queries`.

## notifications

`enabled`, `backend` (`notify-send` or `dbus`), command paths, `app_name`, urgency (`low`, `normal`, `critical`), timeout, and `tts_enabled`. Scheduler results are sent to chat as well as this backend while a UI is active.

## vision

`enabled`, optional visual fallback provider/model/key env, `max_image_bytes`, clipboard backend (`auto`, `wl-paste`, `xclip`, `python`), screenshot command, `retain_images`, and opt-in periodic screen settings. Image attachments are ephemeral by default.

## speech

`enabled`, `engine` (`kokoro_hf`, `kokoro_local`, `chatterbox`; legacy `kokoro` aliases to hosted Kokoro), `voice`, `speed`, `lang_code`, `hf_api_key_env`, `hf_model`, and `hf_provider`. Install the matching optional dependency extra. Speech receives Markdown-stripped, conversationalized text.

## memory

`enabled`, SQLite `directory`, Chroma `chroma_directory`, optional remote embedding URL/model/key env, Ollama host and fallback model list, context budget, ranking weights, and retention days. A retention value of `0` means never expire.

## telemetry

`enabled` turns on the SQLite recorder, `database` selects its path, `live` enables in-process event listeners/UI updates, `retention_days` controls pruning (`0` keeps records indefinitely), and `record_estimates` stores approximate token counts when a provider does not report usage. Telemetry records provider/model, role, request message count, context character size, estimated and provider-reported input/output/total/cached tokens, model/tool latency, tool success/error counts, turn duration, iteration count, and error metadata. It deliberately does not store prompt, response, tool argument, or tool result contents; only lengths and a short argument hash are recorded. Use `/telemetry` for an aggregate snapshot.

## logging

`directory` (the only supported log location should be `data/logs`), `console_level`, `max_bytes`, and `backup_count`. The file handler writes DEBUG through CRITICAL.

## ui

`backend`: `urwid` or `rich`; `show_cot`: live execution trace; `keep_cot`: retain trace in the transcript.

## Environment overrides

Supported examples include:

```dotenv
ARIA_PROVIDER=ollama
ARIA_MODEL=gemma2:9b
ARIA_CODER_MAX_ITERATIONS=100
ARIA_CODER_MAX_OUTPUT_CHARS=20000
ARIA_SPEECH_ENABLED=true
ARIA_SPEECH_ENGINE=kokoro_local
ARIA_WORKSPACE=/home/me/project
ARIA_COMMAND_TIMEOUT_SECONDS=30
ARIA_MAX_COMMAND_OUTPUT_CHARS=12000
ARIA_LOG_DIR=data/logs
```

Provider credentials are named by the provider blocks. Never put actual key values in YAML or documentation.
