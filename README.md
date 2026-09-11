# ARIA - Adaptive Reasoning and Intelligence Assistant

A modular terminal personal assistant for Python 3.11. ARIA talks like a
personal assistant (Jarvis-style persona) and deploys an **independent coding
agent** for heavy work: multi-file edits, large refactors, builds, and other
massive operations run in their own session with their own model context and a
much larger iteration budget, so ARIA's conversation stays clean.

## Highlights

- **Persona & behavior** - ARIA greets, chats, plans, and answers like a
  personal assistant; work on your machine is delegated to the coding agent
  through the `deploy_coder` tool and reported back in plain language.
- **Multi-provider** - Cerebras, Groq, Gemini, Mistral, NVIDIA NIM, OpenRouter,
  OpenAI, Z.ai (all through one OpenAI-compatible adapter: base URL + key), and
  local Ollama.
  Ollama uses
  ARIA's custom `<tool_call>` protocol by default so models without native tool
  calling still work; native tools are opt-in per provider configuration.
- **Model lists & quick switching** - each provider keeps a model list;
  `/providers`, `/models`, `/model`, `/provider` switch at runtime. Runtime
  preferences survive restarts in the ignored `data/state/aria-state.yaml` file;
  the main `config.yaml` is never rewritten.
- **`.env` based secrets** - Each hosted provider has separate ARIA and coder
  credentials (`aria_api_key_env` / `coder_api_key_env`); secrets and `ARIA_*`
  settings overrides live in `.env` (see `.env.example`).
- **Centralized rotating logging** - every level from DEBUG through CRITICAL is
  written to the single `data/logs/aria.log` file (5 MiB, 3 rotated backups).
  Secrets are redacted before anything hits disk.
- **Execution trace** - inspect rounds, tool calls, and tool results as they
  happen without exposing or claiming to expose private model reasoning.
  `/trace on|off` toggles it; `/cot` remains a deprecated alias.
- **Chat-style TUI** - the conversation owns the terminal screen while open;
  mouse-wheel scrolling browses older/newer messages and Home/End jump to
  either end while the prompt stays pinned. `/save` exports the transcript.
- **Human-like three-tier memory** - persistent Tier 1 hard facts in SQLite,
  Tier 2 semantic summaries in Chroma, and Tier 3 cross-session chat history in
  a separate Chroma collection. Memory warms the first prompt with facts,
  relevant semantic memories, and one recent exchange.
- **Switchable speech** - Kokoro through Hugging Face (`kokoro_hf`) or the
  local `KPipeline` backend (`kokoro_local`), plus Chatterbox for local custom
  voice cloning; off by default, `/tts on|off|kokoro_hf|kokoro_local|chatterbox`.
- **Markdown-aware speech** - TTS engines read the words, not the syntax:
  emphasis, links, fences, and tables are stripped before synthesis.
- **Skills folder** - drop Markdown files into `skills/` to shape personality,
  behavior, and workflows; re-read every turn, `/skills` lists what is active.
- **Two TUI backends** - modern urwid TUI by default (`--extra tui`), legacy
  Rich full-screen REPL still available via `ui.backend: rich`.
- **Self-hosted web research** - bounded `web_search` and `open_webpage` tools backed by local SearXNG, with source tracking, citations, extraction, caching, retries, and SSRF protection; no search API key is required.
- **Playwright browser control** - optional ARIA-only persistent Chromium automation for navigation, tabs, snapshots, clicks, typing, key presses, screenshots, downloads, and uploads. Install the browser binary with `playwright install chromium`.
- **Hyprland desktop control** - optional ARIA-only desktop tools for monitors, windows, workspaces, keybind inspection, managed dispatch, configured app launchers, keyboard/mouse input, and screenshots. `desktop.mode: unrestricted` additionally enables raw Hyprland dispatch, marked keybind edits, and arbitrary desktop shell commands. Configured launchers are preferred over shell commands.
- **Gmail, media, and system operations** - optional explicit-schema tools for Google's official Gmail MCP, a separately enabled Gmail API fallback, playerctl media control, native player routes, system metrics, systemd, journal logs, and Docker CLI operations. Consequential actions ask for confirmation unless the current request clearly gives the action and target.
- **Proactive LifeOS work** - optional in-process cron scheduling with persistent SQLite jobs, morning/afternoon/end-of-day briefings, deadline/goal/calendar/routine checks, conservative calendar-conflict signals, inferred local working-style profiles, configurable LifeOS writes, category allowlists, and an immutable autonomous-action audit log.
- **Reminders and notifications** - persistent or session-only timers, alarms, pomodoros, stopwatches, pause/resume/restart/finish controls, Dunst-compatible `notify-send` delivery, and optional TTS.
- **Multimodal input** - ephemeral clipboard/file/screen image attachments, native provider image messages, Ollama image normalization, and a configured visual-model fallback for text-only models. Periodic screen context is separately opt-in.
  (tasks, projects, goals, habits, calendar, journal, health, gym, finance...)
  via the `lifeos` tool; enabled by simply setting `lifeos.base_url` and
  `LIFEOS_API_KEY`, silently inert otherwise.
- **Massive operations** - the coder agent owns bulk tools (`search_workspace`,
  `batch_edit`) plus filesystem/shell tools, a 60-iteration default budget, and
  its own ephemeral memory file. Its provider and model can be selected
  independently in the `coder` section of `config.yaml`.

## Setup

Recommended development installation (Python 3.11):

```bash
uv sync --extra dev --extra tui
```

For a simple pip installation of the complete normal dependency set:

```bash
pip install -r requirements.txt
```(`--extra tui` installs urwid for the default TUI; without it ARIA falls back
 to the legacy Rich interface. Use `--extra voice`/`--extra voice-local` for
 speech as described below. Playwright is a normal dependency; install its
 Chromium runtime with `uv run playwright install chromium`. Install the
 optional PyAutoGUI fallback with `uv sync --extra desktop`.)


### System packages (Arch Linux)

ARIA's optional features need a few host packages. Install the ones you plan to
use with `pacman` or `yay`:

```bash
# Web search backend (local SearXNG)
sudo pacman -S docker docker-compose

# Local Kokoro speech (voice-local)
sudo pacman -S espeak-ng

# Hosted Kokoro / Chatterbox / local speech playback (sounddevice)
sudo pacman -S portaudio pipewire pipewire-alsa pipewire-pulse
```

Optional but useful for the full local voice + embedding stack:

```bash
sudo pacman -S ollama
ollama pull qwen3-embedding:0.6b
```

For Hyprland input on Arch, install the utilities selected in `config.yaml`:

```bash
sudo pacman -S hyprland wtype ydotool grim slurp
```

`wtype`/`ydotool` require the permissions and setup appropriate to your local
Wayland session. If those utilities do not work, set `desktop.input_backend:
pyautogui` and install `uv sync --extra desktop`; PyAutoGUI has different
Wayland limitations and is not a universal replacement.

Docker is only required to run the optional SearXNG deployment; ARIA never starts Docker during normal startup:

```bash
sudo systemctl enable --now docker
docker compose -f infrastructure/searxng/docker-compose.yml up -d
```

Web search is optional: if SearXNG is unreachable at startup, ARIA prints a
warning and continues without the web tools. The runtime data root is `data/`;
logs are written only to `data/logs/aria.log`. Memory works the same way — with
no embedding backend available, ARIA starts in a degraded
mode that keeps structured facts/preferences and skips semantic recall until
Ollama is back.

Configure and run:

```bash
cp config.example.yaml config.yaml
cp .env.example .env          # then fill in the API key(s) you use
uv run aria
# or: python -m aria
```

Command-line flags:

```text
-c, --config PATH   Path to config.yaml (default: config.yaml)
    --ignore-state  Ignore data/state/aria-state.yaml for this run; config.yaml wins
    --reset-state   Delete data/state/aria-state.yaml, then behave like --ignore-state
```

The independent coding agent can use a different backend from ARIA. Set these
under `coder` in `config.yaml`; either value may be omitted. If both are
omitted, the coder uses ARIA's provider and model. If only `provider` is set,
the first model listed for that provider is selected. Hosted providers require
separate credentials: ARIA uses `aria_api_key_env`, while the coder uses
`coder_api_key_env`; the coder never falls back to ARIA's key.

For Ollama, `/ollama clear-vram` unloads all currently running models. If a
request reports an out-of-memory error, ARIA automatically runs the same cleanup
and retries the request once.

```yaml
coder:
  provider: ollama
  model: qwen2.5-coder:7b
  max_iterations: 60
  max_output_chars: 12000
```

No runtime command is required; restart ARIA after changing these settings.

### Proactive work, scheduling, and multimodal input

The scheduler is intentionally **in-process**. Jobs and timers are persisted in
`data/scheduler/scheduler.sqlite3`, but no work runs while ARIA is closed.
Read-only briefings and monitoring run when `scheduler.analysis_enabled: true`;
write/autonomy permissions remain separately controlled by `autonomy.enabled` and
`autonomy.allowed_categories`. Enable it only after reviewing
`autonomy.allowed_categories`; each scheduled attempt, including blocked and
failed actions, is recorded in `scheduler.sqlite3`.

Additional YAML workflows belong in `workflows/`; the complete schema and
examples are documented in [`workflows/README.md`](workflows/README.md). The
built-in schedules cover three daily briefings and periodic deadline, goal,
calendar, and profile checks. Briefings can include configured SearXNG queries
through `scheduler.briefing_web_queries`, and calendar conflicts are surfaced
as deterministic signals before the background model writes prose.

Timers are available through `/timer` or model tools:

```text
/timer create focus pomodoro 1500
/timer start <id>
/timer pause <id>
/timer resume <id>
/timer restart <id>
/timer finish <id>
/timer list
```

`/attach clipboard`, `/attach /path/to/image.png`, and `/screen` queue an
image for the next message. Images are ephemeral. Native image-capable models
receive image parts; text-only models require a configured visual fallback.
Periodic screen analysis is disabled unless both `vision.periodic_screen_enabled`
and its scheduler/autonomy settings are enabled.

### Gmail, media, and system operation

Set `gmail.enabled`, `media.enabled`, `system.enabled`, and/or `docker.enabled` in `config.yaml` to expose the corresponding detailed tools. Gmail uses Google's remote MCP endpoint by default (`https://gmailmcp.googleapis.com/mcp/v1`); see [`docs/integrations/GMAIL_SYSTEM_MEDIA.md`](docs/integrations/GMAIL_SYSTEM_MEDIA.md) for OAuth setup and the direct fallback boundary. System and Docker controls use configured CLI executables, not the Docker socket. Read-only inspection is available without confirmation. Service/container changes and direct Gmail actions require confirmation unless the user's current message clearly requests the exact action and target.

### Browser and desktop operation

Enable `browser.enabled` and/or `desktop.enabled` in `config.yaml`. Browser
state is persisted in `browser.profile_directory`, so cookies and login state
survive restarts; do not point it at a profile used by an already-running
browser. Browser tools are available only to ARIA, not the independent coder.

Desktop defaults to `managed`. It can inspect Hyprland and execute validated
window/workspace actions, use named `desktop.launchers`, send input, and capture
screenshots. Set `desktop.mode: unrestricted` only when you want raw
`hyprctl dispatch`, marked `desktop_set_keybind`, and arbitrary `desktop_shell`.
The current implementation follows the configured no-extra-prompt policy: it
does not ask for confirmation before clicks, typing, downloads, uploads, input,
window changes, app launch/close, or unrestricted commands. Treat unrestricted
mode and persistent browser credentials as trusted-local-agent capabilities.

### Persistent memory

Persistent memory is enabled by default and requires the local `chromadb`
package. ARIA stores SQLite metadata/facts in `data/memory/assistant.db` and
uses two local Chroma collections under `data/memory/chroma`:

- **Tier 1 — hard facts:** structured namespaced facts with confidence,
  importance, provenance, and replacement history.
- **Tier 2 — semantic memory:** concise session summaries and extracted facts.
- **Tier 3 — chat history:** raw cross-session messages, indexed eagerly in the
  background and ranked with similarity, recency, frequency, importance, and
  explicit-reference signals.

After each turn, raw history and a summarization job are queued. A background
worker retries failed jobs every 60 seconds. The active ARIA model performs
summary/fact extraction. Facts are promoted automatically only when they meet
the configurable balanced defaults (confidence `0.80`, importance `0.70`, and
at least two supporting sessions); newer qualifying facts replace older active
values while the previous value remains in fact history.

Embedding setup is local-first. The optional OpenAI-compatible embedding
endpoint is tried first when configured; otherwise Ollama models are tried in
this order:

1. `qwen3-embedding:0.6b` — recommended default for an 8 GB GPU
2. `nomic-embed-text` — lightweight general-purpose alternative
3. `mxbai-embed-large` — stronger but heavier retrieval model
4. `bge-m3` — multilingual, larger/slower option
5. `snowflake-arctic-embed` — efficient model family

Install Chroma with `uv sync` (or `pip install -r requirements.txt`), install Ollama separately, and pull at least one
embedding model, for example:

```bash
ollama pull qwen3-embedding:0.6b
uv sync
```

Remote embedding requests receive redacted text: likely credentials, bearer
tokens, API keys, and machine-specific paths are removed before transmission.
If every embedding backend is unavailable, chat continues and records remain
queued in SQLite for retry; vector retrieval is temporarily empty rather than
blocking the assistant.

Retention is configured through `memory.episodic_retention_days`,
`memory.conversation_retention_days`, and `memory.knowledge_retention_days`
(0 = never expires). Stale low-value memories decay in importance before they
are removed; confirmed and important memories are never auto-deleted purely
for age. Structured facts are retained until explicitly wiped.

Memory commands:

```text
/memory                         Show persistent memory status
/memory facts                   List active hard facts
/memory search <text>           Search Tier 2 and Tier 3
/memory summarize              Process pending summaries/promotions now
/memory promote                Alias for manual promotion processing
/memory retention              Apply retention immediately
/memory wipe session DELETE    Delete the current session's persistent records
/memory wipe 1 DELETE          Delete Tier 1 facts
/memory wipe 2 DELETE          Delete Tier 2 summaries
/memory wipe 3 DELETE          Delete Tier 3 history
/memory wipe all DELETE        Delete all persistent memory
/clear                         Clear only the current session; retain facts
```

Every destructive wipe requires the exact uppercase `DELETE` confirmation.
The old ephemeral JSON session files are not migrated automatically.

All runtime-generated data belongs under `data/`: memory under `data/memory/`,
sessions under `data/sessions/`, scheduler state under `data/scheduler/`, and
runtime settings under `data/state/`.

### Runtime settings persistence

Changes made through the REPL are saved automatically to
`data/state/aria-state.yaml`, a separate local file that is ignored by Git. This
includes the active provider/model, workspace, ARIA and coder iteration limits,
TTS engine/enabled state, and `/trace` visibility. The YAML configuration remains
the source for provider definitions, credentials, and other static settings.
Delete `data/state/aria-state.yaml` (or start with `--ignore-state` / `--reset-state`)
to return to the values in `config.yaml`. At startup ARIA prints the effective
provider/model and which keys the state file overrode, and it validates the
model against the provider's inventory — an unavailable model from stale state
falls back to a working one with a warning instead of failing every turn.
Explicit `ARIA_*` environment variables take precedence over saved runtime
settings.


For speech, install `uv sync --extra voice` for hosted Kokoro, or
`uv sync --extra voice-local` for local Kokoro (`espeak-ng` is also required
by the local pipeline). Use `--extra voice-chatterbox` for Chatterbox.
Hosted Kokoro requires `HF_TOKEN` in `.env`.

## Architecture

```text
src/aria/
├── __main__.py          Entry point: wires config, logging, providers, agents, UI
├── config.py            YAML + .env configuration, validated
├── logging/             Centralized single aria.log handler with secret redaction
├── prompts.py           ARIA's persona and the coder agent's mission prompt
├── memory/              MemoryManager facade plus session/storage internals
├── agent/               Core reasoning and optional coder deployment
│   ├── base.py          Shared agent loop (model rounds, tool execution, limits)
│   ├── aria.py          AriaAgent: conversational assistant + deploy_coder tool
│   └── coder.py         CoderAgent + CoderService: independent deployments
├── llm/
│   ├── base.py          Provider protocol and normalized message types
│   ├── openai_compat.py One adapter for Cerebras/Groq/Gemini/Mistral/NVIDIA/OpenRouter/OpenAI
│   ├── ollama_provider.py  Local Ollama adapter
│   └── factory.py       ProviderManager: model lists, runtime switching
├── tools/
│   ├── base.py          Tool/ToolContext/ToolResult contracts
│   ├── registry.py      Tool registry with execution logging
│   ├── router.py        <tool_call> text protocol for non-native models
│   ├── filesystem.py    list/read/write/edit + search_workspace + batch_edit
│   ├── browser.py       persistent Playwright browser automation (ARIA only)
│   ├── desktop.py       Hyprland/window/workspace/input control (ARIA only)
│   ├── lifeos.py        LifeOS API connection (ported from CIEL)
│   ├── scheduler.py     Persistent cron jobs, timers, and autonomous audit
│   ├── proactive.py     LifeOS briefings, monitoring, and local profile inference
│   ├── notifications.py Dunst-compatible notifications and TTS dispatch
│   ├── images.py        Ephemeral image capture and multimodal routing
│   └── shell.py         Run shell commands with optional limits
├── speech/
│   └── __init__.py      SpeechController + Kokoro/Chatterbox backends
└── ui/
    ├── repl.py          Legacy Rich REPL and the /command suite
    ├── urwid_tui.py     Modern urwid TUI (default backend)
    └── factory.py       UI backend selection (urwid | rich)
```

## Commands

| Command | Effect |
|---|---|
| `/help` | Show all commands |
| `/providers`, `/provider [name]` | List or switch the active provider |
| `/models [provider]`, `/model [name]` | List or switch the active model |
| `/workspace [path]` | Show or change the workspace |
| `/iterations [n]` | ARIA's model/tool round limit |
| `/agent [n]` | The coding agent's iteration limit (default 60) |
| `/trace [on\|off\|keep]` | Show/hide the live execution trace; `/cot` remains a deprecated alias |
| `/tts on\|off\|kokoro_hf\|kokoro_local\|chatterbox` | Speech on/off or engine switch |
| `/memory [action]` | Persistent memory status, search, facts, summarization, retention, or wipe |
| `/timer ...` | Create, control, and list reminders, alarms, pomodoros, and stopwatches |
| `/scheduler` | Show scheduler, workflow, timer, and autonomy status |
| `/profile` | Show locally stored inferred working-style traits |
| `/attach ...`, `/screen` | Queue an ephemeral image or screen capture for the next message |
| `/skills` | List active skill files from the skills/ folder |
| `/clear` | Clear the current persistent session while retaining long-term facts |
| `/save [path]` | Export the session transcript to a text file |
| `/status` | Full configuration overview |
| `browser_*` tools | Persistent Playwright browser actions, when enabled |
| `desktop_*` tools | Hyprland and desktop actions, when enabled |
| `/trace [on\\|off\\|keep]` | Show/hide the execution trace; `/cot` is a deprecated alias |
| `/logs` | Show the single `data/logs/aria.log` location, size, and status |
| `/ollama clear-vram` | Unload all currently running Ollama models |
| `/quit`, `/exit` | Leave |

When entering a message, use the mouse wheel to scroll the transcript and
Home/End to jump to the oldest/newest transcript position.

## Custom Tool Protocol

The system prompt includes the registered tool catalog. A model invokes a tool
by emitting one or more tags in this form:

```text
<tool_call>{"name":"read_file","arguments":{"path":"README.md"}}</tool_call>
```

The agent removes these tags, executes the calls, and returns JSON results
inside `<tool_result>` tags in the next model turn. Malformed calls become
tool errors that the model can correct, so models without native function
calling work fine.

## LifeOS

ARIA talks to LifeOS, the personal-life API (tasks, projects, goals, habits,
calendar, notes, journal, health, diet, gym, finance, events). Set it up:

1. `lifeos.base_url` in `config.yaml` (e.g. `http://127.0.0.1:5000`)
2. `LIFEOS_API_KEY` in `.env`

The model calls one `lifeos` tool with an `operation` (e.g. `list_tasks`,
`create_task`, `complete_task`) and an `arguments` object; path ids are
positive integers, writes carry an automatic idempotency key, and transient
5xx/network errors are retried with backoff. Left unconfigured, the tool
reports that it is not configured instead of breaking the conversation.

## Tests

```bash
uv run pytest
uv run pyright
```

A detailed implementation audit, including current limitations and request
size estimates, is in `docs/reports/REPORT-2026-09-08.md`.
