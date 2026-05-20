"""Tests for Brave LLM Context API and Tavily Extract helpers."""

import json
from unittest.mock import AsyncMock, patch

import httpx

from pivot_web_search_mcp.backends import search_brave_llm_context
from pivot_web_search_mcp.extraction import extract_tavily


class TestExtractTavily:
    async def test_no_api_key(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        result = await extract_tavily(["https://example.com"])
        assert result["results"] == []
        assert len(result["failed_results"]) == 1
        assert "no TAVILY_API_KEY" in result["failed_results"][0]["error"]

    @patch("pivot_web_search_mcp.extraction._open_with_fallback", new_callable=AsyncMock)
    async def test_success(self, mock_open, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test123")
        response_data = {
            "results": [
                {"url": "https://example.com", "raw_content": "# Page Title\n\nContent here"}
            ],
            "failed_results": [],
        }
        mock_open.return_value = httpx.Response(200, json=response_data)

        result = await extract_tavily(["https://example.com"], extract_depth="advanced")
        assert len(result["results"]) == 1
        assert result["results"][0]["raw_content"] == "# Page Title\n\nContent here"
        assert result["failed_results"] == []

    @patch("pivot_web_search_mcp.extraction._open_with_fallback", new_callable=AsyncMock)
    async def test_with_query_and_chunks(self, mock_open, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test123")
        response_data = {"results": [{"url": "https://example.com", "raw_content": "chunk1 [...] chunk2"}],
                         "failed_results": []}
        mock_open.return_value = httpx.Response(200, json=response_data)

        result = await extract_tavily(
            ["https://example.com"], query="python", chunks_per_source=3
        )
        assert len(result["results"]) == 1

        # Verify the request payload
        call_kwargs = mock_open.call_args
        data_arg = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")
        payload = json.loads(data_arg)
        assert payload["query"] == "python"
        assert payload["chunks_per_source"] == 3

    @patch("pivot_web_search_mcp.extraction._open_with_fallback", new_callable=AsyncMock)
    async def test_partial_failure(self, mock_open, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        response_data = {
            "results": [{"url": "https://a.com", "raw_content": "content A"}],
            "failed_results": [{"url": "https://b.com", "error": "timeout"}],
        }
        mock_open.return_value = httpx.Response(200, json=response_data)

        result = await extract_tavily(["https://a.com", "https://b.com"])
        assert len(result["results"]) == 1
        assert len(result["failed_results"]) == 1
        assert result["failed_results"][0]["error"] == "timeout"

    @patch("pivot_web_search_mcp.extraction._open_with_fallback", new_callable=AsyncMock)
    async def test_network_error(self, mock_open, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_open.side_effect = Exception("Connection refused")
        result = await extract_tavily(["https://example.com"])
        assert result["results"] == []
        assert "Connection refused" in result["failed_results"][0]["error"]


class TestSearchBraveLlmContext:
    async def test_no_api_key(self, monkeypatch):
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        result = await search_brave_llm_context("test query")
        assert result is None

    @patch("pivot_web_search_mcp.backends._open_with_fallback", new_callable=AsyncMock)
    async def test_success(self, mock_open, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "BSA-test123")
        response_data = {
            "grounding": {
                "generic": [
                    {
                        "url": "https://example.com/page1",
                        "title": "Page One",
                        "snippets": ["First snippet about the topic.", "Second relevant passage."],
                    },
                    {
                        "url": "https://example.com/page2",
                        "title": "Page Two",
                        "snippets": ["Another snippet here."],
                    },
                ]
            },
            "sources": {
                "https://example.com/page1": {"title": "Page One", "hostname": "example.com"},
                "https://example.com/page2": {"title": "Page Two", "hostname": "example.com"},
            },
        }
        mock_open.return_value = httpx.Response(
            200, json=response_data,
            headers={"Content-Type": "application/json"},
        )

        result = await search_brave_llm_context("test query", max_results=5)
        assert result is not None
        results, headers = result
        assert len(results) == 2
        assert results[0]["title"] == "Page One"
        assert results[0]["url"] == "https://example.com/page1"
        assert len(results[0]["snippets"]) == 2
        assert "First snippet" in results[0]["snippet"]

    @patch("pivot_web_search_mcp.backends._open_with_fallback", new_callable=AsyncMock)
    async def test_empty_results(self, mock_open, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "BSA-test")
        response_data = {"grounding": {"generic": []}, "sources": {}}
        mock_open.return_value = httpx.Response(200, json=response_data)

        result = await search_brave_llm_context("obscure query")
        assert result is None

    @patch("pivot_web_search_mcp.backends._open_with_fallback", new_callable=AsyncMock)
    async def test_network_error(self, mock_open, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "BSA-test")
        mock_open.side_effect = Exception("timeout")
        result = await search_brave_llm_context("test")
        assert result is None

    @patch("pivot_web_search_mcp.backends._open_with_fallback", new_callable=AsyncMock)
    async def test_params_passed_correctly(self, mock_open, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "BSA-test")
        response_data = {"grounding": {"generic": [
            {"url": "https://x.com", "title": "X", "snippets": ["text"]}
        ]}, "sources": {}}
        mock_open.return_value = httpx.Response(200, json=response_data)

        await search_brave_llm_context(
            "query", max_results=10, max_tokens=4096,
            context_threshold="strict", freshness="pw",
        )

        # _open_with_fallback is called with (method, url, *, headers, data, timeout)
        call_args = mock_open.call_args
        url = call_args[0][1]  # second positional arg is the URL
        assert "maximum_number_of_tokens=4096" in url
        assert "context_threshold_mode=strict" in url
        assert "freshness=pw" in url
        assert "count=10" in url
