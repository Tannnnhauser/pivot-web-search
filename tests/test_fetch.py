"""Tests for pivot_web_search_mcp.fetch — JS renderer fallback module."""

from unittest.mock import AsyncMock, patch

import pytest

from pivot_web_search_mcp import fetch


class TestIsEmptyContent:
    def test_none_is_empty(self):
        assert fetch.is_empty_content(None) is True

    def test_empty_string_is_empty(self):
        assert fetch.is_empty_content("") is True

    def test_whitespace_is_empty(self):
        assert fetch.is_empty_content("   \n  \t  ") is True

    def test_short_content_is_empty(self):
        assert fetch.is_empty_content("Hello world", threshold=200) is True

    def test_normal_content_not_empty(self):
        content = "This is a substantial article about Python web frameworks. " * 20
        assert fetch.is_empty_content(content) is False

    def test_spa_shell_react_root(self):
        html = '<html><body><div id="root"></div><script src="/main.js"></script></body></html>'
        assert fetch.is_empty_content(html, threshold=10) is True

    def test_spa_shell_vue_app(self):
        html = '<html><body><div id="app"></div></body></html>'
        assert fetch.is_empty_content(html, threshold=10) is True

    def test_spa_shell_next(self):
        html = '<html><body><div id="__next"></div></body></html>'
        assert fetch.is_empty_content(html, threshold=10) is True

    def test_loading_text(self):
        html = "Loading..."
        assert fetch.is_empty_content(html, threshold=5) is True

    def test_enable_javascript(self):
        html = "Please enable JavaScript"
        assert fetch.is_empty_content(html, threshold=5) is True

    def test_real_content_with_div_id(self):
        content = '<div id="root">This is real content with lots of text. ' * 50 + '</div>'
        assert fetch.is_empty_content(content) is False

    def test_custom_threshold(self):
        content = "Short but valid"
        assert fetch.is_empty_content(content, threshold=5) is False
        assert fetch.is_empty_content(content, threshold=100) is True


class TestRenderPlaywright:
    async def test_not_installed_raises(self):
        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def mock_import(name, *args, **kwargs):
            if name == "playwright.async_api":
                raise ImportError("No module named 'playwright'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(RuntimeError, match="Playwright not installed"):
                await fetch.render_playwright("https://example.com")


class TestRenderTavily:
    async def test_no_api_key(self):
        result = await fetch.render_tavily("https://example.com", api_key="")
        assert result is None

    @patch("pivot_web_search_mcp.search.extract_tavily", new_callable=AsyncMock)
    async def test_success(self, mock_extract):
        mock_extract.return_value = {
            "results": [{"url": "https://example.com", "raw_content": "# Hello\n\nExtracted content"}],
            "failed_results": [],
        }
        result = await fetch.render_tavily("https://example.com", api_key="tvly-test123")
        assert result == "# Hello\n\nExtracted content"
        mock_extract.assert_called_once()
        call_kwargs = mock_extract.call_args
        assert call_kwargs[1]["extract_depth"] == "advanced"

    @patch("pivot_web_search_mcp.search.extract_tavily", new_callable=AsyncMock)
    async def test_with_query(self, mock_extract):
        mock_extract.return_value = {
            "results": [{"url": "https://example.com", "raw_content": "Relevant chunk"}],
            "failed_results": [],
        }
        await fetch.render_tavily("https://example.com", api_key="tvly-test", query="python frameworks")
        call_kwargs = mock_extract.call_args
        assert call_kwargs[1]["query"] == "python frameworks"

    @patch("pivot_web_search_mcp.search.extract_tavily", new_callable=AsyncMock)
    async def test_extraction_failed(self, mock_extract):
        mock_extract.return_value = {
            "results": [],
            "failed_results": [{"url": "https://example.com", "error": "timeout"}],
        }
        result = await fetch.render_tavily("https://example.com", api_key="tvly-test")
        assert result is None


class TestRenderWithFallback:
    async def test_none_renderer(self):
        config = {"js_renderer": "none"}
        result = await fetch.render_with_fallback("https://example.com", config)
        assert result is None

    async def test_unknown_renderer(self):
        config = {"js_renderer": "unknown_backend"}
        result = await fetch.render_with_fallback("https://example.com", config)
        assert result is None

    @patch("pivot_web_search_mcp.fetch.render_tavily", new_callable=AsyncMock)
    async def test_tavily_renderer(self, mock_render):
        mock_render.return_value = "Extracted content from Tavily"
        config = {
            "js_renderer": "tavily",
            "tavily": {"extract_depth": "advanced", "format": "markdown", "timeout": 30},
        }
        with patch.dict("os.environ", {"TAVILY_API_KEY": "tvly-testkey"}):
            result = await fetch.render_with_fallback("https://spa-app.com", config)
        assert result == "Extracted content from Tavily"
        mock_render.assert_called_once()

    @patch("pivot_web_search_mcp.fetch.render_playwright", new_callable=AsyncMock)
    async def test_playwright_renderer(self, mock_render):
        mock_render.return_value = "Rendered content"
        config = {
            "js_renderer": "playwright",
            "playwright": {"timeout": 15000, "wait_until": "domcontentloaded"},
        }
        result = await fetch.render_with_fallback("https://spa-app.com", config)
        assert result == "Rendered content"
        mock_render.assert_called_once_with(
            "https://spa-app.com", timeout=15000, wait_until="domcontentloaded"
        )

    @patch("pivot_web_search_mcp.fetch.render_tavily", new_callable=AsyncMock)
    async def test_query_passed_to_tavily(self, mock_render):
        mock_render.return_value = "Focused content"
        config = {"js_renderer": "tavily", "tavily": {}}
        with patch.dict("os.environ", {"TAVILY_API_KEY": "tvly-key"}):
            await fetch.render_with_fallback("https://example.com", config, query="test query")
        assert mock_render.call_args[1]["query"] == "test query"
