"""Structured search orchestration shared by every Pivot interface."""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Literal

from . import quota as _quota
from .backends import search_brave_llm_context
from .providers import ProviderRegistry
from .routing import (
    CircuitBreaker,
    FailureInfo,
    ScoredProvider,
    attempt_single,
    execute_search,
    execute_super_search,
)

SearchMode = Literal["normal", "super"]

_TIME_SENSITIVE_PATTERN = re.compile(
    r"(?:\b(?:latest|recent|newest|current|202[4-9]|this\s+year)\b|今年|最新|最近)",
    re.IGNORECASE,
)
_NEWS_PATTERN = re.compile(
    r"(?:\b(?:news|breaking|announced|released|launches?d?)\b|新闻|发布)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SearchRequest:
    """Complete interface-neutral input for one search operation."""

    query: str
    allowed_domains: list[str] | None = None
    blocked_domains: list[str] | None = None
    max_results: int = 5
    provider: str = "auto"
    mode: SearchMode = "normal"
    news: bool | None = None
    timelimit: str | None = None
    include_answer: bool = False
    search_depth: str = "basic"
    topic: str = "general"
    days: int | None = None
    include_content: bool = False
    max_content_tokens: int = 8192
    region: str = "wt-wt"


@dataclass
class SearchResponse:
    """Structured outcome rendered by MCP, CLI, or an external adapter."""

    query: str
    results: list[dict]
    provider: str
    answer: str | None = None
    content_included: bool = False
    content_downgrade_reason: str | None = None


class SearchServiceError(Exception):
    """Stable business failure raised by the shared search service."""

    def __init__(self, code: str, message: str, *, failures: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.failures = list(failures or [])


def apply_smart_defaults(query: str, kwargs: dict) -> dict:
    """Apply query-derived defaults only when the caller left a value unset."""
    result = dict(kwargs)
    if result.get("timelimit") is None and _TIME_SENSITIVE_PATTERN.search(query):
        result["timelimit"] = "m"
    if result.get("news") is None and _NEWS_PATTERN.search(query):
        result["news"] = True
    return result


def filter_by_domains(results: list[dict], allowed_domains, blocked_domains) -> list[dict]:
    """Post-filter structured results by domain allow/block lists."""
    if not allowed_domains and not blocked_domains:
        return results
    filtered = []
    for result in results:
        host = ""
        try:
            host = (urllib.parse.urlparse(result.get("url", "")).hostname or "").lower().removeprefix("www.")
        except Exception:
            pass
        if allowed_domains and not any(
            host.endswith(domain.lower().removeprefix("www.")) for domain in allowed_domains
        ):
            continue
        if blocked_domains and any(host.endswith(domain.lower().removeprefix("www.")) for domain in blocked_domains):
            continue
        filtered.append(result)
    return filtered


class SearchService:
    """Single authoritative search orchestrator shared by every interface."""

    def __init__(self, registry: ProviderRegistry | None = None, breaker: CircuitBreaker | None = None):
        if registry is None:
            registry = ProviderRegistry()
            registry.load()
        self.registry = registry
        self.breaker = breaker or CircuitBreaker()
        self._configure_quota()

    @staticmethod
    def _configure_quota() -> None:
        raw_limit = os.environ.get("PIVOT_WEB_SEARCH_GEMINI_QUOTA", "").strip()
        daily_limit = 500
        if raw_limit:
            try:
                daily_limit = int(float(raw_limit))
            except (TypeError, ValueError):
                pass
        _quota.set_provider_limit("gemini", daily_limit, period="daily")

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Run one normalized search and return structured data or a business error."""
        if not request.query or not request.query.strip():
            raise SearchServiceError("INVALID_REQUEST", "Empty query")
        if request.mode not in ("normal", "super"):
            raise SearchServiceError("INVALID_REQUEST", f"Unsupported search mode: {request.mode}")

        provider = request.provider
        if (request.allowed_domains or request.blocked_domains) and provider == "auto":
            provider = "tavily"

        search_kwargs = apply_smart_defaults(
            request.query,
            {
                "region": request.region,
                "timelimit": request.timelimit,
                "news": request.news,
                "include_answer": request.include_answer,
                "search_depth": request.search_depth,
                "topic": request.topic,
                "days": request.days,
                "include_domains": request.allowed_domains,
                "exclude_domains": request.blocked_domains,
            },
        )

        content_response, downgrade_reason = await self._include_content(request, search_kwargs)
        if content_response is not None:
            return content_response

        if request.mode == "super":
            max_results = max(1, min(request.max_results, 20))
            search_kwargs["include_answer"] = True
            result = await execute_super_search(
                request.query,
                max_results,
                self.registry.get_ordered(),
                self.breaker,
                **search_kwargs,
            )
            if result and (request.allowed_domains or request.blocked_domains):
                result.results = filter_by_domains(result.results, request.allowed_domains, request.blocked_domains)
        else:
            max_results = max(1, min(request.max_results, 10))
            result = await self._search_normal(request.query, max_results, provider, **search_kwargs)

        if result is None or isinstance(result, FailureInfo) or not result.results:
            failures = result.failures if isinstance(result, FailureInfo) else []
            raise SearchServiceError("SEARCH_FAILED", "All providers failed", failures=failures)

        return SearchResponse(
            query=request.query,
            results=result.results,
            provider=result.provider,
            answer=result.answer,
            content_downgrade_reason=downgrade_reason,
        )

    async def _search_normal(self, query: str, max_results: int, provider_name: str, **kwargs):
        if provider_name and provider_name != "auto":
            provider = self.registry.get_by_name(provider_name)
            if not provider:
                return FailureInfo(
                    failures=[{"provider": provider_name, "error": f"unknown provider '{provider_name}'"}],
                )
            if not provider.enabled:
                return FailureInfo(failures=[{"provider": provider_name, "error": "provider is disabled"}])
            healthy, detail = await provider.health_check()
            if not healthy:
                return FailureInfo(
                    failures=[{"provider": provider_name, "error": detail or "health check failed"}],
                )
            scored = ScoredProvider(provider=provider, effective_priority=0, call_counter=0, rr_seed=0)
            attempt = await attempt_single(scored, query, max_results, self.breaker, **kwargs)
            if attempt.result is not None:
                return attempt.result
            return FailureInfo(failures=[{"provider": provider.name, "error": attempt.error or "unknown"}])

        affinity = kwargs.pop("affinity", "general")
        return await execute_search(
            query,
            max_results,
            self.registry.get_ordered(),
            self.breaker,
            affinity=affinity,
            **kwargs,
        )

    async def _include_content(self, request: SearchRequest, search_kwargs: dict):
        if not request.include_content:
            return None, None
        if search_kwargs.get("news"):
            return None, "include_content not supported in news mode"

        freshness = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}.get(search_kwargs.get("timelimit", ""))
        llm_result = await search_brave_llm_context(
            request.query,
            max_results=request.max_results,
            max_tokens=request.max_content_tokens,
            freshness=freshness,
        )
        if not llm_result:
            self.breaker.record_failure("brave")
            return None, "Brave LLM Context unavailable"

        results, response_headers = llm_result
        if response_headers:
            _quota.update_from_brave_headers(response_headers)
        results = filter_by_domains(results, request.allowed_domains, request.blocked_domains)
        self.breaker.record_success("brave")
        if not results:
            return None, "Brave LLM Context returned no results"
        return SearchResponse(
            query=request.query,
            results=results,
            provider="brave-llm-context",
            content_included=True,
        ), None
