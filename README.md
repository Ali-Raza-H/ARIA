# ARIA — Adaptive Reasoning and Intelligence Assistant

ARIA is a **local-first Python 3.11 terminal personal assistant** designed around a simple idea: keep the normal conversation clean, while giving the assistant real tools and an independent coding agent for heavy work.

It can converse, reason, use tools, remember information across sessions, search the web through self-hosted SearXNG, control a persistent Playwright browser, operate a Hyprland desktop, schedule proactive work, speak responses, process images/screenshots, connect to LifeOS and Gmail, control media/system/Docker resources, and deploy a separate coding agent for large repository tasks.

> **Status:** active personal-project codebase. The documentation describes the current repository implementation and configuration surface; it should be updated alongside behavior changes.

## What makes ARIA different?

ARIA is not just a chat UI around an API call. Its runtime is composed of several cooperating subsystems:

```text
                           ┌─────────────────────┐
                           │       User / TUI    │
                           └──────────┬──────────┘
                                      │
                                      ▼
                           ┌─────────────────────┐
                           │    ARIA Agent Loop  │
                           └──────────┬──────────┘
                                      │
             ┌────────────────────────┼────────────────────────┐
             │                        │                        │
             ▼                        ▼                        ▼
      Provider Manager          Tool Registry            Memory Manager
             │                        │                        │
      hosted / Ollama        ┌────────┼────────┐       SQLite + Chroma
                             │        │        │
                             ▼        ▼        ▼
                           shell   browser  desktop ...
                                      │
                                      ▼
                              Independent Coder
                                      │
                                      ▼
                              large coding tasks
```

The architecture deliberately separates conversational context from expensive implementation work. ARIA can ask its coder to perform a large multi-file operation without filling the main conversation with every intermediate search, edit, build, and test result.

## Highlights

### 🤖 Multi-provider LLM support

ARIA normalizes several hosted providers behind a common OpenAI-compatible adapter and also supports local Ollama.

Configured provider families include:

- Cerebras
- Google Gemini
- Groq
- NVIDIA NIM
- OpenRouter
- OpenAI
- Mistral
- Z.ai
- Ollama

Each provider owns its model inventory. Models/providers can be changed at runtime without rewriting the static YAML configuration.

### 🧑‍💻 Independent coding agent

The `deploy_coder` tool launches a separate coding-agent session with:

- its own provider/model selection;
- its own iteration budget;
- its own context/memory;
- filesystem and shell-oriented coding tools;
- bulk workspace search/edit capabilities;
- a larger default iteration budget than the normal conversational agent.

This is intended for multi-file edits, refactors, debugging, builds, tests, and other tasks where an ordinary conversational turn would become unwieldy.

### 🧠 Hybrid persistent memory

ARIA deliberately does not put everything into a vector database.

- **Tier 1 — SQLite:** exact structured facts, preferences, provenance, confidence, importance, and replacement history.
- **Tier 2 — Chroma:** semantic session summaries and extracted contextual memories.
- **Tier 3 — Chroma:** cross-session chat history.

Retrieval is query-aware and token-bounded. Structured questions can use SQLite, historical/contextual questions can use Chroma, and mixed questions can combine both.

Local Ollama embeddings are preferred when available. If embedding infrastructure disappears, ARIA degrades gracefully instead of refusing to start.

### 🌐 Self-hosted web research

ARIA's `web_search` and `open_webpage` tools use a local SearXNG deployment.

The web subsystem provides:

- bounded search calls;
- bounded page fetches;
- page/result size limits;
- retries and timeouts;
- caching;
- concurrent-fetch limits;
- source tracking/citation support;
- SSRF protection for private/local destinations.

No proprietary search API key is required.

### 🌍 Persistent browser automation

Optional Playwright control gives ARIA an independent persistent Chromium profile for navigation, tabs, snapshots, clicks, typing, keyboard input, screenshots, downloads, and uploads.

Browser state survives restarts. Use a **dedicated ARIA browser profile**; never point it at an already-running everyday browser profile.

### 🖥️ Hyprland desktop control

ARIA can optionally inspect and control a Hyprland desktop:

- monitors;
- windows;
- workspaces;
- validated dispatchers;
- named application launchers;
- keyboard/mouse input;
- screenshots;
- keybind inspection/editing in the appropriate mode.

`managed` mode is the recommended default. `unrestricted` mode exposes raw Hyprland dispatch, marked keybind edits, configured desktop shell commands, and arbitrary desktop shell access.

### 📅 Proactive scheduling

The in-process scheduler supports:

- persistent jobs and timers;
- morning/afternoon/end-of-day briefings;
- deadline/goal/calendar/routine checks;
- local working-style/profile analysis;
- optional LifeOS writes;
- configurable autonomy categories;
- notifications and optional TTS;
- immutable scheduler audit records.

Jobs survive restarts in SQLite, but **they execute only while ARIA is running**.

### 🔊 Speech

Supported speech engines include:

- hosted Kokoro through Hugging Face;
- local Kokoro `KPipeline`;
- Chatterbox local custom voice support.

Speech is optional and disabled unless configured. ARIA strips Markdown syntax and conversationalizes values such as ISO timestamps before synthesis.

### 👁️ Multimodal input

ARIA can queue ephemeral image input from:

- clipboard images;
- image files;
- screenshots.

Native vision-capable models receive image parts. Text-only models can use a configured visual fallback provider. Periodic screen context is a separate opt-in scheduler capability.

### 🧩 Skills

Markdown files under `skills/` act as live behavior extensions. They are re-read every turn, so a skill can change behavior without restarting ARIA.

Skills can describe research procedures, coding conventions, personality preferences, workflows, or any other reusable instruction set.

### ✉️ Gmail, media, system, and Docker integrations

Optional tool groups include:

- Google's official Gmail MCP endpoint;
- separately enabled Gmail REST fallback;
- playerctl/native media routes;
- system metrics, systemd and journal inspection;
- Docker CLI operations.

Consequential operations have explicit tool-level boundaries and confirmation behavior where implemented. Treat credentials and OS permissions as part of the security model.

## Quick start

### 1. Install

Python 3.11 is required.

```bash
git clone https://github.com/Ali-Raza-H/ARIA.git
cd ARIA
uv sync --extra dev --extra tui
```

A pip-oriented complete dependency list is also available:

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

For local Ollama, install/pull a model such as:

```bash
ollama pull gemma2:9b
```

The example configuration uses Ollama by default.

### 3. Run

```bash
uv run aria
```

Equivalent entry points:

```bash
python -m aria
aria
```

The console script is defined by the package metadata.

### 4. First checks

Inside the TUI, try:

```text
/providers
/models
/trace on
/memory
```

See the [Quick Start](docs/getting-started/QUICKSTART.md) for a progressive setup and the [Commands](docs/getting-started/COMMANDS.md) reference for interactive controls.

## Optional features

### Web search

Install/start Docker and the bundled SearXNG deployment:

```bash
docker compose -f infrastructure/searxng/docker-compose.yml up -d
```

Then use the configured `web.searxng_url` (default `http://127.0.0.1:8080`).

### Persistent memory

Install Ollama and pull an embedding model:

```bash
ollama pull qwen3-embedding:0.6b
```

ARIA uses SQLite plus Chroma. If embeddings are unavailable, it continues in degraded mode.

### Browser

```bash
uv run playwright install chromium
```

Then set `browser.enabled: true`. Keep the persistent profile separate from your daily browser.

### Hyprland desktop

On Arch Linux, the example stack uses:

```bash
sudo pacman -S hyprland wtype ydotool grim slurp
```

Enable `desktop.enabled` and start with `desktop.mode: managed`.

### Speech

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

### Desktop fallback

```bash
uv sync --extra desktop
```

This installs PyAutoGUI for the optional desktop input backend.

## Runtime configuration

ARIA separates static configuration from runtime preferences.

```text
config.yaml                       static source of truth
.env                              secrets + supported environment overrides
data/state/aria-state.yaml        ignored runtime UI preferences
data/logs/aria.log                single rotating application log
```

The configuration precedence is:

1. built-in defaults;
2. `config.yaml`;
3. whitelisted runtime preferences from `data/state/aria-state.yaml`;
4. supported `ARIA_*` environment overrides.

Runtime state can change selected operational preferences such as provider/model, workspace, iteration limits, speech state, coder limits, and trace settings. It does not replace the security-sensitive static configuration.

Reset runtime state with:

```bash
uv run aria --reset-state
```

Or ignore it for one run:

```bash
uv run aria --ignore-state
```

See [Configuration](docs/configuration/CONFIGURATION.md) for the detailed reference.

## Data layout

Generated runtime data belongs under `data/`:

```text
data/
├── logs/
│   └── aria.log
├── memory/
│   ├── assistant.db
│   └── chroma/
├── scheduler/
│   └── scheduler.sqlite3
├── sessions/
├── state/
│   └── aria-state.yaml
├── browser-profile/
└── gmail/
```

Keep generated runtime state out of Git. In particular, do not commit credentials, OAuth tokens, browser profiles, memory databases, scheduler state, or logs.

## Project structure

The important source areas are:

```text
ARIA/
├── src/aria/
│   ├── __main__.py            Application startup and CLI
│   ├── agent/                 ARIA and independent coder agents
│   ├── config/                YAML/.env/state loading and validation
│   ├── core/                  Shared agent/tool-loop components
│   ├── llm/                   Provider adapters and model management
│   ├── memory/                SQLite + Chroma memory subsystem
│   ├── services/              Coder, scheduler, speech, vision, notifications
│   ├── skills/                Skill loading/management
│   ├── telemetry/             Operational telemetry
│   ├── tools/                 Tool contracts and implementations
│   └── ui/                    urwid and Rich interfaces
├── docs/                      Maintained documentation
├── infrastructure/            Self-hosted supporting services such as SearXNG
├── skills/                    User-editable Markdown behavior extensions
├── workflows/                 User-editable scheduled workflows
├── tests/                     Unit/integration tests
├── config.example.yaml        Complete annotated configuration template
├── .env.example               Credential/environment template
├── pyproject.toml              Package metadata and optional dependencies
└── requirements.txt            Complete pip dependency list
```

## Architecture at a glance

Startup is orchestrated by `src/aria/__main__.py`:

1. Parse CLI arguments.
2. Load YAML, `.env`, and allowed runtime state.
3. Configure the single rotating log.
4. Initialize the provider manager and validate the model choice.
5. Register configured tools.
6. Initialize optional web, browser, desktop, memory, speech, vision, LifeOS, telemetry, scheduler, and proactive services.
7. Construct `AriaAgent`.
8. Construct the selected TUI backend.
9. Start background scheduler work only after the chat sink exists.
10. Run the interactive loop.

The conversational turn then follows the model/tool iteration documented in [Agent Loop](docs/architecture/AGENT_LOOP.md).

## Security model

ARIA has access to potentially sensitive local and remote capabilities. Important trust boundaries include:

- `.env` and OAuth credentials;
- filesystem/workspace access;
- shell execution;
- persistent authenticated browser state;
- Hyprland desktop control;
- Gmail write/delete operations;
- Docker/system operations;
- scheduled computer control;
- periodic screen context.

Start with optional capabilities disabled or in managed/read-only configurations and enable them deliberately. In particular, `desktop.mode: unrestricted` should be treated as full trusted-local-agent control.

Read [Security and Trust Boundaries](docs/architecture/SECURITY.md) before enabling autonomous or high-impact integrations.

## Documentation

The documentation is intentionally much more detailed than the README. Start here:

| Area | Documentation |
|---|---|
| Installation | [Installation](docs/getting-started/INSTALLATION.md) |
| First run | [Quick Start](docs/getting-started/QUICKSTART.md) |
| Architecture | [Architecture](docs/architecture/ARCHITECTURE.md) |
| Agent loop | [Agent Loop](docs/architecture/AGENT_LOOP.md) |
| Memory | [Memory Architecture](docs/architecture/MEMORY.md) |
| Security | [Security Model](docs/architecture/SECURITY.md) |
| Configuration | [Configuration Reference](docs/configuration/CONFIGURATION.md) |
| Coding agent | [Coding Agent](docs/features/CODING_AGENT.md) |
| Web research | [Web Research](docs/features/WEB_RESEARCH.md) |
| Scheduling | [Scheduler](docs/features/SCHEDULER.md) |
| Development | [Development Guide](docs/development/DEVELOPMENT.md) |
| Integrations | [Gmail/System/Media](docs/integrations/GMAIL_SYSTEM_MEDIA.md) |
| Skills | [Skills](skills/README.md) |
| Workflows | [Workflows](workflows/README.md) |

## Development

Use the development extra:

```bash
uv sync --extra dev --extra tui
```

Run tests:

```bash
uv run pytest
```

Run type checking:

```bash
uv run pyright
```

The development guide explains repository boundaries, adding tools, provider compatibility, memory extraction, UI callbacks, and testing expectations.

## Design principles

ARIA follows these principles throughout the codebase:

1. **Local-first where practical.** Optional infrastructure should degrade rather than prevent chat startup.
2. **Provider-neutral internals.** Provider-specific APIs are normalized behind common message/tool abstractions.
3. **Separate contexts.** Conversational work and heavy coding work use independent contexts.
4. **Explicit capabilities.** High-impact features are opt-in configuration rather than accidental defaults.
5. **Bounded execution.** Tool calls, web fetches, model iterations, command output, and other expensive operations have limits.
6. **Auditable autonomy.** Scheduled actions have categories, allowlists, and persistent audit records.
7. **Secrets outside source.** Credentials belong in `.env` or local credential files, never committed YAML.
8. **Documentation follows implementation.** When behavior changes, the corresponding documentation should change in the same work.

## License

See the repository's license file for the current project licensing terms.
