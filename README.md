# Pivot Web Search

> Resilient multi-provider web search and page extraction for MCP hosts, the command line, and host adapters.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Tests: 358](https://img.shields.io/badge/tests-358%20offline-brightgreen.svg)]()

## What Is This?

Pivot Web Search exposes one set of shared search, fetch, and configuration services through three interfaces:

- **MCP server** (`pivot-web-search-mcp`) — for Claude Code and any MCP-aware host
- **CLI** (`pivot-web-search`) — for people and shell scripts, no AI host required
- **JSON bridge** (`pivot-web-search-bridge`) — a host-neutral subprocess interface for adapters

It routes each query across multiple providers, fails over automatically when results are weak or a provider is down, tracks quota and health, and extracts page content locally — with **no Anthropic API dependency**, so it works on Amazon Bedrock and other API providers. A **Claude Code plugin** and an optional **DeepSeek Harness adapter** provide host-specific installation; adopting Pivot never requires patching a host's source.

DuckDuckGo works with no API key; adding an API-backed provider (Tavily, Brave, Gemini) improves reliability.

## Choose Your Interface

| You use… | Start here |
|---|---|
| **Claude Code** | [Install the plugin](#claude-code-plugin) |
| **Another MCP host** (Cursor, Claude Desktop, …) | [Configure `pivot-web-search-mcp`](#mcp-hosts) |
| **Terminal / scripts / CI** | [Install and run the CLI](#cli) |
| **DeepSeek Harness** | [Install runtime + DSH bundle](#deepseek-harness) |

## Key Features

- **One runtime, three interfaces** — the same search/fetch/config services back MCP, the CLI, and the JSON bridge.
- **Multi-provider failover** — quota-aware routing across providers; weak or exhausted providers are skipped, partial results kept as fallback.
- **Super mode** — query all providers in parallel, dedupe by URL, rank by cross-provider agreement.
- **Local page extraction** — full content via trafilatura, with Next.js/Nuxt.js SPA fallbacks and an optional JS renderer.
- **Hot-reloadable config** — add/remove/reorder providers and proxies via YAML; changes apply on the next request.
- **Actionable diagnostics** — when everything fails, get per-provider failure reasons and suggestions, not a generic error.

Single-provider search fails when an API is unavailable, rate-limited, blocked, or out of quota. Pivot routes each request across configured providers, keeps usable partial results, and exposes the same behavior through MCP, CLI, and adapter-friendly JSON.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — manages Python and dependencies automatically (used by every interface).
- At least one search provider (DDG needs no API key).

> **Recommended:** configure at least one free API key — [Tavily](https://tavily.com) (1000 credits/month, no card) or [Brave](https://brave.com/search/api/) (1000 queries/month, card required). DDG is a free fallback but can be unreliable under heavy use.

Per-host setup is covered in each Quick Start below.

## Quick Starts

### Claude Code plugin

**Step 1** — add the marketplace (one-time):

```sh
claude plugin marketplace add https://github.com/Tannnnhauser/pivot-web-search.git
```

**Step 2** — install:

```sh
claude plugin install pivot-web-search
```

The plugin prompts for configuration at install time:

| Setting | Description |
|---|---|
| **Tavily API Key** | Stored in system keychain. Set to enable Tavily (or inherit `TAVILY_API_KEY` from the shell). |
| **Brave Search API Key** | Stored in system keychain. Set to enable Brave (or inherit `BRAVE_API_KEY`). |
| **Gemini API Key** | Stored in system keychain. Set to enable Gemini (or inherit `GEMINI_SEARCH_API_KEY` / `GOOGLE_STUDIO_API_KEY`). |
| **Proxy URLs** | Comma-separated proxies to try in order. `direct` is **always appended as the final fallback** — to disable that, use `~/.pivot-web-search/proxies.yaml`. |

Providers are enabled automatically for whichever keys you supply; DDG is always on. Routing order and timeouts come from [smart defaults](#configuration) — the order you supply keys does not matter. Reconfigure anytime via `claude plugin configure pivot-web-search`.

**Verify:** ask Claude Code to run `WebSearchConfig` with action `status` — you should see a provider health report.

<details>
<summary>Key resolution & macOS GUI caveat</summary>

For each provider key the plugin reads (1) the standard env var (e.g. `TAVILY_API_KEY`) inherited from the shell — **wins if set** — then (2) the `/plugin` UI value (injected as `PIVOT_USERCONFIG_TAVILY_API_KEY`). To use the UI value instead, `unset TAVILY_API_KEY` in your shell.

**macOS:** when Claude Code launches from Spotlight or the Dock it does **not** see `~/.zshrc` exports. Start it from a terminal, set keys via the `/plugin` UI, or add them to the `env` block in `~/.claude/config.json`.
</details>

### MCP hosts

Any MCP-aware host (Claude Code, Claude Desktop, Cursor, …) can run the server directly via `uvx` — no clone, no venv. Add to your `.mcp.json`:

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

Pin a version with `@v1.1.0`:

```
git+https://github.com/Tannnnhauser/pivot-web-search.git@v1.1.0#subdirectory=plugins/pivot-web-search
```

Advanced provider/proxy config lives in `~/.pivot-web-search/*.yaml` (see [Configuration](#configuration)) and applies to every launch method.

### CLI

The installed package provides `pivot-web-search`, a human-facing command backed by the same services as the MCP tools — for use in a terminal, a script, or CI, without going through an AI host. Install it standalone with uv:

```sh
uv tool install 'git+https://github.com/Tannnnhauser/pivot-web-search.git@v1.1.0#subdirectory=plugins/pivot-web-search'
```

```sh
pivot-web-search search "latest Python release" --format json
pivot-web-search fetch https://example.com --format md
pivot-web-search config status
```

**`search`** flags:

| Flag | Default | Description |
|---|---|---|
| `--max-results` | `5` | Number of results (1–10, or 1–20 with `--super`) |
| `--provider` | `auto` | Force a configured provider by name |
| `--super` | off | Query all providers in parallel |
| `--news` | off | Search news instead of the general web |
| `--timelimit` | — | `d` / `w` / `m` / `y` recency filter |
| `--include-answer` | off | Include an AI answer when supported |
| `--search-depth` | `basic` | `basic` or `advanced` (Tavily) |
| `--topic` | `general` | `general` or `news` (Tavily) |
| `--days` | — | Limit news to recent N days |
| `--include-domains` | — | Domain allowlist |
| `--exclude-domains` | — | Domain blocklist |
| `--include-content` | off | Return pre-extracted page content (Brave LLM Context) |
| `--max-content-tokens` | `8192` | Token budget for `--include-content` |
| `--region` | `wt-wt` | DDG region (CLI-only; no MCP equivalent) |
| `--format` | `md` | `md` or `json` |

**`fetch`** (alias `extract`): `--query` (relevance hint for JS renderers), `--max-chars` (per-URL truncation), `--format` (`json` default, or `md`).

**`config`**: positional action, `status` (default) or `reload`.

### DeepSeek Harness

The optional [`pivot-web-search-dsh`](integrations/deepseek-harness/) Profile Bundle registers Pivot as DSH's existing `web_search` and `web_fetch` provider through DSH's published Bundle, `ctx.web`, and `ctx.subprocess` APIs. The model keeps seeing DSH's standard tools — no second set of Pivot-specific tools. Host integrations use public extension APIs; the adapter requires no DSH fork or source patch.

Install the runtime and the Bundle:

```sh
uv tool install 'git+https://github.com/Tannnnhauser/pivot-web-search.git@v1.1.0#subdirectory=plugins/pivot-web-search'
dsh plugin --profile web add pivot-web-search-dsh
```

Restart the profile after adding the Bundle. It selects provider `pivot` as `searchProvider`/`fetchProvider` and enables DSH's `tool-web` entry (which the shipped web profile disables). **Verify** with `dsh --profile web --dump-config` — you should see `searchProvider: pivot`, `fetchProvider: pivot`, an enabled `tool-web`, and `pivot-web-search-provider`.

Provider keys are forwarded explicitly by `cordis.patch.yml` (`TAVILY_API_KEY`, `BRAVE_API_KEY`, `GEMINI_SEARCH_API_KEY`, `GOOGLE_STUDIO_API_KEY`, plus `PIVOT_WEB_SEARCH_PROXIES`); config files under `~/.pivot-web-search/` are read directly.

> **Custom providers:** the bridge subprocess receives **only** the env vars listed in `cordis.patch.yml` — it does not inherit the parent shell. If a custom provider's `api_key_env` names a key outside the list above (e.g. a self-hosted gateway token), add that variable to the Bundle's `env` block or it will not reach the bridge.

Development install and removal steps are documented in [`integrations/deepseek-harness/`](integrations/deepseek-harness/).

## Configuration

Pivot runs on **auto-detection** by default — supply API keys (UI or shell env) and matching providers are enabled with smart routing defaults. No YAML required for the common case.

For advanced setups, drop YAML into `~/.pivot-web-search/`:

| File | Purpose |
|---|---|
| `providers.yaml` | Take over provider config: SearXNG, custom JSON APIs, LLM-search providers, explicit priorities |
| `proxies.yaml` | Take over proxy config: SOCKS5, forced-proxy (no direct fallback), per-proxy priority |

**Precedence is all-or-nothing per file:** if a file exists, auto-detection for that concern is bypassed — list every entry you want (including DDG). Templates live in [`examples/`](examples/).

### `~/.pivot-web-search/providers.yaml`

Providers are tried by priority (lower = first). Same-priority providers are hedged — queried concurrently with staggered starts, first quality result wins. Without an explicit `priority`, smart defaults apply by type.

```yaml
providers:
  - name: tavily
    type: tavily
    api_key_env: TAVILY_API_KEY

  - name: brave
    type: brave
    api_key_env: BRAVE_API_KEY

  - name: gemini
    type: gemini
    api_key_env: GEMINI_SEARCH_API_KEY
    model: gemini-2.5-flash

  - name: ddg
    type: ddg

  # Self-hosted SearXNG
  # - name: searxng-local
  #   type: searxng
  #   endpoint: "http://localhost:8888/search"

  # Generic JSON API (Serper, Google CSE, etc.) — multiple instances allowed,
  # each with independent priority, quota tracking, and circuit-breaker state.
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

**Smart default priorities** (when no explicit `priority`):

| Type | Priority | Timeout |
|---|---|---|
| `llm_search` | 10 | 15s |
| `tavily` / `brave` / `gemini` | 20 | 4s / 4s / 20s |
| `searxng` / `json_api` | 30 | 6s |
| `ddg` | 90 | 6s |

> **Note:** an `llm_search` provider at priority 10 runs ahead of Tavily/Brave with a 15s timeout, so every query may take 15+s. Bump its `priority` above 20 if latency matters more than answer quality.

### LLM Search Providers (`type: llm_search`)

For any model with built-in web search grounding (Perplexity Sonar Pro, OpenAI with `web_search`, SAP AI Core, etc.). These return an AI answer plus cited URLs. Configure via `~/.pivot-web-search/providers.yaml` (template at [`examples/providers.yaml`](examples/providers.yaml)).

**`chat_completions`** — any `/chat/completions`-compatible endpoint with built-in search:

```yaml
  - name: sonar-pro
    type: llm_search
    api_format: chat_completions
    endpoint: "https://api.perplexity.ai/chat/completions"
    model: sonar-pro
    api_key_env: PERPLEXITY_API_KEY
    timeout: 15
```

Response parsing tries, in order: `search_results` (Perplexity/Sonar), `annotations` with `type: url_citation` (OpenAI Chat Completions), then top-level `citations`.

**`responses`** — OpenAI Responses API (`/responses`) with the `web_search` tool:

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

**Common fields:**

| Field | Required | Description |
|---|---|---|
| `api_format` | No | `chat_completions` (default) or `responses` |
| `endpoint` | Yes | Full URL to the API endpoint |
| `model` | Yes | Model identifier |
| `api_key_env` | Yes | Env var holding the API key (sent as Bearer token) |
| `max_tokens` | No | Max response tokens (default: 500 / 4000 for responses) |
| `timeout` | No | Request timeout in seconds (default: 30) |
| `system_prompt` | No | System prompt (chat_completions only) |
| `headers` | No | Additional request headers |
| `web_search_options` | No | Extra search options (chat_completions only) |

The `gemini` type is also LLM-search internally (Google Search grounding) but keeps `type: gemini` for backward compatibility and dual-key fallback.

### `~/.pivot-web-search/proxies.yaml`

When present it takes over completely — the install-time `Proxy URLs` field is ignored and `direct` is **not** auto-appended (the escape hatch for forced-proxy setups).

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

Controls WebFetch behavior including the JS rendering fallback:

```yaml
js_renderer: none         # none (default), "playwright", or "tavily"
max_chars: 100000         # content truncation limit
```

Set `js_renderer: playwright` for JavaScript-heavy sites (requires `uv sync --extra browser`).

## MCP Tool Reference

Via MCP, tools are prefixed: `mcp__pivot-web-search__WebSearch`, `mcp__pivot-web-search__WebFetch`, `mcp__pivot-web-search__WebSearchConfig`.

### `WebSearch`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | str | *required* | Search query |
| `provider` | str | `"auto"` | Force provider: `auto` / `ddg` / `tavily` / `brave` / `gemini` / `searxng`, or any registered name |
| `super_mode` | bool | `false` | Query all providers in parallel |
| `max_results` | int | `5` | 1–10 (1–20 in super mode) |
| `allowed_domains` | list[str] | `null` | Only include results from these domains |
| `blocked_domains` | list[str] | `null` | Exclude results from these domains |
| `news` | bool | `false` | Search news instead of web |
| `timelimit` | str | `null` | `d` / `w` / `m` / `y` |
| `include_answer` | bool | `false` | AI-generated answer summary (Tavily) |
| `include_content` | bool | `false` | Pre-extracted page content (Brave LLM Context) |
| `max_content_tokens` | int | `8192` | Token budget when `include_content=true` (1024–32768) |
| `search_depth` | str | `"basic"` | `basic` or `advanced` — advanced costs 2x credits (Tavily) |
| `topic` | str | `"general"` | `general` or `news` (Tavily) |
| `days` | int | `null` | Limit news to recent N days (Tavily) |

### `WebFetch`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | str / list[str] | *required* | URL(s) to extract. HTTP auto-upgrades to HTTPS. Batch mode with multiple URLs. |
| `query` | str | `null` | Optional relevance query for JS-fallback renderers |
| `max_chars` | int | `null` | Truncate output to this many characters (default: 100,000) |

**Behaviors:** 15-minute per-URL cache · binary-content detection & rejection · cross-host redirect safety (blocks before following) · SPA fallback (`__NEXT_DATA__` / RSC payload / `__NUXT_DATA__`).

### `WebSearchConfig`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `action` | str | `"status"` | `status` — provider health, quota, config sources; `reload` — hot-reload YAML |

`status` returns provider health, per-provider quota, and **config-source annotations** showing where each setting comes from (env var, YAML path, or built-in default).

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

Each provider independently tries all configured proxies (direct → myproxy1 → …) with per-host caching persisted to `~/.cache/pivot-web-search/`.

### Quota Management

Usage is tracked across sessions in `~/.cache/pivot-web-search/quota.json`:

| Provider | Tracking | Free tier | Details |
|---|---|---|---|
| **DuckDuckGo** | Not tracked | Unlimited | Free, no API key |
| **Tavily** | API sync | 1000 credits/month | Calls `GET /usage` at startup for real credit data |
| **Brave** | Response headers | Rolling 30-day window | Parses `X-RateLimit-Remaining` / `X-RateLimit-Reset` |
| **Gemini** | Local (daily) | Varies (resets PT midnight) | Set limit via `PIVOT_WEB_SEARCH_GEMINI_QUOTA` |

Quota-aware scheduling skips providers at 100% usage; resets on calendar rollover.

## Architecture

Three interfaces sit on one set of shared services:

```
MCP server ─┐
CLI ────────┼─→ search / fetch / config services ─→ provider registry ─→ providers
JSON bridge ┘                                                            (DDG/Tavily/Brave/Gemini/SearXNG/json_api/llm_search)
```

- **`server.py`** — FastMCP adapter (the 3 MCP tools) · **`cli.py`** — CLI adapter · **`machine_bridge.py`** — host-neutral JSON adapter
- **`search_service.py` / `fetch_service.py` / `config_service.py`** — authoritative orchestration · **`presentation.py`** — Markdown/JSON projections
- **`routing.py` / `quality_gate.py`** — priority-group routing, hedging, circuit breaker · **`backends.py` / `extraction.py` / `http_client.py`** — provider I/O, extraction, proxy failover
- **`providers/`** — adapter base, 6 built-ins, mtime hot-reload registry · **`quota.py` / `config.py`** — cross-session quota, YAML hot-reload

The Claude Code plugin payload lives under `plugins/pivot-web-search/`; the DSH adapter under `integrations/deepseek-harness/` (never patches DSH source); YAML templates under `examples/`.

## Testing

```sh
uv sync                                # install workspace + dev deps
uv run pytest -m "not integration" -q  # 358 offline tests
uv run pytest -m integration -vv       # 7 live network/API tests
uv run pytest                          # all 365 Python tests
npm --prefix integrations/deepseek-harness test
npm --prefix integrations/deepseek-harness pack --dry-run
```

For local Claude Code development: `claude --plugin-dir /path/to/pivot-web-search/`.

## Troubleshooting

**Enable debug logging** — set `PIVOT_WEB_SEARCH_DEBUG=1`; timestamped logs go to `~/.cache/pivot-web-search/server.log`.

**No results from any provider** — run `WebSearchConfig` action `status` (or `pivot-web-search config status`) to check provider health and active config sources. Ensure at least one provider has a valid key (or DDG is reachable).

**SSL certificate errors on macOS** — the plugin uses `certifi`; if errors persist: `uv sync --upgrade-package certifi`.

**DuckDuckGo rate limiting (403)** — the circuit breaker bypasses DDG for 60s after consecutive failures, then probes to recover. Configure an API-backed provider for reliability.

**`trafilatura` extraction returns empty** — some JS-heavy sites need a renderer. Set `js_renderer: playwright` in `config/fetch.yaml`, then `uv sync --extra browser && playwright install chromium`.

## Alternatives Comparison

| Feature | Pivot Web Search | Single-provider MCP tools | Built-in WebSearch |
|---|---|---|---|
| Multi-provider failover | 4+ providers, auto-fallback | Single point of failure | N/A on Bedrock |
| Quota management | Cross-session tracking | None | N/A |
| Super mode (parallel) | All providers at once | Not possible | N/A |
| Local content extraction | trafilatura + SPA fallback | Usually Tavily Extract | Anthropic-hosted |
| Interfaces | MCP + CLI + JSON bridge | MCP only | Built-in only |
| Works on Bedrock | Yes | Yes | No |
| Self-hosted | Yes | Varies | No |

## License

Apache-2.0
