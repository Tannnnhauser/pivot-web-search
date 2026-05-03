# Pivot Web Search

This is a **Claude Code Plugin** repository. It replaces Claude Code's built-in WebSearch and WebFetch tools with a configurable failover search engine (DDG -> Tavily -> Brave -> Gemini) and content extraction with optional JS rendering fallback.

## Plugin Installation Context

Users install this plugin at different scopes. All design and implementation decisions must account for all three:

- **User scope** (`~/.claude.json`) — available across all projects on the user's machine
- **Project scope** (`.mcp.json` at project root, checked into VCS) — shared with team members
- **Local scope** (`~/.claude.json` under project path) — private to one user in one project

Implications:
- File paths must use `${CLAUDE_PLUGIN_ROOT}` — never assume a fixed install location
- Config/cache files go to `~/.cache/pivot-web-search/` (user-global), not relative to the plugin directory
- Hooks (PreToolUse, SessionStart) apply at whatever scope the plugin is enabled — they affect all sessions in that scope
- Environment variables from `userConfig` are injected by Claude Code at runtime, not read from disk
- The plugin must work correctly whether it's the only plugin installed or one of many

## Tool Usage

- **NEVER call the built-in WebSearch or WebFetch tools.** They are blocked by this plugin's PreToolUse hook and will fail with exit code 2.
- Use the MCP server tools instead: `mcp__pivot-web-search__WebSearch` and `mcp__pivot-web-search__WebFetch`.
- These are available to all agent types including Explore and Plan subagents.

## Three Tools

- **`mcp__pivot-web-search__WebSearch`** — Search the web. Quota-aware provider failover with quality detection and smart defaults. Auto-detects time-sensitive and news queries. Supports provider selection, domain filtering, news search, super mode (all providers in parallel). Use `include_content=true` to get pre-extracted page content with results (via Brave LLM Context API).
- **`mcp__pivot-web-search__WebFetch`** — Extract full page content from URLs. Uses trafilatura with Next.js/Nuxt.js SPA fallback. Configurable JS renderer fallback (Playwright or Tavily Extract) for dynamic pages. Supports batch URLs, query-aware extraction, and configurable truncation.
- **`mcp__pivot-web-search__WebSearchConfig`** — Runtime config inspection (`status`) and hot-reload (`reload`). Status includes provider health and quota usage.

## Key Features

- **Quota-aware scheduling**: Providers sorted by API usage (lowest first), exhausted providers skipped. Quota persisted to `~/.cache/pivot-web-search/quota.json`.
- **Smart defaults**: Time-sensitive queries auto-get recency filter; news-related queries auto-enable news mode. Explicit parameters always win.
- **Quality detection**: If a provider returns fewer than 2 results, failover continues to the next provider.
- **JS rendering fallback**: Configure `config/fetch.yaml` with `js_renderer: playwright` or `tavily` to handle JavaScript-rendered SPAs. Playwright requires `uv sync --extra browser`.
- **Brave LLM Context**: WebSearch with `include_content=true` uses Brave's LLM Context API for search+content in one call with token budget control.

## Testing

```sh
uv sync --extra dev
pytest -m "not integration"     # 193 offline tests (~4s)
pytest                          # all tests including live API integration
```

## Development Rules

- **Always update README** when implementing features, enhancements, or behavioral changes. Documentation must stay in sync with code — treat it as part of the implementation, not a follow-up.
- **Run tests before committing** — all 193 offline tests must pass (`pytest -m "not integration"`).
- **Use `log()` from `pivot_web_search_mcp.logging`** for all diagnostic output. Never use `print(..., file=sys.stderr)` directly.
