# Installation

This guide takes ARIA from a clean checkout to a verified first launch. ARIA currently targets **Python 3.11** (`>=3.11,<3.12`), and the repository defines the `aria` console entry point in `pyproject.toml`.

## 1. Prerequisites

### Required

- Git
- Python 3.11
- `uv` is recommended for dependency and virtual-environment management.
- A model provider: either a local Ollama server or credentials for one of the configured hosted providers.

### Recommended

- Linux/Wayland for the full desktop-control feature set.
- Ollama for local chat and local embeddings.
- Docker for the optional self-hosted SearXNG web backend.

## 2. Clone and enter the repository

```bash
git clone https://github.com/Ali-Raza-H/ARIA.git
cd ARIA
```

Verify Python:

```bash
python --version
```

It should report Python 3.11.x. Do not assume a newer Python version is supported just because it happens to run part of the code: the package metadata deliberately constrains the supported interpreter range.

## 3. Install the base environment

Recommended development installation:

```bash
uv sync --extra dev --extra tui
```

This installs the normal runtime dependencies plus development tooling and the modern urwid TUI.

For a standard pip-based installation, the repository also provides:

```bash
pip install -r requirements.txt
```

The canonical packaging metadata remains `pyproject.toml`; `requirements.txt` exists as a convenient complete dependency list for straightforward environments.

## 4. Install optional feature dependencies

Install only the features you intend to use.

### Voice

Hosted Kokoro:

```bash
uv sync --extra voice
```

Local Kokoro:

```bash
uv sync --extra voice-local
```

Chatterbox:

```bash
uv sync --extra voice-chatterbox
```

### Desktop fallback

If the configured Wayland tools are unsuitable and you want PyAutoGUI:

```bash
uv sync --extra desktop
```

### Browser runtime

Playwright is a normal runtime dependency, but Chromium's browser runtime must be installed separately:

```bash
uv run playwright install chromium
```

## 5. Arch Linux host packages

ARIA's core Python package does not install operating-system utilities. Optional capabilities therefore need their host dependencies separately.

For SearXNG/Docker:

```bash
sudo pacman -S docker docker-compose
sudo systemctl enable --now docker
```

For local Kokoro:

```bash
sudo pacman -S espeak-ng
```

For audio playback:

```bash
sudo pacman -S portaudio pipewire pipewire-alsa pipewire-pulse
```

For Hyprland desktop control:

```bash
sudo pacman -S hyprland wtype ydotool grim slurp
```

`wtype` and `ydotool` have Wayland/session-specific permission requirements. If they are unsuitable, use the `pyautogui` backend where appropriate, understanding that PyAutoGUI has its own Wayland limitations.

For local embedding with Ollama:

```bash
sudo pacman -S ollama
sudo systemctl enable --now ollama
ollama pull qwen3-embedding:0.6b
```

## 6. Create configuration files

Copy the templates:

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

`config.yaml` contains static behavior and provider definitions. `.env` contains secrets and environment overrides. Never commit real credentials.

### Minimal local Ollama configuration

The example configuration already contains an Ollama provider. Install a chat model and make sure Ollama is reachable:

```bash
ollama pull gemma2:9b
ollama list
```

Then ensure:

```yaml
provider: ollama
model: gemma2:9b
```

The independent coder can use a separate model, for example:

```yaml
coder:
  provider: ollama
  model: qwen2.5-coder:7b
  max_iterations: 60
  max_output_chars: 12000
```

### Hosted providers

Each hosted provider has separate environment-variable names for ARIA and the coder. For example, a provider definition can contain:

```yaml
aria_api_key_env: ARIA_OPENROUTER_API_KEY
coder_api_key_env: CODER_OPENROUTER_API_KEY
```

The corresponding values belong in `.env`:

```dotenv
ARIA_OPENROUTER_API_KEY=your-key-here
CODER_OPENROUTER_API_KEY=your-coder-key-here
```

The coder deliberately does not fall back to ARIA's credential. This allows the two agents to use separate accounts, quotas, or providers.

## 7. Start ARIA

Using uv:

```bash
uv run aria
```

Or as a Python module:

```bash
python -m aria
```

If installed into an activated environment, the console script is also:

```bash
aria
```

## 8. Command-line options

```text
-c, --config PATH
    Use a configuration file other than config.yaml.

--ignore-state
    Ignore data/state/aria-state.yaml for this run. Static config.yaml values win.

--reset-state
    Delete data/state/aria-state.yaml and then start as though --ignore-state was supplied.
```

Examples:

```bash
uv run aria --config ./config.yaml
uv run aria --ignore-state
uv run aria --reset-state
```

## 9. Verify the installation

At startup ARIA reports its effective provider, model, and workspace. Confirm that the expected values are shown.

Then test a simple conversation. Next test the capabilities you intentionally enabled:

```text
/providers
/models
/model <model-name>
/provider <provider-name>
/trace on
```

For memory, check `/memory`. For telemetry, check `/telemetry`. For timers, use `/timer list`.

If web search is enabled, verify SearXNG separately rather than treating a missing backend as an ARIA installation failure. ARIA is designed to continue without optional web infrastructure.

## 10. Enable SearXNG

The repository includes a Docker deployment under `infrastructure/searxng/`.

Start it manually:

```bash
docker compose -f infrastructure/searxng/docker-compose.yml up -d
```

The example ARIA configuration expects:

```yaml
web:
  enabled: true
  start_backend: false
  searxng_url: http://127.0.0.1:8080
```

`start_backend: false` means ARIA will never start Docker during ordinary startup. If SearXNG is unavailable, ARIA warns and continues; the web tools report backend errors until SearXNG is reachable.

`start_backend: true` is an explicit opt-in that allows ARIA to attempt to start the bundled Compose backend when necessary.

## 11. Memory setup

ARIA's persistent memory uses SQLite for structured facts and Chroma for semantic/history retrieval. Install Ollama and at least one embedding model for local semantic retrieval:

```bash
ollama pull qwen3-embedding:0.6b
```

If embeddings are unavailable, ARIA enters degraded mode instead of refusing to start. Structured facts/preferences can continue to work while semantic recall is unavailable.

## 12. Browser setup

Enable the browser in `config.yaml`:

```yaml
browser:
  enabled: true
```

Then install Chromium:

```bash
uv run playwright install chromium
```

The browser uses a persistent profile directory. **Do not point it at the profile of an already-running everyday browser.** ARIA's automation may navigate, click, type, upload, and download, so treat the profile as an agent-controlled browser session.

## 13. Desktop setup

Enable desktop control and choose the managed mode first:

```yaml
desktop:
  enabled: true
  mode: managed
```

Configure named launchers:

```yaml
launchers:
  terminal: [kitty]
  browser: [firefox]
```

Managed mode is the recommended starting point. Unrestricted mode exposes significantly more local control and should only be enabled intentionally.

## 14. Scheduler and autonomy

Scheduling is in-process. Jobs persist in SQLite, but they execute only while ARIA itself is running.

Start with read-only analysis:

```yaml
scheduler:
  enabled: true
  analysis_enabled: true

autonomy:
  enabled: false
  allowed_categories: []
```

Only add consequential categories after reviewing the workflow definitions and audit behavior.

## 15. Installation troubleshooting

### `python` is the wrong version

Install/select Python 3.11 and recreate the environment. The project metadata intentionally does not declare Python 3.12 support.

### Provider initialization fails

Check the selected provider, model name, API-key environment variable, and whether the model exists. ARIA validates stale runtime state and can fall back to an available model.

### Persistent memory is degraded

Check that Chroma is installed and that at least one embedding backend is reachable. With Ollama, verify:

```bash
ollama list
ollama pull qwen3-embedding:0.6b
```

Then restart ARIA.

### Web search is unavailable

Start SearXNG manually and confirm `web.searxng_url`. ARIA should still launch successfully because web search is optional.

### Browser tools cannot launch

Run `uv run playwright install chromium` and check `browser.executable_path` if using a system browser.

### Desktop input fails

Check the Wayland session, `wtype`/`ydotool` permissions, and configured command paths. Consider `input_backend: pyautogui` only if its limitations are acceptable.

### TUI dependency is missing

Install `uv sync --extra tui`. The repository also retains the Rich backend as an alternative.

## 16. Production/local deployment notes

ARIA is primarily a local terminal application, not a server daemon. Protect `.env`, OAuth files, browser profiles, runtime databases, and the workspace itself using normal OS permissions. Avoid running the process with broader privileges than necessary. The most security-sensitive settings are browser persistence, unrestricted desktop control, shell access, Gmail writes, Docker controls, and autonomous computer-control workflows.
