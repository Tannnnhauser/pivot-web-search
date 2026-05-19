#!/usr/bin/env python3
"""MCP server wrapping the unified Pivot Web Search skill.

Exposes three tools:
  - WebSearch: config-driven failover across all enabled providers, with
    per-provider connection failover through the proxy layer.
  - WebFetch: URL content extraction via trafilatura with Next.js/Nuxt.js SPA fallback,
    same connection failover.
  - WebSearchConfig: runtime config inspection and reload.

Run: python3 server.py  (stdio transport)
"""

import asyncio
import json
import os
import re as _re
import urllib.parse

from fastmcp import FastMCP

from . import quota as _quota
from .backends import search_brave_llm_context
from .config import (
    get_fetch_config_source,
    get_proxy_config_source,
    load_fetch_config,
    load_proxies,
    reload_proxies,
)
from .extraction import extract_trafilatura
from .logging import log
from .providers import ProviderRegistry, SearchResult
from .results import dedup_and_rank, to_markdown
from .routing import CircuitBreaker, FailureInfo, ScoredProvider, attempt_single, _call_counter, execute_search, select_providers
from .validation import MAX_CONTENT_CHARS, validate_url


def _redact_proxy_url(url):
    """Redact user:pass from proxy URL for safe display."""
    if url is None:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        return urllib.parse.urlunparse(parsed._replace(netloc=netloc))
    return url


def _build_instructions():
    ordered = [p.name for p in _registry.get_ordered()]
    chain = " → ".join(ordered) if ordered else "none configured"
    return (
        f"Web search with {len(ordered)}-provider failover ({chain}). "
        "The server auto-selects providers, retries on poor results, and manages quota. "
        "Use WebSearch for queries, WebFetch to extract full page content from URLs. "
        "For critical queries needing maximum coverage, set super_mode=true. "
        "Always cite the URLs from the Sources section in your response."
    )


# Global registry — initialized once, auto-reloads on config change
_registry = ProviderRegistry()
_registry.load()

_enabled = [p for p in _registry.get_ordered() if p.enabled]
log(f"Pivot Web Search loaded: {len(_enabled)} providers enabled "
    f"({', '.join(p.name for p in _enabled) if _enabled else 'none'})")

mcp = FastMCP("pivot-web-search", instructions=_build_instructions())

# Initialize Gemini quota — daily limit (RPD resets at PT midnight)
_gemini_quota_raw = os.environ.get("PIVOT_WEB_SEARCH_GEMINI_QUOTA", "").strip()
_gemini_daily_limit = 500  # Gemini 2.5 Flash free tier: 500 RPD shared with Flash-Lite
if _gemini_quota_raw:
    try:
        _gemini_daily_limit = int(float(_gemini_quota_raw))
    except (ValueError, TypeError):
        pass
_quota.set_provider_limit("gemini", _gemini_daily_limit, period="daily")


def _filter_by_domains(results, allowed_domains, blocked_domains):
    """Post-filter results by domain allow/block lists."""
    if not allowed_domains and not blocked_domains:
        return results
    filtered = []
    for r in results:
        url = r.get("url", "").lower()
        host = ""
        try:
            host = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.")
        except Exception:
            pass
        if allowed_domains:
            if not any(host.endswith(d.lower().removeprefix("www.")) for d in allowed_domains):
                continue
        if blocked_domains:
            if any(host.endswith(d.lower().removeprefix("www.")) for d in blocked_domains):
                continue
        filtered.append(r)
    return filtered


# ---------------------------------------------------------------------------
# Session-scoped circuit breaker (replaces DDG demotion globals)
# ---------------------------------------------------------------------------

_breaker = CircuitBreaker()


async def _search_with_registry(query, max_results, provider_name="auto", **kwargs):
    """Run search through the provider registry with failover.

    Explicit provider: direct attempt with health check, no quality gate.
    Auto mode: priority-group execution with hedging and quality gate.
    Returns SearchResult, FailureInfo (all failed), or None.
    """
    if provider_name and provider_name != "auto":
        p = _registry.get_by_name(provider_name)
        if not p:
            return FailureInfo(failures=[{"provider": provider_name, "error": f"unknown provider '{provider_name}'"}])
        if not p.enabled:
            return FailureInfo(failures=[{"provider": provider_name, "error": "provider is disabled"}])
        ok, detail = await p.health_check()
        if not ok:
            return FailureInfo(failures=[{"provider": provider_name, "error": detail or "health check failed"}])

        scored = ScoredProvider(provider=p, effective_priority=0, call_counter=0, rr_seed=0)
        attempt = await attempt_single(scored, query, max_results, _breaker, **kwargs)
        if attempt.result is not None:
            return attempt.result
        return FailureInfo(failures=[{"provider": p.name, "error": attempt.error or "unknown"}])

    # Auto mode: delegate to routing engine
    affinity = kwargs.pop("affinity", "general")
    return await execute_search(
        query, max_results, _registry.get_ordered(), _breaker,
        affinity=affinity, **kwargs,
    )


async def _search_super_with_registry(query, max_results, **kwargs):
    """Query all eligible providers in parallel with per-provider timeouts.

    Super mode ignores priority ordering — all providers run concurrently.
    Per-provider timeouts are enforced. Results merged via dedup_and_rank.
    """
    affinity = kwargs.pop("affinity", "general")
    candidates = select_providers(_registry.get_ordered(), _breaker, affinity=affinity)

    if not candidates:
        return None

    async def _timed_search(provider):
        try:
            return await asyncio.wait_for(
                provider.search(query, max_results, **kwargs),
                timeout=provider.timeout_seconds,
            )
        except asyncio.TimeoutError:
            _breaker.record_failure(provider.name)
            log(f"super: {provider.name} timed out after {provider.timeout_seconds}s")
            return None
        except Exception as e:
            _breaker.record_failure(provider.name)
            log(f"super: {provider.name} failed: {e}")
            return None

    tasks = [_timed_search(c.provider) for c in candidates]
    search_results = await asyncio.gather(*tasks)

    results_by_provider = {}
    answer = None

    for c, sr in zip(candidates, search_results):
        p = c.provider
        if sr and sr.results:
            results_by_provider[p.name] = sr.results
            _quota.record_usage(p.name)
            _breaker.record_success(p.name)
            _call_counter.increment(p.name)
            log(f"super: {p.name} returned {len(sr.results)} results")
            if sr.answer and not answer:
                answer = sr.answer
        elif sr is None:
            log(f"super: {p.name} returned nothing")

    if not results_by_provider:
        return None

    merged, providers_used = dedup_and_rank(results_by_provider, max_results)
    return SearchResult(
        results=merged,
        provider=",".join(providers_used),
        answer=answer,
    )


_TIME_SENSITIVE_PATTERN = _re.compile(
    r'(?:\b(?:latest|recent|newest|current|202[4-9]|this\s+year)\b|今年|最新|最近)', _re.IGNORECASE)
_NEWS_PATTERN = _re.compile(
    r'(?:\b(?:news|breaking|announced|released|launches?d?)\b|新闻|发布)', _re.IGNORECASE)


def _apply_smart_defaults(query, kwargs):
    """Detect query characteristics and set soft defaults for unset parameters."""
    result = dict(kwargs)
    if result.get("timelimit") is None and _TIME_SENSITIVE_PATTERN.search(query):
        result["timelimit"] = "m"
    if result.get("news") is None and _NEWS_PATTERN.search(query):
        result["news"] = True
    return result


def _format_content_results(results, query):
    """Format Brave LLM Context results (with snippets) into markdown output."""
    parts = ["*Source: brave-llm-context*\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippets = r.get("snippets", [])
        content = "\n\n".join(snippets) if snippets else r.get("snippet", "")
        parts.append(f"{i}. **{title}**\n   {url}\n\n{content}\n")

    parts.append("\nSources:")
    for r in results:
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        parts.append(f"- [{title}]({url})")

    return "\n".join(parts)


def _build_failure_suggestions(failures):
    """Generate actionable suggestions based on failure patterns."""
    suggestions = []
    error_texts = " ".join(f.get("error", "") for f in failures).lower()

    if "api key" in error_texts or "no tavily" in error_texts or "no brave" in error_texts:
        suggestions.append("Configure API keys via plugin settings (Tavily, Brave, or Gemini)")
    if "timeout" in error_texts or "connection" in error_texts:
        suggestions.append("Check network connectivity or configure proxies in config/proxies.yaml")
    if "rate" in error_texts or "429" in error_texts or "quota" in error_texts:
        suggestions.append("Provider rate-limited — wait and retry, or switch providers")
    if not suggestions:
        suggestions.append("Run WebSearchConfig status to check provider health")
    return suggestions


@mcp.tool
async def WebSearch(
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    max_results: int = 5,
    provider: str = "auto",
    super_mode: bool = False,
    news: bool | None = None,
    timelimit: str | None = None,
    include_answer: bool = False,
    search_depth: str = "basic",
    topic: str = "general",
    days: int | None = None,
    include_content: bool = False,
    max_content_tokens: int = 8192,
) -> str:
    """Search the web for information. 4-layer provider failover with automatic retry on poor results.

    The server auto-selects the best provider and manages quota across sessions.
    Time-sensitive queries (containing "latest", "recent", year numbers) automatically
    get a recency filter. News-related queries auto-enable news mode.
    Results include a Sources section with URLs — always cite these in your response.

    In normal mode, providers are tried in quota-aware order until one returns good results.
    In super mode, all providers are queried in parallel for maximum coverage.
    Domain filtering (allowed_domains/blocked_domains) auto-routes through Tavily for native support.

    Args:
        query: The search query
        allowed_domains: Only include results from these domains
        blocked_domains: Exclude results from these domains
        max_results: Maximum number of results (1-10, or 1-20 for super mode)
        provider: Usually unnecessary — server auto-selects. Force: "tavily" for deep research, "gemini" for Google
        super_mode: Queries all providers in parallel. Higher latency but maximum coverage. Use for critical queries
        news: Search news instead of web
        timelimit: Time filter (d=day, w=week, m=month, y=year). Auto-set for time-sensitive queries
        include_answer: Include AI-generated answer summary (Tavily)
        search_depth: Tavily search depth. "advanced" gives more detailed results but costs 2x credits
        topic: Search topic for Tavily (general/news)
        days: Limit news to recent N days (Tavily)
        include_content: Return extracted page content with results (uses Brave LLM Context)
        max_content_tokens: Token budget for content extraction (1024-32768, default 8192)
    """
    if not query or not query.strip():
        return json.dumps({"error": "Empty query", "query": query})

    try:
        include_domains = allowed_domains
        exclude_domains = blocked_domains

        # If domain filters specified, force Tavily (only provider that supports them natively)
        if (include_domains or exclude_domains) and provider == "auto":
            provider = "tavily"

        search_kwargs = {
            "region": "wt-wt", "timelimit": timelimit,
            "news": news, "include_answer": include_answer,
            "search_depth": search_depth, "topic": topic,
            "days": days, "include_domains": include_domains,
            "exclude_domains": exclude_domains,
        }

        search_kwargs = _apply_smart_defaults(query, search_kwargs)

        # include_content mode: try Brave LLM Context first (search + content in one call)
        if include_content and not news:
            freshness_map = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}
            freshness = freshness_map.get(search_kwargs.get("timelimit", ""))
            llm_result = await search_brave_llm_context(
                query, max_results=max_results,
                max_tokens=max_content_tokens,
                freshness=freshness,
            )
            if llm_result:
                results, resp_headers = llm_result
                if resp_headers:
                    _quota.update_from_brave_headers(resp_headers)
                if include_domains or exclude_domains:
                    results = _filter_by_domains(results, include_domains, exclude_domains)
                if results:
                    _breaker.record_success("brave")
                    return _format_content_results(results, query)
            if llm_result is not None:
                _breaker.record_success("brave")
            else:
                _breaker.record_failure("brave")

        sr = None

        if super_mode:
            max_results = max(1, min(max_results, 20))
            search_kwargs["include_answer"] = True
            sr = await _search_super_with_registry(query, max_results, **search_kwargs)
            if sr and (include_domains or exclude_domains):
                sr.results = _filter_by_domains(sr.results, include_domains, exclude_domains)
        else:
            max_results = max(1, min(max_results, 10))
            sr = await _search_with_registry(query, max_results, provider, **search_kwargs)

        if sr is None or isinstance(sr, FailureInfo) or not sr.results:
            failures = sr.failures if isinstance(sr, FailureInfo) else []
            return json.dumps({
                "error": "All providers failed",
                "query": query,
                "provider_errors": failures,
                "suggestions": _build_failure_suggestions(failures),
            }, indent=2)

        return to_markdown(sr.results, query, sr.answer, sr.provider)

    except Exception as e:
        return json.dumps({"error": str(e), "query": query})


@mcp.tool
async def WebFetch(
    url: str | list[str],
    prompt: str,
    query: str | None = None,
    max_chars: int | None = None,
) -> str:
    """Fetch and extract content from a URL. Uses trafilatura with Next.js/Nuxt.js SPA fallback.
    Connection failover: direct -> configured proxies.

    The prompt is returned alongside the extracted content for the model to apply.
    No server-side filtering or summarization is performed — the calling model should
    use the prompt to focus on the relevant parts of the returned content.

    No JavaScript execution. Content truncated at 100K characters.
    Binary content (images, PDFs, zips) is detected and rejected with a descriptive message.

    Args:
        url: The URL to fetch content from (http/https only, auto-upgrades http to https)
        prompt: Passed through alongside content — the server does NOT filter. Apply the prompt yourself
        query: Optional relevance query — when set, JS fallback renderers use it for focused extraction
        max_chars: Override default content truncation limit (default: from config or 100K)
    """
    from . import fetch as _fetch

    # Normalize to list
    urls = url if isinstance(url, list) else [url]
    if not urls or all(not u or not u.strip() for u in urls):
        return json.dumps({"error": "Empty URL"})

    # Validate all URLs
    validated = []
    for u in urls:
        if not u or not u.strip():
            continue
        try:
            validated.append(validate_url(u))
        except ValueError as e:
            if len(urls) == 1:
                return json.dumps({"error": str(e), "url": u})
            validated.append(None)

    fetch_config = load_fetch_config()
    truncation_limit = max_chars if max_chars else fetch_config.get("max_chars", MAX_CONTENT_CHARS)
    empty_threshold = fetch_config.get("empty_threshold", 200)

    try:
        valid_urls = [u for u in validated if u]
        result = await extract_trafilatura(valid_urls)

        extracted = {r["url"]: r["raw_content"] for r in result.get("results", []) if r.get("raw_content")}
        failed_urls = {f["url"] for f in result.get("failed_results", [])}

        # For each URL: check if content is empty and attempt fallback
        final_contents = []
        for u in valid_urls:
            content = extracted.get(u, "")

            if _fetch.is_empty_content(content, threshold=empty_threshold) or u in failed_urls:
                fallback = await _fetch.render_with_fallback(u, fetch_config, query=query)
                if fallback:
                    content = fallback

            if not content:
                final_contents.append((u, None, "extraction returned empty"))
            else:
                if len(content) > truncation_limit:
                    content = content[:truncation_limit] + "\n\n[Content truncated due to length...]"
                final_contents.append((u, content, None))

        # Single URL mode — backward compatible output
        if len(urls) == 1 or (len(valid_urls) == 1 and not isinstance(url, list)):
            u, content, error = final_contents[0] if final_contents else (urls[0], None, "unknown error")
            if error:
                return json.dumps({"error": error, "url": u})
            return f"Source: {u}\nExtraction prompt: {prompt}\n\n---\n\n{content}"

        # Batch mode — structured multi-URL output
        parts = []
        for u, content, error in final_contents:
            if error:
                parts.append(f"## {u}\n\n[Error: {error}]")
            else:
                parts.append(f"## {u}\n\n{content}")

        extracted_count = len([c for _, c, e in final_contents if c])
        header = f"Extraction prompt: {prompt}\nURLs extracted: {extracted_count}/{len(valid_urls)}"
        return f"{header}\n\n---\n\n" + "\n\n---\n\n".join(parts)

    except Exception as e:
        return json.dumps({"error": str(e), "url": urls[0] if urls else ""})


@mcp.tool
async def WebSearchConfig(
    action: str = "status",
) -> str:
    """Manage Pivot Web Search plugin configuration at runtime.

    Actions:
      - status: Show current providers and proxies (enabled, priority, health)
      - reload: Re-read config files and apply changes without restart

    Args:
        action: The action to perform (status/reload)
    """
    if action == "reload":
        _registry.reload()
        proxies = reload_proxies()
        providers = _registry.get_all()
        return json.dumps({
            "action": "reload",
            "providers_loaded": len(providers),
            "proxies_loaded": len(proxies),
            "providers_config": _registry.config_source,
        }, indent=2)

    # status
    providers = _registry.get_all()
    quota_summary = _quota.get_quota_summary()
    provider_info = []
    for p in sorted(providers, key=lambda x: x.effective_priority):
        available, detail = await p.health_check()
        info = {
            "name": p.name,
            "type": p.provider_type,
            "priority": p.effective_priority,
            "affinity": p.affinity,
            "timeout": p.timeout_seconds,
            "enabled": p.enabled,
            "available": available,
            "detail": detail,
            "breaker": _breaker.get_status(p.name),
        }
        if p.name in quota_summary:
            info["quota"] = quota_summary[p.name]
        provider_info.append(info)

    proxies = load_proxies()
    proxy_info = [{"url": _redact_proxy_url(p), "label": _redact_proxy_url(p) or "direct"} for p in proxies]

    config_sources = {
        "providers": _registry.get_config_sources(),
        "proxies": get_proxy_config_source(),
        "fetch": get_fetch_config_source(),
    }

    return json.dumps({
        "action": "status",
        "providers_config": _registry.config_source,
        "config_sources": config_sources,
        "providers": provider_info,
        "proxies": proxy_info,
    }, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
