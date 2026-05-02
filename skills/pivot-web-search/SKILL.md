---
name: pivot-web-search
description: >
  Search the public web for information, or extract full page content from URLs.
  Use ONLY when the user explicitly asks to search the web, look something up online,
  find recent information, check current events, or extract/fetch content from a URL.
  Never use autonomously. Four providers with automatic failover (DDG -> Tavily -> Brave -> Gemini).
  Super mode queries all providers in parallel for best coverage.
allowed-tools: ""
disable-model-invocation: true
---

# Pivot Web Search — Advanced Reference

The server handles provider selection, failover, quota management, and result quality detection automatically. This guide covers power-user parameter combinations and troubleshooting.

## mcp__pivot-web-search__WebSearch

### Parameter Combinations

- **Time-sensitive queries**: Add `timelimit: "d"` (day), `"w"` (week), `"m"` (month), `"y"` (year). The server auto-detects common time-sensitive patterns, but explicit values always win.
- **Deep research**: `provider: "tavily"`, `search_depth: "advanced"` — costs 2x Tavily credits.
- **Google Search results**: `provider: "gemini"` — uses grounded search via Gemini 2.5 Flash (~15-20s).
- **Tavily news with recency**: `topic: "news"`, `days: 3`
- **Domain-filtered search**: `allowed_domains` / `blocked_domains` — auto-routes through Tavily for native support, post-filtered for other providers.
- **Super mode**: `super_mode: true`, `max_results` caps at 20 (vs 10 in normal mode). Uses quota on all paid providers simultaneously.
- **AI answer summary**: `include_answer: true` — Tavily generates an answer when it's the active provider.

## mcp__pivot-web-search__WebFetch

### How to Use the `prompt` Parameter

The `prompt` parameter is **passed through** alongside the extracted content — the MCP server does not filter or summarize. **You** should apply the prompt yourself:

1. Pass a specific prompt describing what you need (e.g., "Extract the API authentication section")
2. When the content returns, **focus your response on what the prompt asked for**
3. Don't dump the entire extracted content to the user — summarize based on the prompt

### Limitations

- trafilatura fetches raw HTML — no JavaScript execution
- **Built-in SPA fallback**: When trafilatura returns empty, automatically tries `__NEXT_DATA__`, React Server Components (RSC), and `__NUXT_DATA__` extraction
- When both fail, consider using `curl` for raw content (e.g., `raw.githubusercontent.com`)
- Downloads capped at 10MB; content truncated at 100K chars

## References

For provider-specific API details:
- **Tavily parameters**: Read [references/tavily-api.md](${CLAUDE_SKILL_DIR}/references/tavily-api.md)
- **Gemini Search Grounding**: Read [references/gemini-api.md](${CLAUDE_SKILL_DIR}/references/gemini-api.md)

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Using Tavily for simple queries | Default auto mode tries DDG first — saves quota |
| All providers timeout | Proxy may be down; check network connectivity |
| `ddgs` not installed | `uv sync` (reinstall deps) |
| Tavily missing API key | Set `TAVILY_API_KEY` env var |
| Brave key expired | Falls through to next provider automatically in auto mode |
| Gemini missing API key | Set `GEMINI_SEARCH_API_KEY` or `GOOGLE_STUDIO_API_KEY` env var |
| Gemini slow (~15-20s) | Normal — uses Google Search grounding via Gemini 2.5 Flash |
| Super mode too slow | Gemini is the bottleneck (~15-20s); other providers return in 1-3s |
| Proxy cache stale | Delete `~/.cache/pivot-web-search-proxy-cache.json` or let self-healing fix it |
| `max_results` capped | Normal mode caps at 10, super mode caps at 20 |
| WebFetch returns truncated | Content >100K chars is truncated; use search snippets for overview |
