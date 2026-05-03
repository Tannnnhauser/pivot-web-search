# Architecture

## System Overview

```mermaid
graph TB
    subgraph "Claude Code Runtime"
        LLM[LLM / Claude]
        Hook[PreToolUse Hook<br/>blocks built-in WebSearch/WebFetch]
        Health[SessionStart Hook<br/>health-check.py]
    end

    subgraph "MCP Server (stdio)"
        Server[server.py — FastMCP]
        WS[WebSearch Tool]
        WF[WebFetch Tool]
        WC[WebSearchConfig Tool]
    end

    subgraph "Provider Layer"
        Registry[ProviderRegistry<br/>config/providers.yaml]
        P1[Provider 1<br/>e.g. DDG]
        P2[Provider 2<br/>e.g. Tavily]
        P3[Provider 3<br/>e.g. Brave]
        PN[Provider N<br/>e.g. Gemini, SearXNG, custom]
    end

    subgraph "Extraction Layer"
        Traf[trafilatura]
        SPA[SPA Detection<br/>fetch.py]
        PW[Playwright<br/>optional]
        TE[Tavily Extract API]
        BLC[Brave LLM Context API]
    end

    subgraph "Infrastructure"
        Proxy[Proxy Failover<br/>direct only by default]
        Quota[Quota Manager<br/>~/.cache/pivot-web-search/quota.json<br/>filelock + corruption recovery]
        Config[Hot-reload Config<br/>providers.yaml / proxies.yaml / fetch.yaml]
        Lock[Concurrency<br/>asyncio.Lock on caches]
        SSRF[SSRF Protection<br/>DNS resolve + IP range check]
        Redirect[SafeRedirectHandler<br/>pre-redirect cross-host block]
    end

    LLM -->|tool call| Server
    Hook -.->|exit 2 blocks| LLM
    Health -.->|startup probe| Server

    Server --> WS
    Server --> WF
    Server --> WC

    WS -->|normal mode| Registry
    WS -->|super mode| Registry
    WS -->|include_content| BLC
    Registry --> P1
    Registry --> P2
    Registry --> P3
    Registry --> PN

    WF --> Traf
    Traf -->|empty?| SPA
    SPA -->|fallback| PW
    SPA -->|fallback| TE

    P1 --> Proxy
    P2 --> Proxy
    P3 --> Proxy
    PN --> Proxy
    Traf --> Proxy
    TE --> Proxy
    BLC --> Proxy

    Proxy --> SSRF
    Proxy --> Redirect
    Proxy --> Lock

    Registry --> Quota
    WS --> Quota
    Config --> Registry
```

## Request Flow — WebSearch

```mermaid
sequenceDiagram
    participant LLM as Claude (LLM)
    participant S as server.py
    participant RT as routing.py
    participant Q as Quota Manager
    participant R as ProviderRegistry
    participant P as Provider (DDG/Tavily/Brave/Gemini)

    LLM->>S: WebSearch(query, ...)
    S->>S: _apply_smart_defaults(query)
    
    alt include_content=true
        S->>S: search_brave_llm_context(query)
        S-->>LLM: formatted content results
    else super_mode=true
        S->>R: get all enabled providers
        S->>Q: filter exhausted providers
        S->>P: parallel search (asyncio.gather)
        P-->>S: results from each provider
        S->>S: deduplicate & rank (dedup_and_rank)
        S-->>LLM: merged markdown results
    else normal mode
        S->>R: get_ordered() providers
        S->>RT: route_providers(providers, breaker)
        RT->>Q: get usage_pct per provider
        RT->>RT: tuple-sort (tier_rank, metric, priority)
        RT->>RT: circuit breaker — skip unhealthy
        RT-->>S: ordered provider list
        loop each provider (tuple-sorted)
            S->>P: search(query, max_results)
            P-->>S: SearchResult
            alt results >= min_acceptable
                S->>Q: record_usage(provider)
                S-->>LLM: formatted markdown
            else too few results
                S->>S: try next provider
            end
        end
    end
```

## Request Flow — WebFetch

```mermaid
sequenceDiagram
    participant LLM as Claude (LLM)
    participant S as server.py
    participant T as trafilatura
    participant F as fetch.py (SPA detection)
    participant R as JS Renderer (Playwright/Tavily)

    LLM->>S: WebFetch(url, prompt, query?, max_chars?)
    S->>S: validate URL(s)
    S->>T: extract_trafilatura(urls)
    T-->>S: extracted content

    loop each URL
        S->>F: is_empty_content(content)?
        alt content is empty / SPA shell
            S->>F: render_with_fallback(url, config, query)
            F->>R: render (based on js_renderer config)
            R-->>F: rendered content
            F-->>S: fallback content
        end
        S->>S: apply max_chars truncation
    end

    S-->>LLM: formatted content + prompt
```

## Provider Failover Strategy

```mermaid
graph LR
    subgraph "Priority Order (configurable via YAML)"
        P1[Provider 1<br/>priority: 10<br/>e.g. DDG — free] --> P2[Provider 2<br/>priority: 20<br/>e.g. Tavily]
        P2 --> P3[Provider 3<br/>priority: 30<br/>e.g. Brave]
        P3 --> PN[Provider N<br/>priority: 40<br/>e.g. Gemini]
    end

    subgraph "Quota-Aware Reordering"
        Q{usage_pct<br/>comparison}
        Q -->|lowest usage first| Reordered[Reordered list<br/>skip exhausted]
    end

    P1 --> Q
    P2 --> Q
    P3 --> Q
    PN --> Q
```

> Built-in adapters: DDG, Tavily, Brave, Gemini, SearXNG, a generic `json_api` adapter for any REST search API, and `llm_search` for any LLM with built-in web search grounding (Perplexity Sonar Pro, OpenAI web_search, SAP AI Core, etc.). New adapters are one class. `searxng`, `json_api`, and `llm_search` types can be instantiated multiple times with different names — each instance gets independent priority, quota tracking, and circuit breaker state. The `llm_search` adapter uses a Strategy pattern (`LlmSearchFormat`) with pluggable format handlers (`chat_completions`, `responses`, `gemini`).

## WebFetch JS Fallback

```mermaid
graph TD
    URL[URL Input] --> Traf[trafilatura extract]
    Traf --> Check{Content empty<br/>or SPA shell?}
    Check -->|No| Done[Return content]
    Check -->|Yes| Config{js_renderer<br/>config}
    Config -->|none| Empty[Return empty/error]
    Config -->|playwright| PW[Headless Chromium<br/>extra: browser]
    Config -->|tavily| TV[Tavily Extract API<br/>extract_depth: advanced]
    PW --> Done
    TV --> Done
```

## File Structure

```
pivot-web-search/
├── .claude-plugin/          # Claude Code plugin manifest
│   ├── plugin.json          # Tool declarations, userConfig schema
│   └── marketplace.json     # Marketplace listing
├── .mcp.json                # MCP server launch config (stdio)
├── config/
│   ├── providers.yaml       # Search providers (type, priority, key)
│   ├── proxies.yaml         # Proxy endpoints (failover order)
│   └── fetch.yaml           # WebFetch config (JS renderer, limits)
├── hooks/
│   └── hooks.json           # PreToolUse block + SessionStart health
├── scripts/
│   ├── health-check.py      # Startup probe (parallel provider check)
│   └── pretool-check.py     # PreToolUse hook — fail-open tool blocker
├── pivot_web_search_mcp/
│   ├── __init__.py
│   ├── __main__.py          # Entry: mcp.run(transport="stdio")
│   ├── server.py            # FastMCP server, 3 tools, smart defaults
│   ├── search.py            # Core: search, proxy fallover, extraction, dedup_and_rank
│   ├── providers.py         # ProviderRegistry, adapters, config loaders
│   ├── llm_search_formats.py # Strategy pattern for LLM search API formats
│   ├── routing.py           # Tuple-sort routing, circuit breaker, pacing pressure
│   ├── fetch.py             # SPA detection, JS renderer dispatch
│   ├── logging.py           # Centralized logging (stderr + optional file)
│   └── quota.py             # Per-provider quota tracking, filelock (cross-platform)
├── tests/                   # 232 tests (pytest-asyncio), 14 modules
├── skills/pivot-web-search/       # Skill definition for Claude Code
└── docs/                    # Architecture documentation
```

## Key Design Principles

1. **Zero mandatory external deps for search** — DDG works with no API key
2. **Graceful degradation** — if a provider fails or is exhausted, next one takes over
3. **Config-driven** — providers, proxies, and fetch behavior all via YAML, hot-reloadable
4. **Quota-aware** — tracks API usage, prefers cheapest available provider
5. **Optional heavy deps** — Playwright only installed via the `browser` extra (`uv sync --extra browser`)
6. **Proxy failover** — every HTTP call goes through the proxy chain (direct by default, configurable)
7. **Security by default** — SSRF protection, pre-redirect blocking, credential redaction
8. **Cross-platform** — filelock instead of fcntl, works on Windows/macOS/Linux
9. **Async-safe** — shared mutable state protected by `asyncio.Lock`; config loaders use `threading.Lock`
