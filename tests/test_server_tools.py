"""MCP tool function tests — WebSearch, WebFetch, WebSearchConfig with mocked providers."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

from pivot_web_search_mcp import server
from pivot_web_search_mcp.fetch_service import FetchItem, FetchResponse
from pivot_web_search_mcp.providers import SearchProvider, SearchResult
from pivot_web_search_mcp.routing import execute_super_search
from pivot_web_search_mcp.search_service import SearchResponse, SearchServiceError, apply_smart_defaults


class TestWebSearch:
    @patch.object(server._service, "search", new_callable=AsyncMock)
    async def test_normal_output(self, mock_search):
        mock_search.return_value = SearchResponse(
            query="test query",
            results=[{"title": "Example", "url": "https://example.com", "snippet": "A test result"}],
            provider="ddg",
        )
        result = await server.WebSearch("test query")
        assert "Example" in result
        assert "Sources:" in result
        assert "https://example.com" in result

    @patch.object(server._service, "search", new_callable=AsyncMock)
    async def test_all_providers_fail(self, mock_search):
        mock_search.side_effect = SearchServiceError("SEARCH_FAILED", "All providers failed")
        result = await server.WebSearch("failing query")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "All providers failed" in parsed["error"]

    @patch.object(server._service, "search", new_callable=AsyncMock)
    async def test_domain_filtering_applied(self, mock_search):
        mock_search.return_value = SearchResponse(
            query="test",
            results=[
                {"title": "Good", "url": "https://good.com/page", "snippet": "ok"},
            ],
            provider="tavily",
        )
        result = await server.WebSearch("test", allowed_domains=["good.com"])
        assert "Good" in result
        assert "good.com" in result
        assert mock_search.await_args.args[0].allowed_domains == ["good.com"]

    @patch.object(server._service, "search", new_callable=AsyncMock)
    async def test_super_mode(self, mock_super):
        mock_super.return_value = SearchResponse(
            query="test",
            results=[{"title": "Multi", "url": "https://m.com", "snippet": "x"}],
            provider="ddg,tavily",
        )
        result = await server.WebSearch("test", super_mode=True)
        assert "Multi" in result
        assert mock_super.await_args.args[0].mode == "super"

    async def test_empty_query_error(self):
        result = await server.WebSearch("")
        parsed = json.loads(result)
        assert "error" in parsed

    async def test_whitespace_query_error(self):
        result = await server.WebSearch("   ")
        parsed = json.loads(result)
        assert "error" in parsed

    @patch.object(server._service, "search", new_callable=AsyncMock)
    async def test_include_content_mode(self, mock_search):
        mock_search.return_value = SearchResponse(
            query="test",
            results=[{"title": "Page", "url": "https://p.com", "snippet": "text", "snippets": ["chunk1"]}],
            provider="brave-llm-context",
            content_included=True,
        )
        result = await server.WebSearch("test", include_content=True)
        assert "chunk1" in result
        assert "Page" in result


class TestWebFetch:
    @patch.object(server._fetch_service, "fetch", new_callable=AsyncMock)
    async def test_success(self, mock_fetch):
        mock_fetch.return_value = FetchResponse([FetchItem(url="https://example.com", content="# Content here")])
        result = await server.WebFetch("https://example.com", "extract main content")
        assert "Content here" in result
        assert "example.com" in result
        assert mock_fetch.await_args.args[0].query == "extract main content"

    @patch.object(server._fetch_service, "fetch", new_callable=AsyncMock)
    async def test_extraction_failure(self, mock_fetch):
        mock_fetch.return_value = FetchResponse([FetchItem(url="https://bad.com", error="timeout")])
        result = await server.WebFetch("https://bad.com", "test")
        parsed = json.loads(result)
        assert parsed == {"error": "timeout", "url": "https://bad.com"}

    async def test_empty_url(self):
        result = await server.WebFetch("", "test")
        parsed = json.loads(result)
        assert "error" in parsed

    @patch.object(server._fetch_service, "fetch", new_callable=AsyncMock)
    async def test_batch_mode(self, mock_fetch):
        mock_fetch.return_value = FetchResponse(
            [
                FetchItem(url="https://a.com", content="Content A"),
                FetchItem(url="https://b.com", content="Content B"),
            ]
        )
        result = await server.WebFetch(["https://a.com", "https://b.com"], "extract")
        assert "Content A" in result
        assert "Content B" in result
        assert "2/2" in result

    @patch.object(server._fetch_service, "fetch", new_callable=AsyncMock)
    async def test_batch_mode_omits_invalid_urls_from_output_and_denominator(self, mock_fetch):
        mock_fetch.return_value = FetchResponse(
            [
                FetchItem(url="https://a.com", content="Content A"),
                FetchItem(url="not-a-url", error="unsupported URL", invalid=True),
                FetchItem(url="https://b.com", content="Content B"),
            ]
        )
        result = await server.WebFetch(["https://a.com", "not-a-url", "https://b.com"], "extract")
        assert "Content A" in result
        assert "Content B" in result
        # Validation-failed URL is omitted entirely and excluded from the denominator.
        assert "not-a-url" not in result
        assert "2/2" in result

    @patch.object(server._fetch_service, "fetch", new_callable=AsyncMock)
    async def test_max_chars_truncation(self, mock_fetch):
        mock_fetch.return_value = FetchResponse(
            [FetchItem(url="https://example.com", content="x" * 100, truncated=True)]
        )
        result = await server.WebFetch("https://example.com", "test", max_chars=100)
        assert "[Content truncated" in result


class TestWebSearchConfig:
    async def test_status_action(self):
        result = await server.WebSearchConfig("status")
        parsed = json.loads(result)
        assert parsed["action"] == "status"
        assert "providers" in parsed
        assert "proxies" in parsed

    async def test_reload_action(self):
        result = await server.WebSearchConfig("reload")
        parsed = json.loads(result)
        assert parsed["action"] == "reload"
        assert "providers_loaded" in parsed

    async def test_status_includes_config_sources(self):
        result = await server.WebSearchConfig("status")
        parsed = json.loads(result)
        assert "config_sources" in parsed
        sources = parsed["config_sources"]
        assert "providers" in sources
        assert "proxies" in sources
        assert "fetch" in sources
        assert "source" in sources["providers"]
        assert sources["providers"]["source"] in ("yaml", "auto-detect")


class TestStructuredErrors:
    @patch.object(server._service, "search", new_callable=AsyncMock)
    async def test_failure_info_includes_provider_errors(self, mock_search):
        mock_search.side_effect = SearchServiceError(
            "SEARCH_FAILED",
            "All providers failed",
            failures=[
                {"provider": "ddg", "error": "timeout"},
                {"provider": "tavily", "error": "no api key"},
            ],
        )
        result = await server.WebSearch("test query")
        parsed = json.loads(result)
        assert parsed["error"] == "All providers failed"
        assert len(parsed["provider_errors"]) == 2
        assert parsed["provider_errors"][0]["provider"] == "ddg"
        assert "suggestions" in parsed
        assert len(parsed["suggestions"]) > 0

    @patch.object(server._service, "search", new_callable=AsyncMock)
    async def test_failure_suggestions_api_key(self, mock_search):
        mock_search.side_effect = SearchServiceError(
            "SEARCH_FAILED",
            "All providers failed",
            failures=[{"provider": "tavily", "error": "no api key configured"}],
        )
        result = await server.WebSearch("test")
        parsed = json.loads(result)
        assert any("API keys" in s for s in parsed["suggestions"])

    @patch.object(server._service, "search", new_callable=AsyncMock)
    async def test_failure_suggestions_timeout(self, mock_search):
        mock_search.side_effect = SearchServiceError(
            "SEARCH_FAILED",
            "All providers failed",
            failures=[{"provider": "ddg", "error": "connection timeout"}],
        )
        result = await server.WebSearch("test")
        parsed = json.loads(result)
        assert any("network" in s.lower() or "connectivity" in s.lower() for s in parsed["suggestions"])


class _SuperFakeProvider(SearchProvider):
    """Configurable provider for super-mode tests."""

    provider_type = "fake"

    def __init__(self, name, results=None, raise_exc=None, delay=0.0, timeout=5.0, priority=10):
        super().__init__(name, priority, True, {"timeout": timeout})
        self._results = results
        self._raise_exc = raise_exc
        self._delay = delay

    async def search(self, query, max_results=5, **kwargs):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise_exc:
            raise self._raise_exc
        if self._results is None:
            return None
        return SearchResult(results=self._results[:max_results], provider=self.name)

    async def health_check(self):
        return True, None


def _mk_results(n, prefix="r"):
    return [{"title": f"{prefix}{i}", "url": f"https://{prefix}{i}.example.com", "snippet": f"s{i}"} for i in range(n)]


class TestSuperMode:
    async def test_super_mode_runs_providers_in_parallel(self):
        # Two providers each delayed 200ms — sequential would be ~400ms,
        # parallel should finish closer to 200ms. Ceiling 350ms catches
        # accidental serialization (e.g. awaiting one before launching the next).
        providers = [
            _SuperFakeProvider("p1", _mk_results(2, prefix="p1_"), delay=0.2),
            _SuperFakeProvider("p2", _mk_results(2, prefix="p2_"), delay=0.2),
        ]
        start = time.monotonic()
        sr = await execute_super_search("q", 5, providers, server._breaker)
        elapsed = time.monotonic() - start
        assert sr is not None
        assert elapsed < 0.35, f"super mode appears serial: took {elapsed:.3f}s for two 0.2s providers"

    async def test_super_mode_isolates_provider_exceptions(self):
        providers = [
            _SuperFakeProvider("bad", raise_exc=RuntimeError("boom")),
            _SuperFakeProvider("good1", _mk_results(2, prefix="g")),
            _SuperFakeProvider("good2", _mk_results(2, prefix="h")),
        ]
        sr = await execute_super_search("q", 5, providers, server._breaker)
        assert sr is not None
        assert sr.results
        urls = [r["url"] for r in sr.results]
        assert any("g" in u or "h" in u for u in urls)

    async def test_super_mode_per_provider_timeout(self):
        # Provider A would block for 5s if not timed out, but its configured
        # timeout is 0.2s. Provider B returns immediately. Total wall time must
        # stay near A's timeout window — never approach A's 5s delay — proving
        # one slow provider cannot stall the others.
        providers = [
            _SuperFakeProvider("slow", _mk_results(2, prefix="slow"), delay=5.0, timeout=0.2),
            _SuperFakeProvider("fast", _mk_results(2, prefix="fast"), timeout=2.0),
        ]
        start = time.monotonic()
        sr = await execute_super_search("q", 5, providers, server._breaker)
        elapsed = time.monotonic() - start

        assert sr is not None
        urls = " ".join(r["url"] for r in sr.results)
        assert "fast" in urls, "fast provider's results should be returned"
        assert "slow" not in urls, "timed-out provider's results must not leak in"
        # Hard ceiling well below A's 5s delay: timeout (0.2s) + slack for scheduling.
        assert elapsed < 1.0, f"slow provider blocked the gather: took {elapsed:.3f}s"

    async def test_super_mode_records_breaker_state(self):
        providers = [
            _SuperFakeProvider("crash", raise_exc=RuntimeError("boom")),
            _SuperFakeProvider("ok", _mk_results(1, prefix="o")),
        ]
        with patch.object(server._breaker, "record_failure") as mock_fail:
            await execute_super_search("q", 5, providers, server._breaker)
            failed_names = [c.args[0] for c in mock_fail.call_args_list]
            assert "crash" in failed_names


class TestSmartDefaults:
    def test_english_time_sensitive_sets_timelimit(self):
        out = apply_smart_defaults("latest news on AI", {"timelimit": None, "news": None})
        assert out["timelimit"] == "m"

    def test_cjk_time_sensitive_sets_timelimit(self):
        out = apply_smart_defaults("今年的最新新闻", {"timelimit": None, "news": None})
        assert out["timelimit"] == "m"

    def test_news_only_query_sets_news_but_not_timelimit(self):
        # Behavioral: query matches only the news pattern, not the
        # time-sensitive pattern. The function must apply news=True while
        # leaving timelimit=None — proving the two detectors are independent.
        out = apply_smart_defaults("breaking news on quantum mechanics", {"timelimit": None, "news": None})
        assert out["news"] is True
        assert out["timelimit"] is None

    def test_news_mode_detection(self):
        out = apply_smart_defaults("breaking news on election", {"timelimit": None, "news": None})
        assert out["news"] is True

    def test_returns_new_dict_with_modifications_applied(self):
        # Behavioral: the function must (a) return a fresh dict, not the input,
        # and (b) the fresh dict must reflect the smart-defaults transformation.
        # A pass-through stub that returned the input unchanged would fail both.
        kwargs = {"timelimit": None, "news": None}
        out = apply_smart_defaults("latest news on AI", kwargs)
        assert out is not kwargs, "should return a copy, not mutate caller's dict"
        assert out["timelimit"] == "m"
        assert kwargs["timelimit"] is None, "caller's dict must not be mutated"

    def test_explicit_timelimit_preserved_while_news_still_applied(self):
        # Behavioral: with timelimit explicitly set, the function must NOT
        # overwrite it — but it must still apply news=True from the news
        # pattern. A pass-through stub would return news=None and fail.
        out = apply_smart_defaults("latest breaking news", {"timelimit": "d", "news": None})
        assert out["timelimit"] == "d", "explicit timelimit must be preserved"
        assert out["news"] is True, "news default must still be applied"


class TestExplicitProviderFailure:
    @patch.object(server._registry, "get_by_name")
    async def test_explicit_provider_failure_records_breaker(self, mock_get):
        provider = _SuperFakeProvider("tavily", raise_exc=RuntimeError("api down"))
        mock_get.return_value = provider
        with patch.object(server._breaker, "record_failure") as mock_fail:
            result = await server.WebSearch("test", provider="tavily")
            failed_names = [c.args[0] for c in mock_fail.call_args_list]
            assert "tavily" in failed_names
            parsed = json.loads(result)
            assert "error" in parsed
