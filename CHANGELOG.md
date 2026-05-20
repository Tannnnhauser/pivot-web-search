# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Fixed
- `LlmSearchProvider.health_check` now resolves the endpoint via the format strategy (`resolve_endpoint`) instead of reading `config["endpoint"]` directly, so Gemini (whose endpoint is derived from `model`) no longer reports a spurious `"no endpoint"` in `WebSearchConfig status`.
- `PIVOT_WEB_SEARCH_PROVIDERS` env-var path no longer assigns sequential `(i+1)*10` priorities — it now defers to the same smart defaults as the YAML path (ddg → 90, tavily/brave/gemini → 20), so plugin users with the default `userConfig.providers` setting actually get Routing v2's hedged execution instead of v1-style linear failover.

### Added
- Routing v2: priority-group failover with hedged execution
  - Same-priority providers fire concurrently with 200ms staggered starts; first quality-gate pass wins
  - Soft total budget (10s, +llm_timeout when LLM provider in play); 2 consecutive `tcp_failure` aborts the walk
  - 3-tier quality gate (ACCEPT/PARTIAL/REJECT) drives failover; best partial kept as fallback
  - `pick_recovery_candidate` probes HALF_OPEN breakers when no eligible candidates remain
- `SearchOutcome` dataclass: discriminated union of `result`/`failure`/`partial` for routing internals
- All-providers-failed responses now surface per-provider state (`disabled`, `affinity_mismatch`, `quota_exhausted` with `retry_after_seconds`, `circuit_open` with `cooldown_remaining_seconds`) plus actionable suggestions
- `include_content` downgrade reason explicitly surfaced in markdown output when Brave LLM Context falls back
- `cache_still_valid()` config helper, reused by providers/proxies/fetch loaders
- `quota.retry_after_seconds()` for surfacing rate-limit windows
- `execute_super_search()` in routing — super mode runs every eligible provider via `asyncio.gather` with per-provider timeouts and exception isolation
- Configurable JS renderer fallback for WebFetch (`config/fetch.yaml`)
  - `playwright`: local headless browser rendering (optional dep: `pip install pivot-web-search-mcp[browser]`)
  - `tavily`: remote extraction via Tavily Extract API (advanced mode handles JS/tables)
- Tavily Extract API integration (`search.extract_tavily()`) with query-aware chunk extraction
- Brave LLM Context API integration (`search.search_brave_llm_context()`) for pre-extracted content
- WebFetch enhancements:
  - Batch URL extraction (pass list of URLs)
  - `query` parameter for relevance-aware extraction
  - `max_chars` parameter for configurable content truncation
  - Auto-fallback to JS renderer when trafilatura returns empty/SPA content
- WebSearch `include_content` mode — uses Brave LLM Context for search+content in one call
- `max_content_tokens` parameter on WebSearch for token budget control
- SPA shell detection (`is_empty_content`) for React, Vue, Next.js, Nuxt app shells
- Hot-reloadable fetch config (`load_fetch_config()`) with mtime-based caching

### Changed
- **Breaking**: `providers.py` split into `providers/` subpackage (`base.py`, `adapters.py`, `registry.py`); module-level constants renamed `_ADAPTER_MAP` → `ADAPTER_MAP`, `_DEFAULT_PROVIDERS` → `DEFAULT_PROVIDERS`
- **Breaking**: `GeminiProvider` class deleted; `gemini` is now an alias of `LlmSearchProvider` with `api_format="gemini"` and `api_key_env_fallback` chain (`GEMINI_SEARCH_API_KEY` → `GOOGLE_STUDIO_API_KEY`)
- LLM auth style now driven by format strategy (`auth_style = "bearer" | "x-goog-api-key"`)
- Tavily `news=True` now correctly translates to `topic="news"` when no explicit topic is set
- `WebSearchConfig reload` action also resets the circuit breaker
- `WebFetch.query` parameter is now optional (defaults to empty)
- `execute_search` refactored into 3 helpers (`_classify_unavailable`, `_select_or_recover`, `_walk_priority_groups`)
- Boundary-only error handling across routing/providers/results — replaced blanket `except Exception` with `httpx.HTTPError`, `ValueError`, `OSError`

### Removed
- `threading.Lock` usage in `CircuitBreaker` (asyncio-only path)
- `_search_super_with_registry` wrapper in server (logic moved to `routing.execute_super_search`)
- Historical design drafts under `docs/` (gpt/, manus/, minimax/, routing-algorithm-codex.md, routing-algorithm-final.md, architecture.md duplicate)

### Tests
- 306 offline + 7 integration = 313 total

## [1.0.0] - 2026-04-28

### Added
- 4-provider failover search (DDG, Tavily, Brave, Gemini)
- Trafilatura-based URL extraction with Next.js/Nuxt.js SPA fallback
- Per-host proxy cache with self-healing
- Super mode (parallel all-provider query with dedup and ranking)
- Server-side intelligence: quota-aware provider scheduling, smart defaults
- Brave response header quota tracking
- Provider registry with hot-reloadable YAML config
- PreToolUse hook blocking built-in WebSearch/WebFetch
- SessionStart health check
- `pyproject.toml` with proper Python packaging
- Apache 2.0 license, CONTRIBUTING.md, SECURITY.md
- GitHub Actions CI (pytest + ruff)
- Comprehensive test suite (73 offline tests)
