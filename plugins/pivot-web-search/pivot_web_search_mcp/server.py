#!/usr/bin/env python3
"""MCP tools exposed by the Pivot Web Search Claude Code plugin."""

import json

from fastmcp import FastMCP

from .config_service import ConfigService, ConfigServiceError
from .fetch_service import FetchRequest, FetchService, FetchServiceError
from .logging import log
from .presentation import format_fetch_markdown, format_search_markdown
from .providers import ProviderRegistry
from .routing import CircuitBreaker
from .search_service import SearchRequest, SearchService, SearchServiceError


def _build_instructions():
    ordered = [provider.name for provider in _registry.get_ordered()]
    chain = " → ".join(ordered) if ordered else "none configured"
    return (
        f"Web search with {len(ordered)}-provider failover ({chain}). "
        "The server auto-selects providers, retries on poor results, and manages quota. "
        "Use WebSearch for queries, WebFetch to extract full page content from URLs. "
        "For critical queries needing maximum coverage, set super_mode=true. "
        "Always cite the URLs from the Sources section in your response."
    )


_registry = ProviderRegistry()
_registry.load()
_enabled = [provider for provider in _registry.get_ordered() if provider.enabled]
log(
    f"Pivot Web Search loaded: {len(_enabled)} providers enabled "
    f"({', '.join(provider.name for provider in _enabled) if _enabled else 'none'})"
)

mcp = FastMCP("pivot-web-search", instructions=_build_instructions())
_breaker = CircuitBreaker()
_service = SearchService(_registry, _breaker)
_fetch_service = FetchService()
_config_service = ConfigService(_registry, _breaker)


def _build_failure_suggestions(failures):
    suggestions = []
    error_texts = " ".join(f"{failure.get('error', '')} {failure.get('state', '')}" for failure in failures).lower()
    if "api key" in error_texts or "no tavily" in error_texts or "no brave" in error_texts:
        suggestions.append("Configure API keys via plugin settings (Tavily, Brave, or Gemini)")
    if "timeout" in error_texts or "connection" in error_texts or "tcp_failure" in error_texts:
        suggestions.append("Check network connectivity or configure proxies in config/proxies.yaml")
    if "rate" in error_texts or "429" in error_texts or "quota" in error_texts or "circuit_open" in error_texts:
        suggestions.append("Provider rate-limited or in cooldown — wait and retry, or switch providers")
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
    """Search the web with quota-aware provider failover and smart defaults.

    Args:
        query: The search query
        allowed_domains: Only include results from these domains
        blocked_domains: Exclude results from these domains
        max_results: Maximum number of results (1-10, or 1-20 for super mode)
        provider: Usually unnecessary. Force a configured provider by name
        super_mode: Query all providers in parallel for maximum coverage
        news: Search news instead of the general web
        timelimit: Time filter (d=day, w=week, m=month, y=year)
        include_answer: Include an AI-generated answer when supported
        search_depth: Provider search depth (basic or advanced)
        topic: Provider topic (general or news)
        days: Limit news to the most recent N days
        include_content: Return extracted page content with results
        max_content_tokens: Token budget for included content
    """
    try:
        response = await _service.search(
            SearchRequest(
                query=query,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                max_results=max_results,
                provider=provider,
                mode="super" if super_mode else "normal",
                news=news,
                timelimit=timelimit,
                include_answer=include_answer,
                search_depth=search_depth,
                topic=topic,
                days=days,
                include_content=include_content,
                max_content_tokens=max_content_tokens,
            )
        )
        return format_search_markdown(response)
    except SearchServiceError as error:
        if error.code == "INVALID_REQUEST":
            return json.dumps({"error": str(error), "query": query})
        return json.dumps(
            {
                "error": str(error),
                "query": query,
                "provider_errors": error.failures,
                "suggestions": _build_failure_suggestions(error.failures),
            },
            indent=2,
        )
    except Exception as error:
        return json.dumps({"error": str(error), "query": query})


@mcp.tool
async def WebFetch(
    url: str | list[str],
    query: str | None = None,
    max_chars: int | None = None,
) -> str:
    """Fetch and extract content from one URL or a batch of URLs.

    Args:
        url: HTTP(S) URL or list of URLs
        query: Optional relevance query for configured fallback renderers
        max_chars: Override the configured content truncation limit
    """
    urls = url if isinstance(url, list) else [url]
    try:
        response = await _fetch_service.fetch(FetchRequest(urls=urls, query=query, max_chars=max_chars))
        rendered = format_fetch_markdown(response)
        if rendered:
            return rendered
        item = response.items[0]
        return json.dumps({"error": item.error, "url": item.url})
    except FetchServiceError as error:
        return json.dumps({"error": str(error), "url": urls[0] if urls else ""})
    except Exception as error:
        return json.dumps({"error": str(error), "url": urls[0] if urls else ""})


@mcp.tool
async def WebSearchConfig(action: str = "status") -> str:
    """Inspect or hot-reload Pivot Web Search plugin configuration.

    Args:
        action: status or reload
    """
    try:
        return json.dumps(await _config_service.execute(action), indent=2)
    except ConfigServiceError as error:
        return json.dumps({"error": str(error), "action": action})


if __name__ == "__main__":
    mcp.run(transport="stdio")
