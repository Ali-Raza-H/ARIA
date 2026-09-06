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
- **Multi-provider** - Gemini, Mistral, NVIDIA NIM, OpenRouter, OpenAI (all
  through one OpenAI-compatible adapter: base URL + key), and local Ollama.
  Ollama uses
  ARIA's custom `<tool_call>` protocol by default so models without native tool
  calling still work; native tools are opt-in per provider configuration.
- **Model lists & quick switching** - each provider keeps a model list;
  `/providers`, `/models`, `/model`, `/provider` switch at runtime. Runtime
  preferences survive restarts in the ignored `data/aria-state.yaml` file;
  the main `config.yaml` is never rewritten.
- **`.env` based secrets** - API keys and `ARIA_*` settings overrides live in
  `.env` (see `.env.example`), loaded with `python-dotenv`.
- **Full rotating logging** - `data/logs/debug.log` (everything, the complete
  data flow: function calls, arguments, results, errors), `info.log` (INFO+),
  and `error.log` (ERROR only). 5 MiB per file, 3 rotated backups. Secrets are
  redacted before anything hits disk.
- **Live chain of thought** - watch ARIA's reasoning as it happens: every
  round, tool call with arguments, and tool result streams inside the response
  panel. `/cot on|off` toggles it.
- **Chat-style TUI** - the conversation tail fills the screen from the top and
  the prompt box stays pinned to the bottom row, chat-app style. `/save`
  exports the session transcript to a text file.
- **Switchable speech** - Kokoro through Hugging Face (`kokoro_hf`) or the
  local `KPipeline` backend (`kokoro_local`), plus Chatterbox for local custom
  voice cloning; off by default, `/tts on|off|kokoro_hf|kokoro_local|chatterbox`.
- **LifeOS connection** - manage your real life through the LifeOS API
  (tasks, projects, goals, habits, calendar, journal, health, gym, finance...)
  via the `lifeos` tool; enabled by simply setting `lifeos.base_url` and
  `LIFEOS_API_KEY`, silently inert otherwise.
- **Massive operations** - the coder agent owns bulk tools (`search_workspace`,
  `batch_edit`) plus filesystem/shell tools, a 60-iteration default budget, and
  its own ephemeral memory file. Its provider and model can be selected
  independently in the `coder` section of `config.yaml`.

## Setup

Install `uv`, then create the project-local Python 3.11 environment and install
dependencies:

```bash
uv venv --python 3.11
uv sync --extra dev
```

Configure and run:

```bash
cp config.example.yaml config.yaml
cp .env.example .env          # then fill in the API key(s) you use
uv run aria
```

The independent coding agent can use a different backend from ARIA. Set these
under `coder` in `config.yaml`; either value may be omitted. If both are
omitted, the coder uses ARIA's provider and model. If only `provider` is set,
the first model listed for that provider is selected.

```yaml
coder:
  provider: ollama
  model: qwen2.5-coder:7b
  max_iterations: 60
  max_output_chars: 12000
```

No runtime command is required; restart ARIA after changing these settings.

### Runtime settings persistence

Changes made through the REPL are saved automatically to
`data/aria-state.yaml`, a separate local file that is ignored by Git. This
includes the active provider/model, workspace, ARIA and coder iteration limits,
TTS engine/enabled state, and `/cot` visibility. The YAML configuration remains
the source for provider definitions, credentials, and other static settings.
Delete `data/aria-state.yaml` to return to the values in `config.yaml`.
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
├── logging_setup.py     Rotating 3-file logger, secret redaction, log_call tracing
├── prompts.py           ARIA's persona and the coder agent's mission prompt
├── memory.py            Ephemeral JSON session memory
├── agent/
│   ├── base.py          Shared agent loop (model rounds, tool execution, limits)
│   ├── aria.py          AriaAgent: conversational assistant + deploy_coder tool
│   └── coder.py         CoderAgent + CoderService: independent deployments
├── llm/
│   ├── base.py          Provider protocol and normalized message types
│   ├── openai_compat.py One adapter for Gemini/Mistral/NVIDIA/OpenRouter/OpenAI
│   ├── ollama_provider.py  Local Ollama adapter
│   └── factory.py       ProviderManager: model lists, runtime switching
├── tools/
│   ├── base.py          Tool/ToolContext/ToolResult contracts
│   ├── registry.py      Tool registry with execution logging
│   ├── router.py        <tool_call> text protocol for non-native models
│   ├── filesystem.py    list/read/write/edit + search_workspace + batch_edit
│   ├── lifeos.py        LifeOS API connection (ported from CIEL)
│   └── shell.py         Run shell commands with optional limits
├── speech/
│   └── __init__.py      SpeechController + Kokoro/Chatterbox backends
└── ui/
    └── repl.py          Rich REPL and the /command suite
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
| `/cot [on\|off]` | Show/hide the live chain of thought (default on) |
| `/tts on\|off\|kokoro_hf\|kokoro_local\|chatterbox` | Speech on/off or engine switch |
| `/memory` | Session memory statistics |
| `/clear` | Fresh conversation (rebuilds the system prompt) |
| `/save [path]` | Export the session transcript to a text file |
| `/status` | Full configuration overview |
| `/logs` | Log file locations and sizes |
| `/quit`, `/exit` | Leave |

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
