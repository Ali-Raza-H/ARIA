# ARIA deployment and operations

ARIA is a local process, not a server. The scheduler and browser automation run only while ARIA is open. Persistent state is stored below `data/` and should be backed up according to its privacy requirements.

## Install

```bash
uv sync --extra tui
cp config.example.yaml config.yaml
cp .env.example .env
# edit config.yaml and .env
uv run playwright install chromium
uv run aria
```

For a system Python installation, use the repository's `requirements.txt`, but keep the Python version at 3.11.

## Arch Linux host packages

Install only what is needed:

```bash
sudo pacman -S hyprland wtype ydotool grim
sudo pacman -S portaudio pipewire pipewire-alsa pipewire-pulse
sudo pacman -S espeak-ng
```

`wtype` and `ydotool` require a correctly configured Wayland session. If Arch input is unavailable, install the desktop optional extra and select `desktop.input_backend: pyautogui`, noting that PyAutoGUI has separate Wayland limitations.

## Browser

ARIA's Playwright browser uses the bundled Chromium unless `browser.executable_path` is set. If launch fails, run:

```bash
uv run playwright install chromium
```

A native browser opened by `desktop_launch` is independent; configure its executable under `desktop.launchers.browser` (for example `[firefox]` or `[zen]`). Do not share `browser.profile_directory` with an already-running everyday browser.

## SearXNG

SearXNG is optional. Start the bundled deployment with:

```bash
docker compose -f infrastructure/searxng/docker-compose.yml up -d
```

ARIA never starts Docker unless `web.start_backend: true`. If SearXNG is unavailable, normal chat continues and web tools return a clear unavailable result.

## Ollama

Install Ollama separately and pull a chat model plus at least one embedding model:

```bash
ollama pull gemma2:9b
ollama pull qwen3-embedding:0.6b
```

Set the same host in `providers.ollama.host` and `memory.embedding_ollama_host` when non-default. `/ollama clear-vram` unloads active models.

## Credentials

Put hosted API keys in `.env` using the names referenced by `config.yaml`. ARIA and coder keys are intentionally separate. `HF_TOKEN` is used by hosted Kokoro; `LIFEOS_API_KEY` is used by LifeOS; `MEMORY_EMBEDDING_API_KEY` is used by optional remote embeddings. Never commit `.env` or paste secrets into prompts.

## Security posture

Keep `desktop.mode: managed` and `autonomy.enabled: false` unless the expanded capabilities are deliberate. Unrestricted desktop shell, persistent browser cookies, LifeOS writes, uploads, downloads, and computer-control workflows can affect real accounts and machines. Review `autonomous_audit` records and narrow allowlists.

## Service behavior

- Logs are written to `data/logs/aria.log` with rotation.
- Memory is under `data/memory/`; session memory is under `data/sessions/`.
- Scheduler state and audit are in `data/scheduler/scheduler.sqlite3`.
- Browser cookies are under `data/browser-profile/`.
- Timers and jobs do not run while ARIA is closed.
- Scheduled briefings and timer completions appear in the active chat and, when enabled, desktop notifications/TTS.

## Troubleshooting

- **Browser launch error:** install Playwright Chromium or set a valid executable path.
- **Launcher not configured:** call `desktop_launch` with a name present in `desktop.launchers`, not an executable path; add the route to `config.yaml`.
- **Hyprland 422 `custom_tool_calls`:** current code uses standard assistant fields only; restart after updating.
- **Keyboard chord does not work:** use `desktop_input` with `modifiers` and ensure `wtype` is recent and functional in the session.
- **Clipboard image unavailable:** direct binary Ctrl+V into a terminal is not supported. Use `/attach clipboard` with `wl-paste` or `xclip`, or `/attach /path/to/image.png`.
- **TTS sounds mechanical:** ARIA removes Markdown and converts ISO timestamps to conversational clock/date phrases before synthesis. Adjust `speech.voice`, `speech.speed`, and engine.
- **No briefing content:** ensure `scheduler.enabled` and `scheduler.analysis_enabled` are true; a background provider is optional because deterministic fallback summaries still work.
- **Memory extraction fallback:** malformed model JSON is safely parsed when possible, then deterministic rules are used.

## Updating

Pull source changes, run `uv sync`, review configuration changes against `config.example.yaml`, and restart ARIA. Runtime state can be reset with `uv run aria --reset-state`.
