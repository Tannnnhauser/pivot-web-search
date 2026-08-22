# Pivot Web Search — Multi-Provider MCP Search Plugin for Claude Code

> A resilient, multi-provider web search and content extraction tool for Claude Code on Amazon Bedrock and other API providers.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Tests: 355](https://img.shields.io/badge/tests-355%20offline-brightgreen.svg)]()

## What Is This?

Pivot Web Search is first and foremost a **Claude Code Plugin**. It gives Claude Code users on **Amazon Bedrock** (or other API providers) a fully self-hosted, multi-provider search failover engine and local content extraction without an Anthropic API dependency. MCP is the Plugin's runtime interface, not a replacement for the Plugin product boundary.

## Key Features

- **Priority-group routing** — providers grouped by priority and executed with hedged requests. Same-priority providers fire concurrently with 200ms stagger; first quality-gate pass wins. Groups tried sequentially from highest to lowest priority.
- **Smart defaults** — quality-first ordering (LLM Search > Tavily/Brave/Gemini > SearXNG/json_api > DDG) applied automatically. No manual priority tuning needed for common setups.
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
| **Tavily API Key** | Stored in system keychain. Set to enable Tavily, leave empty to skip (or inherit `TAVILY_API_KEY` from the shell). |
| **Brave Search API Key** | Stored in system keychain. Set to enable Brave, leave empty to skip (or inherit `BRAVE_API_KEY` from the shell). |
| **Gemini API Key** | Stored in system keychain. Set to enable Gemini, leave empty to skip (or inherit `GEMINI_SEARCH_API_KEY` / `GOOGLE_STUDIO_API_KEY` from the shell). |
| **Proxy URLs** | Comma-separated proxy URLs to try in order. `direct` is **always appended as the final fallback** — to disable that, use `~/.pivot-web-search/proxies.yaml`. |

Providers are enabled automatically based on which keys you supply: each provider with a key (or its corresponding env var) is added to the routing chain. DDG is always enabled (no key needed). Routing order and timeouts are determined by [smart defaults](#configuration); the order you supply keys does not matter.

For advanced setups (SearXNG, custom JSON APIs, LLM-search providers, SOCKS5 proxies, forced-proxy with no direct fallback), see [`~/.pivot-web-search/`](#configuration).

**Key resolution order** — for each provider key, the plugin reads:
1. The standard env var (e.g. `TAVILY_API_KEY`) inherited from the parent shell. **Wins if set.**
2. The `/plugin` UI value (injected as `PIVOT_USERCONFIG_TAVILY_API_KEY`).

This means a value exported in your shell always takes precedence over the UI config. To use the UI value instead, `unset TAVILY_API_KEY` in your shell.

> **macOS GUI launch caveat:** When Claude Code is started from Spotlight or the Dock, it does **not** see your `~/.zshrc` exports. Either start it from a terminal, set keys via the `/plugin` UI, or add them to the `env` block in `~/.claude/config.json`.

You can reconfigure anytime via `claude plugin configure pivot-web-search`.

### Verify installation

After installing, ask Claude Code to run `WebSearchConfig` with action `status`. You should see a provider health report showing which providers are online and their quota usage.

### Manual install

For users who want to skip the plugin marketplace entirely, run the MCP server remotely via `uvx`. Requires only **[uv](https://docs.astral.sh/uv/)** — no clone, no venv, no `pip install`. `uvx` fetches the package directly from GitHub and runs it in a managed cache.

Add this to your `.mcp.json` (works in Claude Code, Claude Desktop, Cursor, and any MCP-aware host):

```json
{
  "mcpServers": {
    "pivot-web-search": {
      "command": "uvx",
      "args": [
        "git+https://github.com/Tannnnhauser/pivot-web-search.git#subdirectory=plugins/pivot-web-search"
      ],
      "env": {
        "TAVILY_API_KEY": "tvly-...",
        "BRAVE_API_KEY": "BSA...",
        "GEMINI_SEARCH_API_KEY": "AI..."
      }
    }
  }
}
```

Or one-shot from the shell (e.g. for debugging):

```sh
uvx "git+https://github.com/Tannnnhauser/pivot-web-search.git#subdirectory=plugins/pivot-web-search"
```

First run takes a few seconds (clone + dependency resolve); subsequent runs reuse the uvx cache. Pin a specific version with `@v1.0.2`:

```
git+https://github.com/Tannnnhauser/pivot-web-search.git@v1.0.2#subdirectory=plugins/pivot-web-search
```

For advanced provider/proxy config, drop YAML files at `~/.pivot-web-search/providers.yaml` and `~/.pivot-web-search/proxies.yaml` (templates in [`examples/`](examples/)) — they apply to `uvx` launches the same way they apply to plugin installs.

### Uninstall

```sh
claude plugin uninstall pivot-web-search
```

### Upgrade

```sh
claude plugin update pivot-web-search
# For uvx-based installs: refresh the cache, or pin a new @vX.Y.Z tag
uvx --refresh "git+https://github.com/Tannnnhauser/pivot-web-search.git#subdirectory=plugins/pivot-web-search"
```

### Local development

```sh
claude --plugin-dir /path/to/pivot-web-search/
```

## CLI and Other Hosts

The installed Python package also provides a human-facing CLI backed by the
same search, fetch, and configuration services as the Plugin's MCP tools:

```sh
pivot-web-search search "latest Python release" --format json
pivot-web-search fetch https://example.com --format md
pivot-web-search config status
```

`extract` remains an alias for `fetch`. The CLI does not duplicate provider
routing or content-extraction logic.

Hosts that support MCP can run `pivot-web-search-mcp` directly. A host-specific
native integration must be an installable adapter using that host's public API;
it must never require patching the host's source tree.

### DeepSeek Harness

The optional [`pivot-web-search-dsh`](integrations/deepseek-harness/) Profile
Bundle is maintained in this repository but is not part of the Claude Plugin
payload. It registers Pivot as DSH's existing `web_search` and `web_fetch`
provider through DSH's published plugin interfaces:

```sh
uv tool install 'git+https://github.com/Tannnnhauser/pivot-web-search.git#subdirectory=plugins/pivot-web-search'
dsh plugin --profile web add ./integrations/deepseek-harness
```

Once published, the second command becomes
`dsh plugin --profile web add pivot-web-search-dsh`. No DSH source checkout,
fork, or pull request is required. The adapter invokes the host-neutral
`pivot-web-search-bridge` command and maps structured results into DSH's
standard web contracts.

## MCP Tool Reference

When called via MCP, tools are prefixed: `mcp__pivot-web-search__WebSearch`, `mcp__pivot-web-search__WebFetch`, `mcp__pivot-web-search__WebSearchConfig`.

### `WebSearch`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | str | *required* | Search query |
| `provider` | str | `"auto"` | Force provider: `auto` / `ddg` / `tavily` / `brave` / `gemini` / `searxng`, or any registered provider name (e.g. custom `json_api` or `llm_search` instances) |
| `super_mode` | bool | `false` | Query all providers in parallel for maximum coverage |
| `max_results` | int | `5` | Number of results: 1–10 (1–20 in super mode) |
| `allowed_domains` | list[str] | `null` | Only include results from these domains |
| `blocked_domains` | list[str] | `null` | Exclude results from these domains |
| `news` | bool | `false` | Search news instead of web |
| `timelimit` | str | `null` | Time filter: `d` = day, `w` = week, `m` = month, `y` = year |
| `include_answer` | bool | `false` | AI-generated answer summary (Tavily only) |
| `include_content` | bool | `false` | Return pre-extracted page content with results (Brave LLM Context) |
| `max_content_tokens` | int | `8192` | Token budget for content extraction when `include_content=true` (1024–32768) |
| `search_depth` | str | `"basic"` | `basic` or `advanced` — advanced gives more detail but costs 2x credits (Tavily only) |
| `topic` | str | `"general"` | `general` or `news` (Tavily only) |
| `days` | int | `null` | Limit news to recent N days (Tavily only) |

### `WebFetch`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | str / list[str] | *required* | URL(s) to extract content from. HTTP auto-upgrades to HTTPS. Supports batch mode with multiple URLs. |
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

The plugin runs on **auto-detection** by default — supply API keys via the install-time UI (or shell env vars), and the matching providers are enabled automatically with smart routing defaults. No YAML required for the common case.

For advanced setups, drop YAML files into `~/.pivot-web-search/`:

| File | Purpose |
|---|---|
| `~/.pivot-web-search/providers.yaml` | Take over provider config: SearXNG, custom JSON APIs, LLM-search providers, explicit priorities |
| `~/.pivot-web-search/proxies.yaml` | Take over proxy config: SOCKS5, forced-proxy (no direct fallback), per-proxy priority |

**Precedence is "all-or-nothing per file":** if `~/.pivot-web-search/providers.yaml` exists, auto-detection is bypassed entirely — list every provider you want enabled (including DDG). Same for proxies. Templates live in [`examples/`](examples/) — copy what you need.

### `~/.pivot-web-search/providers.yaml`

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
    # priority: 20 (smart default — hedged with tavily/brave)
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
```

**Smart default priorities** (when no explicit `priority` is set):

| Type | Priority | Timeout |
|---|---|---|
| `llm_search` | 10 | 15s |
| `tavily` / `brave` / `gemini` | 20 | 4s / 4s / 20s |
| `searxng` / `json_api` | 30 | 6s |
| `ddg` | 90 | 6s |

> **Note:** Enabling an `llm_search` provider trades latency for quality — at priority 10 it runs ahead of Tavily/Brave with a 15s timeout, so every query may take 15+s. Bump its `priority` above 20 (or omit it from the config) if latency matters more than answer quality.

### LLM Search Providers (`type: llm_search`)

For LLM-based search — any model with built-in web search grounding (Perplexity Sonar Pro, OpenAI with web_search, SAP AI Core, etc.). These providers return an AI-generated answer plus cited URLs extracted from the response.

This is a power-user feature. Configure by creating `~/.pivot-web-search/providers.yaml` (template at [`examples/providers.yaml`](examples/providers.yaml)).

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

### `~/.pivot-web-search/proxies.yaml`

When this file exists it takes over completely — the install-time `Proxy URLs` field is ignored, and `direct` is **not** auto-appended (escape hatch for forced-proxy / no-direct-fallback setups).

```yaml
proxies:
  - name: direct
    url: null             # null = direct connection
    enabled: true
    priority: 1

  - name: myproxy1
    url: "http://myproxy1.example:8080"
    enabled: true
    priority: 2

  # SOCKS5 (requires PySocks: uv pip install pysocks)
  # - name: ssh-tunnel
  #   url: "socks5://127.0.0.1:1080"
  #   enabled: true
  #   priority: 3
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
  │   ├─ Group 2 (priority 20): Tavily + Brave + Gemini ← hedged (200ms stagger, first quality-gate pass wins)
  │   ├─ Group 3 (priority 30): SearXNG / json_api
  │   └─ Group 4 (priority 90): DDG (free exhaustion fallback)
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
plugins/pivot-web-search/        Plugin payload — what gets installed into the user's plugin cache
  .claude-plugin/plugin.json     Plugin manifest (userConfig schema)
  .mcp.json                      MCP server registration (uv run -m pivot_web_search_mcp)
  pyproject.toml + uv.lock       Runtime dependencies, resolved by uv at startup
  hooks/hooks.json               PreToolUse hook — blocks built-in WebSearch/WebFetch (fail-open)
                                 SessionStart hook — async health check on startup
  pivot_web_search_mcp/          Shared application services and Plugin interfaces
    server.py                    FastMCP adapter exposing the Plugin's 3 tools
    search_service.py            Authoritative search orchestration
    fetch_service.py             Authoritative fetch/extraction orchestration
    config_service.py            Status and reload operations
    presentation.py              Markdown and JSON projections
    cli.py                       Human-facing CLI adapter
    machine_bridge.py            Host-neutral one-shot structured adapter API
    routing.py                   Priority-group routing, hedged execution, circuit breaker, quality gate
    quality_gate.py              3-tier quality gate (answer/URLs/keywords)
    backends.py                  Async search backends (DDG/Tavily/Brave/Gemini, httpx)
    extraction.py                trafilatura wrapper for URL content extraction
    http_client.py               Shared httpx client + proxy failover
    results.py                   dedup_and_rank, markdown rendering
    validation.py                URL/SSRF validation
    config.py                    YAML config loaders (hot-reload)
    defaults.py                  Smart-defaults priority table
    providers/                   Subpackage: base (SearchProvider/SearchResult), adapters (6 built-ins + ADAPTER_MAP), registry (mtime hot-reload)
    llm_search_formats.py        Strategy pattern for LLM search API formats (chat_completions, responses, gemini)
    fetch.py                     SPA detection, async JS renderer dispatch (Playwright/Tavily)
    quota.py                     Cross-session quota tracking (filelock, cross-platform)
    logging.py                   Centralized logging (stderr + optional file via PIVOT_WEB_SEARCH_DEBUG)
  config/                        Bundled fetch.yaml defaults (provider/proxy YAML lives in ~/.pivot-web-search/)
  scripts/
    health-check.py              Startup probe — reports provider availability and quota
    pretool-check.py             PreToolUse hook script — fail-open tool blocker
  skills/pivot-web-search/       Skill definition surfaced to Claude Code

.claude-plugin/marketplace.json  Marketplace manifest pointing to ./plugins/pivot-web-search
integrations/deepseek-harness/   Optional out-of-tree DSH Profile Bundle; never patches DSH source
examples/                        YAML templates for ~/.pivot-web-search/ — copy and edit for advanced setups
tests/                           355 offline tests + 7 live integration tests (pytest-asyncio)
pyproject.toml                   Workspace shell — dev deps and lint/test config (no runtime code)
```

## Testing

```sh
uv sync                               # install workspace + dev dependencies
pytest -m "not integration"           # 355 offline tests
pytest                                # all tests including live integration (requires BRAVE/TAVILY keys)
npm --prefix integrations/deepseek-harness test
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
The SessionStart health check runs asynchronously and never blocks your session. If you're not behind a proxy, leave the **Proxy URLs** field empty — `direct` is always the final fallback.

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
