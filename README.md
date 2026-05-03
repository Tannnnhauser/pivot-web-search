# Pivot Web Search — Multi-Provider MCP Search Plugin for Claude Code

> A resilient, multi-provider web search and content extraction tool for Claude Code on Amazon Bedrock and other API providers.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Tests: 200](https://img.shields.io/badge/tests-200%20passing-brightgreen.svg)]()

## What Is This?

Claude Code users on **Amazon Bedrock** (or other API providers) don't get Anthropic's built-in `WebSearch` and `WebFetch` tools. This Model Context Protocol (MCP) search server fills that gap — giving you a fully self-hosted, multi-provider search failover engine and a local content extractor, with no Anthropic API dependency.

## Key Features

- **Multi-provider failover** — chains DuckDuckGo → Tavily → Brave → Gemini with automatic fallback. If one fails, the next takes over transparently.
- **Tuple-sort routing** — providers scored as `(tier_rank, metric, priority)` tuples. Free providers first, then daily-quota (Gemini), then paid with pacing pressure. No manual priority tuning needed.
- **Circuit breaker** — per-provider health tracking with sliding window. After 3 consecutive failures (or >60% failure rate), a provider is temporarily bypassed with automatic recovery probing.
- **Super mode** — queries all providers in parallel via native `asyncio.gather`, deduplicates by URL, and ranks by cross-provider agreement.
- **Fully async I/O** — all network calls use `httpx.AsyncClient` with connection pooling. No thread pools for HTTP — enables efficient concurrent requests and prepares for streamable-HTTP remote transport.
- **Local content extraction** — fetches and extracts full page content via trafilatura. No external API needed. Includes Next.js/Nuxt.js SPA fallback.
- **Quota-aware scheduling** — tracks API usage per provider, prefers cheapest available, skips exhausted providers.
- **Per-host proxy cache** — remembers which connection path worked for each hostname. Re-probes automatically on network changes.
- **Hot-reloadable config** — add/remove/reorder providers and proxies via YAML. Changes apply on the next request.
- **Pluggable adapters** — built-in support for DuckDuckGo (DDG), Tavily, Brave, Gemini, SearXNG, and a generic `json_api` adapter. Writing a new adapter is one class.
- **Structured error diagnostics** — when all providers fail, returns per-provider failure reasons and actionable suggestions instead of a generic error.
- **Debug logging** — set `PIVOT_WEB_SEARCH_DEBUG=1` to get timestamped verbose logs at `~/.cache/pivot-web-search/server.log`.

## Why Multi-Provider Failover?

Most MCP search tools bind to a single provider. If the API is down, rate-limited, or blocked in your network, you get nothing. Single-provider tools also can't compare results across sources or handle quota exhaustion gracefully.

This plugin solves these problems by combining multiple search backends with quality detection — if a provider returns fewer than 2 results, failover continues automatically. In super mode, all providers are queried in parallel for maximum coverage.

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
| **Tavily API Key** | Stored in system keychain. Leave empty to skip. |
| **Brave Search API Key** | Stored in system keychain. Leave empty to skip. |
| **Gemini API Key** | Stored in system keychain. Leave empty to skip. |
| **Proxy URLs** | Comma-separated, priority order. `direct` = no proxy (default: `direct`) |
| **Gemini daily quota** | Optional. Limits Gemini grounded searches per day (resets at PT midnight). Check your limit at [AI Studio](https://aistudio.google.com/rate-limit). |

DDG needs no API key. Providers without a key are automatically skipped during failover.

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

```yaml
providers:
  - name: ddg
    type: ddg
    tier: free
    enabled: true
    priority: 10          # lower = tried first

  - name: tavily
    type: tavily
    tier: paid
    enabled: true
    priority: 20
    api_key_env: TAVILY_API_KEY

  - name: brave
    type: brave
    tier: paid
    enabled: true
    priority: 30
    api_key_env: BRAVE_API_KEY

  - name: gemini
    type: gemini
    tier: daily
    enabled: true
    priority: 40
    api_key_env: GEMINI_SEARCH_API_KEY

  - name: searxng
    type: searxng
    enabled: false
    priority: 50
    endpoint: "http://localhost:8080/search"

  # You can define multiple json_api instances — each with a unique name,
  # independent priority, quota tracking, and circuit breaker.

  - name: custom-api
    type: json_api
    enabled: false
    priority: 60
    endpoint: "https://api.example.com/search"
    api_key_env: CUSTOM_API_KEY
    method: GET
    request_params:
      q: "{{query}}"
      num: "{{max_results}}"
    response_mapping:
      results_path: "data.results"
      title: "title"
      url: "link"
      snippet: "description"

  - name: serper
    type: json_api
    enabled: false
    priority: 65
    endpoint: "https://google.serper.dev/search"
    api_key_env: SERPER_API_KEY
    method: POST
    request_body:
      q: "{{query}}"
      num: "{{max_results}}"
    response_mapping:
      results_path: "organic"
      title: "title"
      url: "link"
      snippet: "snippet"

  # SearXNG, json_api, etc. — see config/providers.yaml for full examples
```

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
  ├─ failover mode: tuple-sort routing (tier_rank, metric, priority)
  │   free (DDG/SearXNG, metric=0) → daily (Gemini, metric=usage%) → paid (Tavily/Brave, metric=pacing pressure)
  │   Circuit breaker: unhealthy providers bypassed, auto-recovery after 120s cooldown
  │   Quality check: continues if results < 2, keeps best fallback
  │   High-water: Gemini demoted to paid tier at >85% daily usage (unless <4h to midnight)
  │   News queries: DDG deprioritized below paid providers
  │
  └─ super mode:    DDG ──┐
                    Tavily ┤ parallel (skip exhausted, ignore breaker) → dedup → rank by provider count
                    Brave ─┤
                    Gemini ┘
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

Quota-aware scheduling prefers providers with lower usage. Providers at 100% are skipped entirely. Paid providers are ranked by pacing pressure (usage_pct / elapsed_time_pct) so that over-budget providers are naturally deprioritized. Resets automatically on calendar month rollover.

## Architecture

```
hooks/hooks.json        PreToolUse hook — blocks built-in WebSearch/WebFetch (fail-open)
                        SessionStart hook — async health check on startup

pivot_web_search_mcp/         FastMCP server (stdio) — fully async, exposes 3 MCP tools
  server.py             Async tool handlers, failover orchestration, smart defaults
  routing.py            Tuple-sort routing, circuit breaker, pacing pressure
  search.py             Async search backends (httpx), URL extraction, proxy failover, dedup_and_rank
  providers.py          Async provider adapters, registry, config source tracking
  fetch.py              SPA detection, async JS renderer dispatch (Playwright/Tavily)
  quota.py              Cross-session quota tracking (filelock, cross-platform)
  logging.py            Centralized logging (stderr + optional file via PIVOT_WEB_SEARCH_DEBUG)

config/                 YAML config for providers, proxies, and fetch (hot-reloadable)
scripts/
  health-check.py       Startup probe — reports provider availability and quota
  pretool-check.py      PreToolUse hook script — fail-open tool blocker
tests/                  200 tests across 13 modules (pytest-asyncio)
```

## Testing

```sh
uv sync --extra dev                   # install dev dependencies
pytest -m "not integration"           # 193 offline tests (~4s)
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
DDG occasionally rate-limits aggressive queries. The circuit breaker automatically detects consecutive failures and temporarily bypasses DDG (120s cooldown with probe-based recovery). DDG is restored once a probe request succeeds. For reliability, configure at least one API-backed provider.

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
