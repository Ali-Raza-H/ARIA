# Environment and secrets

ARIA separates configuration values from secrets so the repository can contain a complete `config.example.yaml` without containing credentials.

## Files

### `config.yaml`

Local static configuration. Copy it from `config.example.yaml` and customize it for the machine.

Do not put API-key values directly into this file.

### `.env`

Environment variables loaded by ARIA. This is the preferred location for hosted API keys and supported environment overrides.

Start from:

```bash
cp .env.example .env
```

Keep the file private and never commit real credentials.

### Runtime state

```text
data/state/aria-state.yaml
```

This ignored file contains selected runtime preferences saved by the TUI. It is not a replacement for `.env` and is not intended to contain secrets.

## Provider credentials

Provider blocks identify the environment variable containing ARIA's credential and the environment variable containing the coder's credential:

```yaml
providers:
  openrouter:
    aria_api_key_env: ARIA_OPENROUTER_API_KEY
    coder_api_key_env: CODER_OPENROUTER_API_KEY
```

Then:

```dotenv
ARIA_OPENROUTER_API_KEY=...
CODER_OPENROUTER_API_KEY=...
```

This allows ARIA and its independent coding agent to use different keys.

## Supported runtime overrides

The configuration reference documents the supported `ARIA_*` environment overrides. Examples include:

```dotenv
ARIA_PROVIDER=ollama
ARIA_MODEL=gemma2:9b
ARIA_CODER_MAX_ITERATIONS=100
ARIA_CODER_MAX_OUTPUT_CHARS=20000
ARIA_SPEECH_ENABLED=true
ARIA_SPEECH_ENGINE=kokoro_local
ARIA_WORKSPACE=/home/me/project
ARIA_COMMAND_TIMEOUT_SECONDS=30
ARIA_MAX_COMMAND_OUTPUT_CHARS=12000
ARIA_LOG_DIR=data/logs
```

Only supported/whitelisted settings should be treated as runtime overrides. Do not assume an arbitrary YAML key becomes an environment variable automatically.

## Gmail credentials

Gmail can use the official MCP route and/or the direct REST fallback. The exact environment variable names are configured by:

```yaml
gmail:
  access_token_env: GMAIL_MCP_ACCESS_TOKEN
  direct_api_access_token_env: GMAIL_API_ACCESS_TOKEN
```

OAuth client/token files can also be configured under `gmail.oauth_client_secrets_path` and `gmail.oauth_token_path`.

Treat these files as credentials. Restrict their filesystem permissions and never commit them.

## Hosted speech

Hosted Kokoro uses the environment variable named by `speech.hf_api_key_env` (the example configuration uses a Hugging Face token variable).

Keep the token in `.env`, not in YAML or a skill.

## Remote embeddings

If an OpenAI-compatible embedding endpoint is configured, its URL/model/key environment variable are defined under `memory`. The memory subsystem redacts likely secrets and machine-specific paths before sending text to a remote embedding backend.

Even with redaction, treat remote embedding as a data-sharing decision: configure it only when the operator accepts sending the selected memory text to that endpoint.

## Secret handling rules

Never:

- commit `.env`;
- paste API keys into `config.yaml`;
- put credentials in skills/workflows;
- include OAuth tokens in bug reports;
- print full environment variables to logs;
- paste `data/logs/aria.log` publicly without checking it for sensitive content;
- share browser profiles containing authenticated sessions.

If a credential is accidentally exposed, rotate/revoke it at the provider rather than relying on deleting the Git commit alone.

## Configuration precedence

ARIA's effective configuration follows:

```text
built-in defaults
      ↓
config.yaml
      ↓
whitelisted runtime state
      ↓
ARIA_* environment overrides
```

Credential environment variables are referenced by provider/tool configuration rather than becoming arbitrary configuration keys.

## Debugging environment configuration

To debug a missing credential, verify only whether the variable is present rather than printing its value. For example, in a shell you can use a presence test instead of `echo $SECRET`.

When ARIA reports a provider initialization error, check the provider block's `aria_api_key_env`/`coder_api_key_env` name against `.env` exactly, including capitalization.
