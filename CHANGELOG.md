# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
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
- 32 new tests for fetch, extract, and LLM context features

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
