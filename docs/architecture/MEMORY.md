# Memory architecture

ARIA uses a hybrid memory design because not every piece of information should be represented as a vector. Exact facts, preferences, and durable structured state need deterministic storage; conversational history and contextual recollections benefit from semantic retrieval.

## Storage model

```text
                         MemoryManager
                              │
             ┌────────────────┴────────────────┐
             │                                 │
        SQLite store                         Chroma
             │                                 │
      exact structured data          semantic/history collections
             │                                 │
      Tier 1 hard facts              Tier 2 semantic memories
                                      Tier 3 chat history
```

The default runtime data lives under `data/memory/`.

## Tier 1 — hard facts

SQLite stores structured facts and registry metadata. A fact has machine-readable attributes such as its namespace, confidence, importance, provenance, and replacement history.

This tier is appropriate for information where exactness matters, for example a durable preference or an explicitly confirmed value. Structured facts are not automatically deleted merely because they become old; explicit memory wipe operations are available instead.

The memory extractor applies promotion thresholds so a transient model statement does not immediately become a durable fact. The balanced defaults described by the current implementation use confidence `0.80`, importance `0.70`, and at least two supporting sessions.

When a newer qualifying value replaces an active fact, the older value remains available in fact history rather than silently disappearing.

## Tier 2 — semantic memory

Chroma stores concise session summaries and extracted contextual memories. These records are designed for similarity retrieval rather than exact database lookup.

Typical questions for Tier 2 are:

- "What was I working on recently?"
- "What approach did we discuss for this project?"
- "Have we talked about this design before?"

The active model's memory extraction step creates summaries after turns. A background worker processes queued work and retries failures rather than making the interactive turn depend on successful embedding generation.

## Tier 3 — cross-session chat history

Raw cross-session messages are indexed separately. Retrieval combines semantic similarity with other signals so a highly similar but ancient message does not automatically dominate a recent explicit reference.

The ranking implementation considers factors including similarity, recency, frequency, importance, and explicit-reference signals. The exact ranking weights are configurable under `memory`.

Tier 3 is useful when the user remembers a conversation but the information was never promoted to a structured fact or concise semantic memory.

## Retrieval strategy

ARIA uses the type of question to determine the most useful source:

```text
Exact structured question ───────► SQLite
Historical/contextual question ─► Chroma
Mixed question ─────────────────► SQLite + Chroma → merge/rank
```

Retrieved memory is token-bounded. It is inserted into the agent's prompt as context and is explicitly separated from instructions.

## Embeddings

Embedding is local-first.

1. If configured, an OpenAI-compatible embedding endpoint is attempted.
2. Otherwise ARIA tries the configured Ollama fallback models.
3. If no backend is available, semantic memory enters degraded mode.

The documented local fallback order is:

1. `qwen3-embedding:0.6b` — recommended lightweight local default.
2. `nomic-embed-text` — lightweight general-purpose alternative.
3. `mxbai-embed-large` — stronger but heavier.
4. `bge-m3` — larger multilingual option.
5. `snowflake-arctic-embed` — efficient embedding family.

For Ollama:

```bash
ollama pull qwen3-embedding:0.6b
```

Remote embedding requests are redacted before transmission. Likely credentials, bearer tokens, API keys, and machine-specific paths are removed from text sent to a remote embedding endpoint.

## Degraded mode

Semantic memory is deliberately non-fatal. If every embedding backend is unavailable:

- ARIA can still start;
- structured facts/preferences remain usable;
- semantic vector retrieval is temporarily empty;
- queued records can be retried later;
- the user sees a startup warning rather than repeated per-turn failures.

This design prevents a broken optional embedding service from turning a conversational assistant into an unusable application.

## Extraction pipeline

After a conversation turn:

```text
Turn complete
   │
   ├── queue raw history indexing
   └── queue memory extraction
          │
          ▼
      active model
          │
          ▼
      JSON candidate data
          │
          ├── direct JSON
          ├── balanced embedded JSON
          └── deterministic fallback when model output is unusable
          │
          ▼
      validate candidates
          │
          ├── structured fact → SQLite
          └── semantic memory → Chroma
```

The extractor is expected to tolerate direct JSON, fenced/prose-wrapped JSON, provider failures, malformed candidates, and missing model responses. Deterministic classification remains the fallback rather than discarding all persistence because a model response was imperfect.

## Retention

The following settings control retention:

- `memory.episodic_retention_days`
- `memory.conversation_retention_days`
- `memory.knowledge_retention_days`

`0` means never expire that class automatically.

Low-value stale memories can decay in importance before deletion. Confirmed and important memories are not automatically removed solely because of age. Structured facts remain until explicitly wiped.

## Memory commands

```text
/memory                         Show memory status
/memory facts                   List active Tier 1 facts
/memory search <text>           Search Tier 2 and Tier 3
/memory summarize               Process pending summaries/promotions
/memory promote                 Alias for manual promotion processing
/memory retention               Apply retention immediately
/memory wipe session DELETE     Delete current session's persistent records
/memory wipe 1 DELETE           Delete Tier 1 facts
/memory wipe 2 DELETE           Delete Tier 2 summaries
/memory wipe 3 DELETE           Delete Tier 3 history
/memory wipe all DELETE         Delete all persistent memory
/clear                          Clear only current session state
```

Destructive wipes require the exact uppercase `DELETE` confirmation.

## Session memory vs persistent memory

If persistent memory is disabled, unavailable, or fails during startup, ARIA falls back to session-oriented memory storage under `data/sessions/`. This allows the conversation to continue without silently pretending that durable memory exists.

The old ephemeral JSON session files are not automatically migrated.

## Operational guidance

When debugging memory:

1. Check `/memory` for the current backend/degraded state.
2. Check Ollama with `ollama list` if using local embeddings.
3. Confirm `data/memory/` exists and is writable.
4. Inspect only the single supported application log at `data/logs/aria.log`.
5. Use `/memory summarize` to process pending extraction work manually.
6. Do not delete SQLite/Chroma data casually: those stores contain durable user state.
