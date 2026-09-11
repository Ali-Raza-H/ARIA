# Quick start

This is the shortest reliable path to a working ARIA installation. It assumes Linux and a local Ollama setup; hosted providers can be substituted later.

## 1. Install

```bash
git clone https://github.com/Ali-Raza-H/ARIA.git
cd ARIA
uv sync --extra dev --extra tui
```

## 2. Prepare Ollama

Start Ollama and pull a chat model:

```bash
ollama pull gemma2:9b
```

For persistent semantic memory, also pull an embedding model:

```bash
ollama pull qwen3-embedding:0.6b
```

## 3. Configure

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

The default example selects Ollama. You can initially leave `.env` empty when using only local Ollama.

## 4. Launch

```bash
uv run aria
```

You should see the effective provider/model/workspace before the TUI opens.

## 5. Have a first conversation

Try a normal request first. Then verify the tool loop:

```text
/trace on
```

Ask ARIA for a small, safe filesystem inspection in the configured workspace. Trace output should show tool activity without pretending to expose private model reasoning.

## 6. Test runtime model switching

```text
/providers
/models
/model gemma2:9b
```

Runtime choices are saved in `data/state/aria-state.yaml`, not written back into `config.yaml`.

## 7. Test memory

```text
/memory
/memory facts
```

Give ARIA a harmless preference you want it to remember, then later ask a related question. Persistent memory is split between exact structured facts and semantic/history retrieval; see [Memory architecture](../architecture/MEMORY.md).

## 8. Enable web research

Install Docker if necessary and start the bundled SearXNG deployment:

```bash
docker compose -f infrastructure/searxng/docker-compose.yml up -d
```

ARIA's example configuration points to `http://127.0.0.1:8080`. Once reachable, ARIA can expose bounded search and webpage tools without a third-party search API key.

## 9. Enable the coding agent

The coder is already part of the architecture. Give it a separate provider/model if desired:

```yaml
coder:
  provider: ollama
  model: qwen2.5-coder:7b
  max_iterations: 60
  max_output_chars: 12000
```

Then ask ARIA for work that genuinely benefits from a separate multi-file coding session. The coder has its own model context and iteration budget so a large refactor does not consume ARIA's conversational context.

## 10. Enable browser automation

```yaml
browser:
  enabled: true
```

Install Chromium:

```bash
uv run playwright install chromium
```

The browser is ARIA-only and uses a persistent profile. Never reuse your normal browser's profile directory.

## 11. Enable desktop control

Start with:

```yaml
desktop:
  enabled: true
  mode: managed
```

On Arch/Hyprland:

```bash
sudo pacman -S hyprland wtype ydotool grim slurp
```

Configure named application launchers rather than expecting arbitrary GUI programs to be inferred through the shell tool.

## 12. Enable scheduling carefully

First enable the scheduler without consequential autonomy:

```yaml
scheduler:
  enabled: true
  analysis_enabled: true

autonomy:
  enabled: false
  allowed_categories: []
```

Read-only briefings and monitoring can then be explored without granting LifeOS writes, computer control, or other consequential categories.

## 13. Recommended progression

Do not enable everything at once. A sensible progression is:

1. Core chat + Ollama.
2. Persistent memory + embeddings.
3. Coding agent.
4. SearXNG web research.
5. Browser automation.
6. Managed desktop control.
7. Speech.
8. Scheduler/read-only analysis.
9. Carefully scoped autonomy.
10. Gmail and other external write integrations.

This makes failures easy to isolate and keeps the initial trust boundary small.
