"""Integration tests — require real API keys and network access.

Run with: pytest -m integration
Skip with: pytest -m "not integration"
"""

import os

import pytest

from pivot_web_search_mcp import search

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _skip_without_network():
    """Skip all integration tests if no API keys are set and DDG is unreachable."""
    import socket
    has_api_key = any(
        os.environ.get(k)
        for k in ("TAVILY_API_KEY", "BRAVE_API_KEY", "GEMINI_SEARCH_API_KEY")
    )
    if has_api_key:
        return
    try:
        socket.create_connection(("duckduckgo.com", 443), timeout=3)
    except (socket.timeout, OSError):
        pytest.skip("No API keys set and network unreachable")


class TestDdgLive:
    def test_basic_search(self):
        results = search.search_ddg("python programming", max_results=3)
        assert results is not None
        assert len(results) >= 1
        assert results[0].get("url")

    def test_news_search(self):
        results = search.search_ddg("technology", max_results=3, news=True)
        if results:
            assert len(results) >= 1


class TestTavilyLive:
    @pytest.fixture(autouse=True)
    def _require_key(self):
        if not os.environ.get("TAVILY_API_KEY"):
            pytest.skip("TAVILY_API_KEY not set")

    def test_basic_search(self):
        rv = search.search_tavily("python programming", max_results=3)
        assert rv is not None
        results, answer = rv
        assert len(results) >= 1

    def test_with_answer(self):
        rv = search.search_tavily("What is Python?", max_results=3, include_answer=True)
        assert rv is not None
        results, answer = rv
        assert answer


class TestBraveLive:
    @pytest.fixture(autouse=True)
    def _require_key(self):
        if not os.environ.get("BRAVE_API_KEY"):
            pytest.skip("BRAVE_API_KEY not set")

    def test_basic_search(self):
        rv = search.search_brave("python programming", max_results=3)
        assert rv is not None
        results, headers = rv
        assert len(results) >= 1

    def test_returns_rate_limit_headers(self):
        rv = search.search_brave("test query", max_results=1)
        assert rv is not None
        results, headers = rv
        assert "X-RateLimit-Remaining" in headers or "x-ratelimit-remaining" in headers


class TestExtractLive:
    def test_stable_url(self):
        result = search.extract_trafilatura(["https://httpbin.org/html"])
        assert result["results"]
        assert len(result["results"][0]["raw_content"]) > 0
