# Web research

ARIA can provide web search and webpage retrieval through a self-hosted SearXNG backend. This avoids requiring a proprietary search API key and keeps the search service under the operator's control.

## Architecture

```text
ARIA web tool
   │
   ▼
WebToolService
   │
   ▼
SearXNGProvider
   │
   ▼
local SearXNG
   │
   ├── search engines
   └── result metadata
```

Opening a result is a separate webpage-fetch path. Search and page retrieval are independently bounded.

## Setup

Start the repository's bundled deployment:

```bash
docker compose -f infrastructure/searxng/docker-compose.yml up -d
```

The example configuration uses:

```yaml
web:
  enabled: true
  start_backend: false
  searxng_url: http://127.0.0.1:8080
```

ARIA never starts Docker unless `start_backend: true` is explicitly configured.

## Search limits

The web configuration contains multiple independent bounds:

- `search_timeout` — maximum time for a search request.
- `page_timeout` — maximum time for a webpage fetch.
- `max_results` — maximum search results returned.
- `max_search_calls` — maximum search tool calls for the configured execution boundary.
- `max_page_fetches` — maximum pages opened.
- `max_total_web_calls` — total search/open budget.
- `max_page_chars` — maximum extracted page text.
- `max_snippet_chars` — maximum result snippet length.
- `max_concurrent_page_fetches` — concurrency limit.
- `max_retries` — retry count.
- cache TTLs — reduce repeated network work.

These bounds are important because a model can otherwise turn a simple research request into an unbounded crawl.

## Search vs page retrieval

Search is used to discover sources. `open_webpage` is used when the model needs the content of a specific result.

The tool should prefer targeted retrieval over opening many pages indiscriminately. The page extraction path uses HTML extraction facilities so the model receives useful text instead of an entire browser-like DOM where possible.

## Caching

Search and page results have separate cache TTLs:

```yaml
search_cache_ttl: 300
page_cache_ttl: 1800
```

Caching improves responsiveness and avoids unnecessary repeat requests. Lower TTLs are useful for rapidly changing information; higher TTLs are useful for stable reference material.

## SSRF and destination validation

Webpage retrieval is a security boundary. Private/local destinations are blocked by default, including loopback, link-local, and common RFC1918-style private networks.

`allowed_hosts` can explicitly permit hosts when an operator intentionally needs an internal destination:

```yaml
allowed_hosts: []
```

Do not populate this list casually. An allowed internal host becomes reachable through a model-controlled fetch path.

## Optional user agent

The configured user agent defaults to an ARIA identifier. Keep the user agent honest and avoid pretending to be another browser or service.

## Failure behavior

SearXNG is optional. If it is unavailable at startup, ARIA should continue launching and emit a warning. Calls to the web tools then return an actionable backend error instead of taking down the entire assistant.

This means web search can be deployed after the core assistant is working.

## Research workflow

A good research request typically follows:

1. Search for the topic.
2. Inspect several relevant results rather than trusting one snippet.
3. Open the strongest primary/authoritative sources.
4. Compare conflicting claims.
5. Keep the number of searches/pages within the configured budget.
6. Return conclusions with source references/citations where the surrounding UI supports them.

ARIA's web tools provide retrieval capabilities; they do not guarantee that every search result is authoritative. The model remains responsible for evaluating evidence.

## Debugging

Check SearXNG directly first:

```bash
docker compose -f infrastructure/searxng/docker-compose.yml ps
```

Then inspect ARIA's single runtime log:

```text
data/logs/aria.log
```

Do not create or maintain parallel ad-hoc log directories for web debugging.
