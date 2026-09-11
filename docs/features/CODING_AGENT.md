# Independent coding agent

ARIA includes a dedicated coding agent for work that would be wasteful or disruptive inside the main conversational context. The entry point is the `deploy_coder` tool, which is registered against a `CoderService` and executes an independent coding-agent session.

## Why it exists

A normal assistant turn is optimized for conversation. Large coding tasks are different: they may require repeated repository searches, many file edits, shell commands, builds, tests, and retries. Feeding all of that into the primary conversation would consume context and make the assistant harder to use.

ARIA therefore separates the roles:

```text
User
 │
 ▼
ARIA conversational agent
 │
 └── deploy_coder(task)
          │
          ▼
     CoderService
          │
          ▼
     independent CoderAgent
          │
          ├── search workspace
          ├── inspect files
          ├── edit files
          ├── execute commands
          ├── iterate
          └── report result
          │
          ▼
     concise result → ARIA
```

## Independent configuration

The coder may use a different provider and model:

```yaml
coder:
  provider: ollama
  model: qwen2.5-coder:7b
  max_iterations: 60
  max_output_chars: 12000
```

Both `provider` and `model` are optional.

### Neither specified

The coder uses ARIA's active provider/model.

### Only provider specified

The coder uses that provider's first configured model (or the provider's active model where applicable).

### Both specified

The coder uses exactly the configured provider/model.

Hosted providers use `coder_api_key_env`, not ARIA's `aria_api_key_env`. This separation is important when the coder should have its own account, quota, or access policy.

## Iteration budget

`coder.max_iterations` controls how many model/tool rounds the independent agent can perform. The default example is 60, deliberately much larger than a normal conversational turn.

Increasing the budget does not make the model smarter; it gives the agent more opportunities to inspect, modify, test, and recover from intermediate failures. Very high budgets can increase API cost, latency, and unintended work.

## Output limits

`coder.max_output_chars` caps command output passed into the coder context. This prevents a huge compiler log or recursive directory listing from consuming the entire context window.

For debugging, prefer targeted commands or bounded searches rather than simply raising the limit without considering context size.

## Task delegation

ARIA should delegate tasks that have one or more of these characteristics:

- multi-file changes;
- repository-wide refactors;
- implementation followed by tests;
- substantial debugging;
- codebase exploration before editing;
- build/test cycles;
- repetitive mechanical edits.

Small conversational questions, explanations, or one-line changes do not necessarily need the independent agent.

## Workspace

The coder operates against the configured workspace. Treat that directory as trusted input to a coding agent. If the workspace contains credentials, generated secrets, production files, or unrelated private projects, consider using a narrower workspace.

## Context isolation

The coder has its own model context and ephemeral memory file. The primary ARIA conversation does not absorb every intermediate coder tool result.

This is the core architectural benefit: the user can ask ARIA to perform a large implementation task while keeping the main conversation focused on intent and outcome.

## Failure behavior

A coder deployment can fail because of provider initialization, model errors, invalid tool arguments, command failures, iteration exhaustion, or repository state. The deployment should report the failure back to ARIA rather than silently claiming completion.

The caller should treat a successful coder response as a report, not as proof that every desired behavioral requirement is satisfied. For important changes, inspect the resulting diff and run the project's tests/checks.

## Extending the coder

When changing the coder implementation:

1. Preserve the provider-neutral `AssistantMessage`/`ToolCall` contract.
2. Keep tool arguments validated at the registry/service boundary.
3. Bound shell output.
4. Keep credentials out of tool results and logs.
5. Preserve the independent iteration budget.
6. Add tests for success, validation failures, and provider/tool errors.
7. Update this document when the coder's capabilities or configuration contract changes.
