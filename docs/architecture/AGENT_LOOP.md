# ARIA agent loop

ARIA's core conversational behavior is an iterative model/tool loop. The important distinction is that ARIA itself is the conversational agent, while the coding agent is a separate service with its own provider/model, memory file, and iteration budget.

## High-level lifecycle

```text
User input
   │
   ├── queued image attachments (optional, first request only)
   │
   ▼
BaseAgent.run()
   │
   ├── add user message
   ├── retrieve bounded memory context
   ├── load active skills
   │
   ▼
Build provider messages
   │
   ▼
Model request
   │
   ├── streamed assistant text
   ├── native tool calls
   └── custom <tool_call> protocol
          │
          ▼
      ToolRegistry
          │
          ▼
      Tool execution
          │
          └── ToolResult
   │
   ▼
Append results and iterate
   │
   └── stop when the model has no further calls/errors
   │
   ▼
Queue memory extraction + history indexing
```

## 1. Input normalization

The user message is added to the active session. If the user has queued an image through `/attach` or `/screen`, the image is attached to the first provider request only. Attachments are ephemeral unless a feature explicitly persists derived information.

The agent also obtains the bounded memory context available for the current turn. Memory is context, not authority: retrieved memory is inserted into a delimited block and should not be interpreted as a replacement for current user instructions or tool policy.

## 2. Message construction

The agent builds a provider-compatible message list containing the system prompt, active skill instructions, relevant memory, conversation history, and the current user input. Tool schemas are exposed through the provider's supported tool mechanism or the portable text protocol.

Message construction is deliberately conservative. OpenAI-compatible APIs can reject assistant messages containing fields they do not recognize, so internal bookkeeping for custom tool calls is never serialized as an arbitrary `custom_tool_calls` field.

## 3. Provider call

The provider adapter normalizes different model APIs into ARIA's internal message/tool-call representation. A provider may stream text or return a completed assistant message.

Provider selection is independent for ARIA and the coder. Hosted credentials are also independent: the coder never silently reuses ARIA's API key.

## 4. Tool-call parsing

ARIA supports two tool-call paths:

### Native calls

Models with verified native function/tool calling return structured tool calls through the provider API. These are normalized to ARIA's `ToolCall` representation.

### Portable text protocol

For providers/models that do not reliably support native tools, ARIA can parse a custom form:

```text
<tool_call>{"name":"tool_name","arguments":{"key":"value"}}</tool_call>
```

The parser must tolerate the model's surrounding text without turning arbitrary prose into a tool invocation. Parsed calls are merged with native calls before execution.

## 5. Tool registry

`ToolRegistry` owns the mapping from model-visible tool names to implementation handlers and schemas. Tool registration occurs during startup based on configuration.

Typical groups include:

- filesystem
- shell
- browser
- Hyprland desktop
- web/SearXNG
- memory
- scheduler/timers
- LifeOS
- Gmail
- system/media/Docker
- vision/image attachment tools
- `deploy_coder`

Tools should validate their arguments at the service boundary and return a bounded textual result. Failures are represented as tool errors so the model can decide whether to recover, retry, or explain the failure.

## 6. Iteration budget

Each turn is bounded by `max_iterations`. This prevents a malformed tool loop or model that continually calls tools from running forever.

The coder has its own, normally much larger `coder.max_iterations` budget. That separation is intentional: a large repository refactor can require dozens of search/edit/test iterations without consuming ARIA's normal conversational loop budget.

## 7. Tool results

Native tool results are returned using standard provider `tool` messages. Results produced through the portable text protocol use ARIA's supported user-role envelope rather than inventing provider-specific message fields.

Tool output is bounded where appropriate by configuration, such as `max_command_output_chars`, `coder.max_output_chars`, web page limits, and image limits.

## 8. Confirmation boundaries

Some tool families use `ConfirmationManager` for consequential operations. The confirmation decision is made at the tool/service boundary, not by trusting the model's prose.

Examples include disruptive Gmail/system/Docker operations. Desktop/browser capabilities are intentionally configured as trusted-local-agent features; in particular, the current desktop implementation does not add a confirmation prompt for every click, typing action, upload, download, or window operation.

Autonomous scheduled actions have a separate category/allowlist boundary and persistent audit path.

## 9. Completion

The conversational loop ends when there are no further tool calls/errors requiring another round or when the iteration budget is exhausted. The assistant's final text is delivered to the UI.

After the turn, memory extraction/history indexing is queued so persistence does not unnecessarily block the interactive response.

## 10. Coding-agent delegation

`deploy_coder` creates/uses an independent `CoderService`. The coder receives a task, operates against the configured workspace using its own tool loop, and reports a result back to ARIA.

The architectural purpose is context isolation:

- ARIA remembers the conversation and acts as the coordinator.
- The coder receives a focused implementation task.
- Large search/edit/build/test output remains in the coder's context.
- The coder can use a different provider/model.
- The coder has its own iteration/output limits.

This is not merely a shell shortcut. It is a separate agent execution path.

## 11. Error handling and degradation

Optional services should not make the entire assistant fail. Startup therefore initializes subsystems independently where possible:

- missing SearXNG → web tools become unavailable while chat continues;
- missing embeddings → structured memory can remain available while semantic recall degrades;
- missing browser runtime → browser tools report a clear installation problem;
- unavailable speech dependency/device → speech disables itself and chat continues;
- unavailable background/vision provider → that optional role is disabled;
- stale runtime model → ARIA selects a working model rather than failing every turn.

This degradation strategy is a core part of ARIA's runtime contract and should be preserved when adding new optional services.
