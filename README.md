# Pivot Web Search — Multi-Provider MCP Search Plugin for Claude Code

> A resilient, multi-provider web search and content extraction tool for Claude Code on Amazon Bedrock and other API providers.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Tests: 265](https://img.shields.io/badge/tests-265%20passing-brightgreen.svg)]()

## What Is This?

Claude Code users on **Amazon Bedrock** (or other API providers) don't get Anthropic's built-in `WebSearch` and `WebFetch` tools. This Model Context Protocol (MCP) search server fills that gap — giving you a fully self-hosted, multi-provider search failover engine and a local content extractor, with no Anthropic API dependency.

## Key Features

- **Priority-group routing** — providers grouped by priority and executed with hedged requests. Same-priority providers fire concurrently with 200ms stagger; first quality-gate pass wins. Groups tried sequentially from highest to lowest priority.
- **Smart defaults** — quality-first ordering (LLM Search > Tavily/Brave > SearXNG > Gemini > DDG) applied automatically. No manual priority tuning needed for common setups.
- **3-tier quality gate** — AI answer presence, URL count, and keyword overlap drive automatic failover decisions. Partial results are kept as fallback while better sources are tried.
- **Circuit breaker** — per-provider health tracking. After 3 consecutive failures, a provider is temporarily bypassed (60s cooldown) with automatic recovery probing.
- **Super mode** — queries all providers in parallel, deduplicates by URL, and ranks by cross-provider agreement.
- **Fully async I/O** — all network calls use `httpx.AsyncClient` with connection pooling. No thread pools for HTTP — enables efficient concurrent requests and prepares for streamable-HTTP remote transport.
- **Local content extraction** — fetches and extracts full page content via trafilatura. No external API needed. Includes Next.js/Nuxt.js SPA fallback.
- **Quota-aware scheduling** — tracks API usage per provider, prefers cheapest available, skips exhausted providers.
- **Per-host proxy cache** — remembers which connection path worked for each hostname. Re-probes automatically on network changes.
- **Hot-reloadable config** — add/remove/reorder providers and proxies via YAML. Changes apply on the next request.
- **Pluggable adapters** — built-in support for DuckDuckGo (DDG), Tavily, Brave, Gemini, SearXNG, a generic `json_api` adapter, and `llm_search` for any LLM with web search grounding. Writing a new adapter is one class.
- **Structured error diagnostics** — when all providers fail, returns per-provider failure reasons and actionable suggestions instead of a generic error.
- **Debug logging** — set `PIVOT_WEB_SEARCH_DEBUG=1` to get timestamped verbose logs at `~/.cache/pivot-web-search/server.log`.

## Why Multi-Provider Failover?

Most MCP search tools bind to a single provider. If the API is down, rate-limited, or blocked in your network, you get nothing. Single-provider tools also can't compare results across sources or handle quota exhaustion gracefully.

This plugin solves these problems by combining multiple search backends with a quality gate — if results don't pass (too few URLs, no keyword overlap), failover continues automatically to the next priority group. In super mode, all providers are queried in parallel for maximum coverage.

## Prerequisites

- **Claude Code** installed and configured
- **[uv](https://docs.astral.sh/uv/)** — the plugin launcher (manages Python and dependencies automatically)
- At least one search provider configured (DDG works with no API key)

> **Recommended:** Configure at least one free API key — [Tavily](https://tavily.com) (1000 credits/month, no credit card) or [Brave](https://brave.com/search/api/) (1000 queries/month, credit card required). DDG is a free fallback but can be unreliable under heavy use.

## Plugin Installation

**Step 1:** Add the marketplace (one-time):

```sh
claude plugin marketplace add https://github.com/Tannnnhauser/pivot-web-search.git
```

**Step 2:** Install:

```sh
claude plugin install pivot-web-search
```

The plugin prompts for configuration at install time:

| Setting | Description |
|---|---|
| **Search providers** | Comma-separated list in failover order (default: `ddg,tavily,brave,gemini`) |
| **Tavily API Key** | Stored in system keychain. Leave empty to inherit `TAVILY_API_KEY` from the shell. |
| **Brave Search API Key** | Stored in system keychain. Leave empty to inherit `BRAVE_API_KEY` from the shell. |
| **Gemini API Key** | Stored in system keychain. Leave empty to inherit `GEMINI_SEARCH_API_KEY` (or `GOOGLE_STUDIO_API_KEY`) from the shell. |
| **Proxy URLs** | Comma-separated, priority order. `direct` = no proxy (default: `direct`) |
| **Gemini daily quota** | Optional. Limits Gemini grounded searches per day (resets at PT midnight). Check your limit at [AI Studio](https://aistudio.google.com/rate-limit). |

DDG needs no API key. Providers without a key are automatically skipped during failover.

**Key resolution order** — for each provider key, the plugin reads:
1. The standard env var (e.g. `TAVILY_API_KEY`) inherited from the parent shell. **Wins if set.**
2. The `/plugin` UI value (injected as `PIVOT_USERCONFIG_TAVILY_API_KEY`).

This means a value exported in your shell always takes precedence over the UI config. To use the UI value instead, `unset TAVILY_API_KEY` in your shell.

> **macOS GUI launch caveat:** When Claude Code is started from Spotlight or the Dock, it does **not** see your `~/.zshrc` exports. Either start it from a terminal, set keys via the `/plugin` UI, or add them to the `env` block in `~/.claude/config.json`.

You can reconfigure anytime via `claude plugin configure pivot-web-search`.

### Verify installation

After installing, ask Claude Code to run `WebSearchConfig` with action `status`. You should see a provider health report showing which providers are online and their quota usage.

### Manual install

Requires **[uv](https://docs.astral.sh/uv/)** (or Python 3.10+ with pip).

```sh
git clone https://github.com/Tannnnhauser/pivot-web-search.git
cd pivot-web-search
uv sync           # installs all deps in a managed venv
# or: pip install -e ".[dev]"
```

When running manually, set API keys as environment variables:

```sh
export TAVILY_API_KEY=tvly-...
export BRAVE_API_KEY=BSA...
export GEMINI_SEARCH_API_KEY=AI...   # or GOOGLE_STUDIO_API_KEY
```

After manual installation, configure providers and proxies by editing the YAML files in the `config/` directory. To register the server with Claude Code, add it to your `.mcp.json` or run:

```sh
claude mcp add pivot-web-search "uv run --directory /path/to/pivot-web-search python -m pivot_web_search_mcp"
```

### Uninstall

```sh
claude plugin uninstall pivot-web-search
```

### Upgrade

```sh
claude plugin update pivot-web-search
# Or for manual installs:
git pull && uv sync
# or: pip install -e ".[dev]"
```

### Local development

```sh
claude --plugin-dir /path/to/pivot-web-search/
```

## MCP Tool Reference

When called via MCP, tools are prefixed: `mcp__pivot-web-search__WebSearch`, `mcp__pivot-web-search__WebFetch`, `mcp__pivot-web-search__WebSearchConfig`.

### `WebSearch`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | str | *required* | Search query |
| `provider` | str | `"auto"` | Force provider: `auto` / `ddg` / `tavily` / `brave` / `gemini` |
| `super_mode` | bool | `false` | Query all providers in parallel for maximum coverage |
| `max_results` | int | `5` | Number of results: 1–10 (1–20 in super mode) |
| `allowed_domains` | list[str] | `null` | Only include results from these domains |
| `blocked_domains` | list[str] | `null` | Exclude results from these domains |
| `news` | bool | `false` | Search news instead of web |
| `timelimit` | str | `null` | Time filter: `d` = day, `w` = week, `m` = month, `y` = year |
| `include_answer` | bool | `false` | AI-generated answer summary (Tavily only) |
| `include_content` | bool | `false` | Return pre-extracted page content with results (Brave LLM Context) |
| `search_depth` | str | `"basic"` | `basic` or `advanced` — advanced gives more detail but costs 2x credits (Tavily only) |
| `topic` | str | `"general"` | `general` or `news` (Tavily only) |
| `days` | int | `null` | Limit news to recent N days (Tavily only) |

### `WebFetch`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | str / list[str] | *required* | URL(s) to extract content from. HTTP auto-upgrades to HTTPS. Supports batch mode with multiple URLs. |
| `prompt` | str | *required* | Instruction passed alongside the extracted content. The calling AI model uses this to focus its response — no server-side filtering is performed. |
| `query` | str | `null` | Optional query for relevance-aware extraction |
| `max_chars` | int | `null` | Truncate output to this many characters (default: 100,000) |

**Behaviors:**
- 15-minute response cache per URL
- 100K character truncation (configurable via `max_chars`)
- Binary content detection and rejection
- Cross-host redirect safety (blocks before following)
- SPA fallback: Next.js `__NEXT_DATA__` / RSC payload / Nuxt `__NUXT_DATA__`

### `WebSearchConfig`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `action` | str | `"status"` | `status` — inspect provider health, quota, and config sources; `reload` — hot-reload YAML config |

The `status` action returns:
- Provider health (available/unavailable with details)
- Quota usage per provider
- **Config source annotations** — shows where each setting comes from: environment variable (with var name), YAML file (with path), or built-in default

## Configuration

Configuration priority (highest to lowest): **install-time config > YAML files > built-in defaults**.

Install-time settings (via `userConfig`) are injected as environment variables and take priority. YAML files serve as a power-user escape hatch for advanced setups (SearXNG, custom JSON APIs, SOCKS5 proxies). For simple changes, use `claude plugin configure pivot-web-search`. For advanced setups, edit the YAML files directly.

### `config/providers.yaml`

Providers are tried by priority (lower number = tried first). Same-priority providers are hedged — queried concurrently with staggered starts, first quality result wins. If no priority is set, smart defaults apply based on provider type.

```yaml
providers:
  - name: tavily
    type: tavily
    api_key_env: TAVILY_API_KEY
    # priority: 20 (smart default — hedged with brave)
    # timeout: 4 (smart default)

  - name: brave
    type: brave
    api_key_env: BRAVE_API_KEY
    # priority: 20 (smart default — hedged with tavily)
    # timeout: 4 (smart default)

  - name: gemini
    type: gemini
    api_key_env: GEMINI_SEARCH_API_KEY
    model: gemini-2.5-flash
    # priority: 40 (smart default)
    # timeout: 20 (smart default)

  - name: ddg
    type: ddg
    # priority: 90 (smart default — exhaustion fallback)
    # timeout: 6 (smart default)

  # Self-hosted SearXNG
  # - name: searxng-local
  #   type: searxng
  #   endpoint: "http://localhost:8888/search"

  # Generic JSON API (Serper, Google CSE, etc.)
  # Multiple json_api instances allowed — each gets independent
  # priority, quota tracking, and circuit breaker state.
  # - name: serper
  #   type: json_api
  #   endpoint: "https://google.serper.dev/search"
  #   api_key_env: SERPER_API_KEY
  #   method: POST
  #   request_body:
  #     q: "{{query}}"
  #     num: "{{max_results}}"
  #   response_mapping:
  #     results_path: "organic"
  #     title: "title"
  #     url: "link"
  #     snippet: "snippet"

circuit_breaker:
  consecutive_threshold: 3
  cooldown: 60

latency:
  hedge_delay_ms: 200
```

**Smart default priorities** (when no explicit `priority` is set):

| Type | Priority | Timeout |
|---|---|---|
| `llm_search` | 10 | 15s |
| `tavily` / `brave` | 20 | 4s |
| `searxng` / `json_api` | 30 | 6s |
| `gemini` | 40 | 20s |
| `ddg` | 90 | 6s |

### LLM Search Providers (`type: llm_search`)

For LLM-based search — any model with built-in web search grounding (Perplexity Sonar Pro, OpenAI with web_search, SAP AI Core, etc.). These providers return an AI-generated answer plus cited URLs extracted from the response.

This is a power-user feature. Configure by editing `config/providers.yaml` directly.

Two `api_format` paradigms are supported:

**`chat_completions`** — any `/chat/completions`-compatible endpoint with built-in search:

```yaml
  - name: sonar-pro
    type: llm_search
    api_format: chat_completions
    endpoint: "https://api.perplexity.ai/chat/completions"
    model: sonar-pro
    api_key_env: PERPLEXITY_API_KEY
    timeout: 15
    # priority: 10 (smart default)
```

Response parsing uses a data-driven fallback chain:
1. `search_results` array (Perplexity/Sonar style)
2. `annotations` with `type: url_citation` in message (OpenAI Chat Completions style)
3. Top-level `citations` URL array

**`responses`** — OpenAI Responses API (`/responses`) with web_search tool:

```yaml
  - name: gpt-web-search
    type: llm_search
    api_format: responses
    endpoint: "https://api.openai.com/v1/responses"
    model: gpt-4o
    api_key_env: OPENAI_API_KEY
    timeout: 45
    search_tool: web_search
    search_context_size: medium
```

Additional `responses` format options: `filters` (domain filtering object), `user_location` (location context).

**Common fields:**

| Field | Required | Description |
|---|---|---|
| `api_format` | No | `chat_completions` (default) or `responses` |
| `endpoint` | Yes | Full URL to the API endpoint |
| `model` | Yes | Model identifier |
| `api_key_env` | Yes | Environment variable holding the API key (sent as Bearer token) |
| `max_tokens` | No | Max response tokens (default: 500 for chat_completions, 4000 for responses) |
| `timeout` | No | Request timeout in seconds (default: 30) |
| `system_prompt` | No | System prompt (chat_completions only) |
| `headers` | No | Additional request headers |
| `web_search_options` | No | Extra search options object (chat_completions only) |

The existing `gemini` type is also backed by LLM search internally (using Google's Search grounding) but keeps its own `type: gemini` for backward compatibility and dual-key fallback logic.

### `config/proxies.yaml`

```yaml
proxies:
  - name: direct
    url: null             # direct connection
    enabled: true
    priority: 1

  - name: myproxy1
    url: "http://myproxy1.example:8080"
    enabled: true
    priority: 2
```

### `config/fetch.yaml`

Controls WebFetch behavior including JavaScript rendering fallback:

```yaml
js_renderer: none         # none (default), "playwright", or "tavily"
max_chars: 100000         # content truncation limit
```

Set `js_renderer: playwright` for JavaScript-heavy sites (requires `uv sync --extra browser` or `pip install pivot-web-search-mcp[browser]`).

## How Routing and Failover Works

```
Request
  │
  ├─ normal mode: priority-group routing
  │   ┌─ Group 1 (priority 10): LLM Search (Perplexity, OpenAI, etc.)
  │   ├─ Group 2 (priority 20): Tavily + Brave ← hedged (200ms stagger, first quality-gate pass wins)
  │   ├─ Group 3 (priority 30): SearXNG / json_api
  │   ├─ Group 4 (priority 40): Gemini
  │   └─ Group 5 (priority 90): DDG (free exhaustion fallback)
  │
  │   Gates: quota-exhausted → skip | circuit-open → skip | affinity mismatch → skip
  │   Quality gate (3-tier): AI answer ≥40 chars? → unique URLs ≥2? → keyword overlap?
  │   Circuit breaker: 3 consecutive failures → OPEN (60s cooldown) → HALF_OPEN → probe
  │
  └─ super mode:    Tavily ┐
                    Brave  ┤ parallel (skip exhausted) → dedup → rank by provider count
                    Gemini ┤
                    DDG    ┘
```

Each provider independently tries all configured proxies (direct → myproxy1 → myproxy2) with per-host caching. The proxy cache persists to `~/.cache/pivot-web-search-proxy-cache.json` across sessions.

### Quota Management

API usage is tracked across sessions in `~/.cache/pivot-web-search/quota.json`:

| Provider | Tracking | Free tier | Details |
|---|---|---|---|
| **DuckDuckGo** | Not tracked | Unlimited | Free, no API key needed |
| **Tavily** | API sync | 1000 credits/month | Calls `GET /usage` at startup for real credit data |
| **Brave** | Response headers | Rolling 30-day window | Parses `X-RateLimit-Remaining` / `X-RateLimit-Reset` headers |
| **Gemini** | Local counting (daily) | Varies by account (resets at PT midnight) | Set limit via `PIVOT_WEB_SEARCH_GEMINI_QUOTA` env var |

Quota-aware scheduling skips providers at 100% usage. Providers are ordered by priority groups (smart defaults or explicit config). Resets automatically on calendar month rollover.

## Architecture

```
hooks/hooks.json        PreToolUse hook — blocks built-in WebSearch/WebFetch (fail-open)
                        SessionStart hook — async health check on startup

pivot_web_search_mcp/         FastMCP server (stdio) — fully async, exposes 3 MCP tools
  server.py             Async tool handlers, failover orchestration, smart defaults
  routing.py            Priority-group routing, hedged execution, circuit breaker, quality gate
  quality_gate.py       3-tier quality gate (answer/URLs/keywords)
  search.py             Async search backends (httpx), URL extraction, proxy failover, dedup_and_rank
  providers.py          Async provider adapters, registry, smart defaults, config source tracking
  llm_search_formats.py Strategy pattern for LLM search API formats (chat_completions, responses, gemini)
  fetch.py              SPA detection, async JS renderer dispatch (Playwright/Tavily)
  quota.py              Cross-session quota tracking (filelock, cross-platform)
  logging.py            Centralized logging (stderr + optional file via PIVOT_WEB_SEARCH_DEBUG)

config/                 YAML config for providers, proxies, and fetch (hot-reloadable)
scripts/
  health-check.py       Startup probe — reports provider availability and quota
  pretool-check.py      PreToolUse hook script — fail-open tool blocker
tests/                  265 tests across 15 modules (pytest-asyncio)
```

## Testing

```sh
uv sync --extra dev                   # install dev dependencies
pytest -m "not integration"           # 265 offline tests (~5s)
pytest                                # all tests including live API integration (requires API keys)
```

## Troubleshooting

**Enable debug logging**
Set `PIVOT_WEB_SEARCH_DEBUG=1` as an environment variable. Verbose timestamped logs are written to `~/.cache/pivot-web-search/server.log`. Useful for diagnosing provider failures and proxy routing.

**No results from any provider**
Run `WebSearchConfig` with action `status` to check provider health and see which config source is active for each setting. Ensure at least one provider has a valid API key configured (or that DDG is reachable from your network).

**SSL certificate errors on macOS**
The plugin uses `certifi` for CA bundles. If you still see SSL errors:
`uv sync --upgrade-package certifi`

**Proxy timeouts / slow startup**
The SessionStart health check runs asynchronously and never blocks your session. If you're not behind a proxy, ensure only `direct` is enabled in `config/proxies.yaml`
or set Proxy URLs to `direct` via `claude plugin configure pivot-web-search`.

**DuckDuckGo rate limiting (403 errors)**
DDG occasionally rate-limits aggressive queries. The circuit breaker automatically detects consecutive failures and temporarily bypasses DDG (60s cooldown with probe-based recovery). DDG is restored once a probe request succeeds. For reliability, configure at least one API-backed provider.

**`trafilatura` extraction returns empty**
Some JavaScript-heavy sites need a renderer. Set `js_renderer: playwright`
in `config/fetch.yaml` and install: `uv sync --extra browser` (or `pip install pivot-web-search-mcp[browser]`)
then `playwright install chromium`.

**Playwright not installed error**
Playwright is optional. Install only when needed:
`uv sync --extra browser && playwright install chromium`
(or: `pip install pivot-web-search-mcp[browser] && playwright install chromium`)

## Alternatives Comparison

| Feature | Pivot Web Search (this) | Single-provider MCP tools | Built-in WebSearch |
|---|---|---|---|
| Multi-provider failover | 4 providers with auto-fallback | Single point of failure | N/A on Bedrock |
| Quota management | Cross-session tracking | None | N/A |
| Super mode (parallel) | All providers at once | Not possible | N/A |
| Local content extraction | trafilatura + SPA fallback | Usually Tavily Extract | Anthropic-hosted |
| Proxy support | Configurable chain with cache | Usually none | N/A |
| Works on Bedrock | Yes | Yes | No |
| Self-hosted | Yes | Varies | No |

## License

Apache-2.0
