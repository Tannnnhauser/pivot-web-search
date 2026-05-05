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
    participant QG as quality_gate.py
    participant Q as Quota Manager
    participant R as ProviderRegistry
    participant P as Provider (Tavily/Brave/DDG/Gemini/...)

    LLM->>S: WebSearch(query, ...)
    S->>S: _apply_smart_defaults(query)
    
    alt include_content=true
        S->>S: search_brave_llm_context(query)
        S-->>LLM: formatted content results
    else super_mode=true
        S->>RT: select_providers(affinity filter)
        RT->>Q: skip exhausted providers
        RT->>RT: skip circuit-broken providers
        S->>P: parallel search (per-provider timeouts)
        P-->>S: results from each provider
        S->>S: deduplicate & rank (RRF)
        S-->>LLM: merged markdown results
    else normal mode
        S->>RT: execute_search(query, providers, breaker)
        RT->>RT: select_providers (affinity + quota + breaker gates)
        RT->>RT: build_priority_groups (group by effective_priority)
        loop each priority group (high → low)
            alt single provider in group
                RT->>P: search(query) with timeout
            else multiple same-priority (hedged)
                RT->>P: staggered starts (200ms delay)
                P-->>RT: first response
            end
            RT->>QG: quality_gate(query, results, answer)
            alt verdict = ACCEPT
                RT-->>S: return result
            else verdict = PARTIAL
                RT->>RT: keep best, try next group
            end
        end
        S-->>LLM: formatted markdown
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

## Provider Routing Strategy

```mermaid
graph TB
    subgraph "Priority-Group Routing"
        direction TB
        SG[Smart Defaults<br/>Tavily/Brave=10, SearXNG=30<br/>Gemini=40, LLM=60, DDG=90]
        AG[Affinity Gate<br/>deep providers excluded<br/>unless explicitly requested]
        QG[Quota Gate<br/>skip exhausted]
        BG[Breaker Gate<br/>skip circuit-open]
        PG[Priority Grouping<br/>same priority = hedged group]
    end

    subgraph "Hedged Execution (same priority)"
        H1[Provider A fires at t=0]
        H2[Provider B fires at t=200ms]
        H3[First quality-gate ACCEPT wins]
    end

    subgraph "Quality Gate (3-tier)"
        G0[Gate 0: AI answer ≥ 40 chars?]
        G1[Gate 1: Unique URLs ≥ 2?]
        G2[Gate 2: Query term overlap?]
    end

    SG --> AG --> QG --> BG --> PG
    PG --> H1
    PG --> H2
    H1 --> H3
    H2 --> H3
    H3 --> G0
    G0 -->|yes| ACCEPT[Return result]
    G0 -->|no| G1
    G1 -->|yes| G2
    G1 -->|no| PARTIAL[Keep best, try next group]
    G2 -->|yes| ACCEPT
    G2 -->|no| PARTIAL
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
├── ARCHITECTURE.md          # This file
├── config/
│   ├── providers.yaml       # Search providers (type, priority, timeout, affinity)
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
│   ├── search.py            # Core: search, proxy failover, extraction, dedup_and_rank
│   ├── providers.py         # ProviderRegistry, adapters, config loaders
│   ├── llm_search_formats.py # Strategy pattern for LLM search API formats
│   ├── routing.py           # Priority-group routing, hedging, circuit breaker
│   ├── quality_gate.py      # 3-tier quality gate (answer/URLs/keywords)
│   ├── fetch.py             # SPA detection, JS renderer dispatch
│   ├── logging.py           # Centralized logging (stderr + optional file)
│   └── quota.py             # Per-provider quota tracking, filelock (cross-platform)
├── tests/                   # 265 tests (pytest-asyncio), 15 modules
├── skills/pivot-web-search/       # Skill definition for Claude Code
└── docs/                    # Design documents (not tracked in git)
```

## Key Design Principles

1. **Zero mandatory external deps for search** — DDG works with no API key
2. **Priority-group routing** — same-priority providers hedged, first quality-gate pass wins
3. **Config-driven** — providers, proxies, and fetch behavior all via YAML, hot-reloadable
4. **Quota-aware** — binary exclusion (exhausted = skip), no implicit throttling
5. **Per-provider timeouts** — no global budget, each provider gets its configured deadline
6. **Smart defaults** — quality-first ordering (Tavily/Brave > Gemini > LLM > DDG) when no explicit priority
7. **3-tier quality gate** — AI answer presence, URL count, keyword overlap drives failover
8. **Fixed 60s circuit breaker** — 3 consecutive failures opens, no exponential backoff
9. **Provider affinity** — deep research providers excluded from normal routing unless explicitly requested
10. **Security by default** — SSRF protection, pre-redirect blocking, credential redaction
11. **Cross-platform** — filelock instead of fcntl, works on Windows/macOS/Linux
12. **Async-safe** — shared mutable state protected by `asyncio.Lock`; config loaders use `threading.Lock`
