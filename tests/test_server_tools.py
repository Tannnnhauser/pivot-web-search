"""MCP tool function tests — WebSearch, WebFetch, WebSearchConfig with mocked providers."""

import json
from unittest.mock import AsyncMock, patch

from pivot_web_search_mcp import server
from pivot_web_search_mcp.providers import SearchResult


class TestWebSearch:
    @patch.object(server, '_search_with_registry', new_callable=AsyncMock)
    async def test_normal_output(self, mock_search):
        mock_search.return_value = SearchResult(
            results=[{"title": "Example", "url": "https://example.com", "snippet": "A test result"}],
            provider="ddg",
        )
        result = await server.WebSearch("test query")
        assert "Example" in result
        assert "Sources:" in result
        assert "https://example.com" in result

    @patch.object(server, '_search_with_registry', new_callable=AsyncMock)
    async def test_all_providers_fail(self, mock_search):
        mock_search.return_value = None
        result = await server.WebSearch("failing query")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "All providers failed" in parsed["error"]

    @patch.object(server, '_search_with_registry', new_callable=AsyncMock)
    async def test_domain_filtering_applied(self, mock_search):
        mock_search.return_value = SearchResult(
            results=[
                {"title": "Good", "url": "https://good.com/page", "snippet": "ok"},
            ],
            provider="tavily",
        )
        result = await server.WebSearch("test", allowed_domains=["good.com"])
        assert "Good" in result
        assert "good.com" in result

    @patch.object(server, '_search_super_with_registry', new_callable=AsyncMock)
    async def test_super_mode(self, mock_super):
        mock_super.return_value = SearchResult(
            results=[{"title": "Multi", "url": "https://m.com", "snippet": "x"}],
            provider="ddg,tavily",
        )
        result = await server.WebSearch("test", super_mode=True)
        assert "Multi" in result

    async def test_empty_query_error(self):
        result = await server.WebSearch("")
        parsed = json.loads(result)
        assert "error" in parsed

    async def test_whitespace_query_error(self):
        result = await server.WebSearch("   ")
        parsed = json.loads(result)
        assert "error" in parsed

    @patch("pivot_web_search_mcp.search.search_brave_llm_context", new_callable=AsyncMock)
    async def test_include_content_mode(self, mock_llm):
        mock_llm.return_value = (
            [{"title": "Page", "url": "https://p.com", "snippet": "text", "snippets": ["chunk1"]}],
            {},
        )
        result = await server.WebSearch("test", include_content=True)
        assert "chunk1" in result
        assert "Page" in result


class TestWebFetch:
    @patch("pivot_web_search_mcp.fetch.render_with_fallback", new_callable=AsyncMock)
    @patch("pivot_web_search_mcp.search.extract_trafilatura", new_callable=AsyncMock)
    async def test_success(self, mock_extract, mock_fallback):
        mock_extract.return_value = {
            "results": [{"url": "https://example.com", "raw_content": "# Content here"}],
            "failed_results": [],
        }
        mock_fallback.return_value = None
        result = await server.WebFetch("https://example.com", "extract main content")
        assert "Content here" in result
        assert "example.com" in result

    @patch("pivot_web_search_mcp.fetch.render_with_fallback", new_callable=AsyncMock)
    @patch("pivot_web_search_mcp.search.extract_trafilatura", new_callable=AsyncMock)
    async def test_extraction_failure(self, mock_extract, mock_fallback):
        mock_extract.return_value = {
            "results": [],
            "failed_results": [{"url": "https://bad.com", "error": "timeout"}],
        }
        mock_fallback.return_value = None
        result = await server.WebFetch("https://bad.com", "test")
        parsed = json.loads(result)
        assert "error" in parsed

    async def test_empty_url(self):
        result = await server.WebFetch("", "test")
        parsed = json.loads(result)
        assert "error" in parsed

    @patch("pivot_web_search_mcp.fetch.render_with_fallback", new_callable=AsyncMock)
    @patch("pivot_web_search_mcp.search.extract_trafilatura", new_callable=AsyncMock)
    async def test_batch_mode(self, mock_extract, mock_fallback):
        mock_extract.return_value = {
            "results": [
                {"url": "https://a.com", "raw_content": "Content A"},
                {"url": "https://b.com", "raw_content": "Content B"},
            ],
            "failed_results": [],
        }
        mock_fallback.return_value = None
        result = await server.WebFetch(["https://a.com", "https://b.com"], "extract")
        assert "Content A" in result
        assert "Content B" in result
        assert "2/2" in result

    @patch("pivot_web_search_mcp.fetch.render_with_fallback", new_callable=AsyncMock)
    @patch("pivot_web_search_mcp.search.extract_trafilatura", new_callable=AsyncMock)
    async def test_max_chars_truncation(self, mock_extract, mock_fallback):
        long_content = "x" * 500
        mock_extract.return_value = {
            "results": [{"url": "https://example.com", "raw_content": long_content}],
            "failed_results": [],
        }
        mock_fallback.return_value = None
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
        assert sources["providers"]["source"] in ("env", "yaml", "default")


class TestStructuredErrors:
    @patch.object(server, '_search_with_registry', new_callable=AsyncMock)
    async def test_failure_info_includes_provider_errors(self, mock_search):
        mock_search.return_value = server._FailureInfo(
            failures=[
                {"provider": "ddg", "error": "timeout"},
                {"provider": "tavily", "error": "no api key"},
            ]
        )
        result = await server.WebSearch("test query")
        parsed = json.loads(result)
        assert parsed["error"] == "All providers failed"
        assert len(parsed["provider_errors"]) == 2
        assert parsed["provider_errors"][0]["provider"] == "ddg"
        assert "suggestions" in parsed
        assert len(parsed["suggestions"]) > 0

    @patch.object(server, '_search_with_registry', new_callable=AsyncMock)
    async def test_failure_suggestions_api_key(self, mock_search):
        mock_search.return_value = server._FailureInfo(
            failures=[{"provider": "tavily", "error": "no api key configured"}]
        )
        result = await server.WebSearch("test")
        parsed = json.loads(result)
        assert any("API keys" in s for s in parsed["suggestions"])

    @patch.object(server, '_search_with_registry', new_callable=AsyncMock)
    async def test_failure_suggestions_timeout(self, mock_search):
        mock_search.return_value = server._FailureInfo(
            failures=[{"provider": "ddg", "error": "connection timeout"}]
        )
        result = await server.WebSearch("test")
        parsed = json.loads(result)
        assert any("network" in s.lower() or "connectivity" in s.lower() for s in parsed["suggestions"])
