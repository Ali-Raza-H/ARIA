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
  OpenAI (all through one OpenAI-compatible adapter: base URL + key), and local
  Ollama.
  Ollama uses
  ARIA's custom `<tool_call>` protocol by default so models without native tool
  calling still work; native tools are opt-in per provider configuration.
- **Model lists & quick switching** - each provider keeps a model list;
  `/providers`, `/models`, `/model`, `/provider` switch at runtime. Runtime
  preferences survive restarts in the ignored `data/aria-state.yaml` file;
  the main `config.yaml` is never rewritten.
- **`.env` based secrets** - Each hosted provider has separate ARIA and coder
  credentials (`aria_api_key_env` / `coder_api_key_env`); secrets and `ARIA_*`
  settings overrides live in `.env` (see `.env.example`).
- **Full rotating logging** - `data/logs/debug.log` (everything, the complete
  data flow: function calls, arguments, results, errors), `info.log` (INFO+),
  and `error.log` (ERROR only). 5 MiB per file, 3 rotated backups. Secrets are
  redacted before anything hits disk.
- **Live chain of thought** - watch ARIA's reasoning as it happens: every
  round, tool call with arguments, and tool result streams inside the response
  panel. `/cot on|off` toggles it.
- **Chat-style TUI** - the conversation fills the screen with a visible
  scrollbar; PageUp/PageDown browse older/newer messages and Home/End jump to
  either end while the prompt stays pinned. `/save` exports the transcript.
- **Human-like three-tier memory** - persistent Tier 1 hard facts in SQLite,
  Tier 2 semantic summaries in Chroma, and Tier 3 cross-session chat history in
  a separate Chroma collection. Memory warms the first prompt with facts,
  relevant semantic memories, and one recent exchange.
- **Switchable speech** - Kokoro through Hugging Face (`kokoro_hf`) or the
  local `KPipeline` backend (`kokoro_local`), plus Chatterbox for local custom
  voice cloning; off by default, `/tts on|off|kokoro_hf|kokoro_local|chatterbox`.
- **Self-hosted web research** - bounded `web_search` and `open_webpage` tools backed by local SearXNG, with source tracking, citations, extraction, caching, retries, and SSRF protection; no search API key is required.
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

### Persistent memory

Persistent memory is enabled by default and requires the local `chromadb`
package. ARIA stores SQLite metadata/facts in `data/memory/memory.sqlite3` and
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

Install Chroma with `uv sync`, install Ollama separately, and pull at least one
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

The default retention policy keeps raw Tier 3 content for 30 days, archives it
with lower retrieval priority, and removes archived records after one year.
Tier 2 summaries are retained for two years; Tier 1 facts are retained until
explicitly wiped. Change `memory.detail_mode` to `raw` or
`delete_after_summary` when appropriate.

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
│   ├── openai_compat.py One adapter for Cerebras/Groq/Gemini/Mistral/NVIDIA/OpenRouter/OpenAI
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
| `/memory [action]` | Persistent memory status, search, facts, summarization, retention, or wipe |
| `/clear` | Clear the current persistent session while retaining long-term facts |
| `/save [path]` | Export the session transcript to a text file |
| `/status` | Full configuration overview |
| `/logs` | Log file locations and sizes |
| `/ollama clear-vram` | Unload all currently running Ollama models |
| `/quit`, `/exit` | Leave |

When entering a message, use PageUp/PageDown to scroll one viewport and
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
