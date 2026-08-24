# Architecture

## System Overview

```mermaid
graph TB
    subgraph "Claude Code Runtime"
        LLM[LLM / Claude]
        Hook[PreToolUse Hook<br/>blocks built-in WebSearch/WebFetch]
        Health[SessionStart Hook<br/>health-check.py]
    end

    subgraph "Plugin and Local Interfaces"
        Server[server.py — FastMCP]
        WS[WebSearch Tool]
        WF[WebFetch Tool]
        WC[WebSearchConfig Tool]
        CLI[cli.py]
        Bridge[machine_bridge.py]
    end

    SearchCore[search_service.py]
    FetchCore[fetch_service.py]
    ConfigCore[config_service.py]

    subgraph "Optional External Adapters"
        DSH[pivot-web-search-dsh<br/>out-of-tree Profile Bundle]
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

    WS --> SearchCore
    WF --> FetchCore
    WC --> ConfigCore
    CLI --> SearchCore
    CLI --> FetchCore
    CLI --> ConfigCore
    DSH --> Bridge
    Bridge --> SearchCore
    Bridge --> FetchCore

    SearchCore -->|normal mode| Registry
    SearchCore -->|super mode| Registry
    SearchCore -->|include_content| BLC
    Registry --> P1
    Registry --> P2
    Registry --> P3
    Registry --> PN

    FetchCore --> Traf
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
    SearchCore --> Quota
    Config --> Registry
```

## Product and Adoption Boundary

The repository's product is the Claude Code Plugin. MCP is the Plugin's
model-facing transport, while the CLI and machine bridge are additional
interfaces over the same Python services.

Host adoption is always out-of-tree. A host either connects to the MCP server
or installs a thin adapter maintained by Pivot. The DeepSeek Harness adapter is
such a package: its Profile Bundle registers providers through DSH's published
`ctx.web` and `ctx.subprocess` contracts. It does not patch, fork, or require a
build of the DSH source repository.

## Request Flow — WebSearch

```mermaid
sequenceDiagram
    participant LLM as Claude (LLM)
    participant S as server.py
    participant C as search_service.py
    participant RT as routing.py
    participant QG as quality_gate.py
    participant Q as Quota Manager
    participant R as ProviderRegistry
    participant P as Provider (Tavily/Brave/DDG/Gemini/...)

    LLM->>S: WebSearch(query, ...)
    S->>C: SearchRequest
    C->>C: validate + smart defaults
    
    alt include_content=true
        C->>C: search_brave_llm_context(query)
    else super_mode=true
        C->>RT: select_providers(affinity filter)
        RT->>Q: skip exhausted providers
        RT->>RT: skip circuit-broken providers
        C->>P: parallel search (per-provider timeouts)
        P-->>C: results from each provider
        C->>C: deduplicate & rank (provider-agreement count)
    else normal mode
        C->>RT: execute_search(query, providers, breaker)
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
                RT-->>C: return result
            else verdict = PARTIAL
                RT->>RT: keep best, try next group
            end
        end
    end
    C-->>S: structured SearchResponse
    S-->>LLM: Markdown string
```

## Request Flow — WebFetch

```mermaid
sequenceDiagram
    participant LLM as Claude (LLM)
    participant S as server.py
    participant F as fetch_service.py
    participant T as trafilatura
    participant H as fetch.py (SPA detection)
    participant R as JS Renderer (Playwright/Tavily)

    LLM->>S: WebFetch(url, query?, max_chars?)
    S->>F: FetchRequest
    F->>F: validate URL(s)
    F->>T: extract_trafilatura(urls)
    T-->>F: extracted content

    loop each URL
        F->>H: is_empty_content(content)?
        alt content is empty / SPA shell
            F->>H: render_with_fallback(url, config, query)
            H->>R: render (based on js_renderer config)
            R-->>H: rendered content
            H-->>F: fallback content
        end
        F->>F: apply max_chars truncation
    end

    F-->>S: structured FetchResponse
    S-->>LLM: formatted content
```

## Provider Routing Strategy

```mermaid
graph TB
    subgraph "Priority-Group Routing"
        direction TB
        SG[Smart Defaults<br/>LLM=10, Tavily/Brave/Gemini=20<br/>SearXNG/json_api=30, DDG=90]
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
pivot-web-search/                      # marketplace + dev repo root
├── .claude-plugin/
│   └── marketplace.json               # Marketplace manifest → ./plugins/pivot-web-search
├── plugins/pivot-web-search/          # Plugin payload (CLAUDE_PLUGIN_ROOT at install time)
│   ├── .claude-plugin/plugin.json     # Tool declarations, userConfig schema
│   ├── .mcp.json                      # MCP server launch config (stdio)
│   ├── pyproject.toml + uv.lock       # Runtime deps, resolved by uv at startup
│   ├── config/
│   │   ├── providers.yaml             # Search providers (type, priority, timeout, affinity)
│   │   ├── proxies.yaml               # Proxy endpoints (failover order)
│   │   └── fetch.yaml                 # WebFetch config (JS renderer, limits)
│   ├── hooks/
│   │   └── hooks.json                 # PreToolUse block + SessionStart health
│   ├── scripts/
│   │   ├── health-check.py            # Startup probe (parallel provider check)
│   │   └── pretool-check.py           # PreToolUse hook — fail-open tool blocker
│   ├── pivot_web_search_mcp/
│   │   ├── __init__.py
│   │   ├── __main__.py                # Entry: mcp.run(transport="stdio")
│   │   ├── server.py                  # FastMCP adapter, 3 Plugin tools
│   │   ├── search_service.py          # Authoritative structured search orchestration
│   │   ├── fetch_service.py           # Authoritative extraction and fallback orchestration
│   │   ├── config_service.py          # Status and reload operations
│   │   ├── presentation.py            # Markdown and JSON projections
│   │   ├── cli.py                     # Human-facing CLI
│   │   ├── machine_bridge.py          # Host-neutral one-shot JSON protocol
│   │   ├── backends.py                # HTTP adapters: DDG/Tavily/Brave/Gemini
│   │   ├── extraction.py              # trafilatura wrapper
│   │   ├── http_client.py             # Shared httpx client + proxy failover
│   │   ├── results.py                 # dedup_and_rank, markdown rendering
│   │   ├── validation.py              # URL/SSRF validation
│   │   ├── config.py                  # YAML config loaders (hot-reload)
│   │   ├── defaults.py                # Smart-defaults priority table
│   │   ├── providers/                 # Provider subpackage
│   │   │   ├── __init__.py            # Public re-exports
│   │   │   ├── base.py                # SearchProvider, SearchResult
│   │   │   ├── adapters.py            # 6 adapters (Ddg/Tavily/Brave/LlmSearch/Searxng/JsonApi) + ADAPTER_MAP
│   │   │   └── registry.py            # ProviderRegistry with mtime reload
│   │   ├── llm_search_formats.py      # Strategy pattern for LLM search API formats
│   │   ├── routing.py                 # Priority-group routing, hedging, circuit breaker
│   │   ├── quality_gate.py            # 3-tier quality gate (answer/URLs/keywords)
│   │   ├── fetch.py                   # SPA detection, JS renderer dispatch
│   │   ├── logging.py                 # Centralized logging (stderr + optional file)
│   │   └── quota.py                   # Per-provider quota tracking, filelock (cross-platform)
│   └── skills/pivot-web-search/       # Skill definition for Claude Code
├── integrations/deepseek-harness/     # Installable external DSH Profile Bundle
├── tests/                             # 362 tests (355 offline + 7 integration)
├── pyproject.toml                     # uv workspace shell — dev deps + lint/test config
├── ARCHITECTURE.md                    # This file
└── docs/                              # Design documents (not tracked in git)
```

## Key Design Principles

1. **Zero mandatory external deps for search** — DDG works with no API key
2. **Priority-group routing** — same-priority providers hedged, first quality-gate pass wins
3. **Config-driven** — providers, proxies, and fetch behavior all via YAML, hot-reloadable
4. **Quota-aware** — binary exclusion (exhausted = skip), no implicit throttling
5. **Per-provider timeouts** — no global budget, each provider gets its configured deadline
6. **Smart defaults** — quality-first ordering (LLM Search > Tavily/Brave/Gemini > SearXNG/json_api > DDG) when no explicit priority
7. **3-tier quality gate** — AI answer presence, URL count, keyword overlap drives failover
8. **Fixed 60s circuit breaker** — 3 consecutive failures opens, no exponential backoff
9. **Provider affinity** — deep research providers excluded from normal routing unless explicitly requested
10. **Security by default** — SSRF protection, pre-redirect blocking, credential redaction
11. **Cross-platform** — filelock instead of fcntl, works on Windows/macOS/Linux
12. **Async-safe** — single-threaded asyncio, no thread locks; config loaders use mtime caching (`cache_still_valid`)
