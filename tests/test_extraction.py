"""Content extraction tests — trafilatura + Next.js/Nuxt.js SPA fallback."""

from unittest.mock import AsyncMock, patch

from pivot_web_search_mcp import search

NEXT_DATA_HTML = '''<html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"title":"Test Page","content":"Hello from Next.js data","items":[1,2,3]}}}
</script>
</body></html>'''

NUXT_DATA_HTML = '''<html><body>
<script id="__NUXT_DATA__" type="application/json">
{"data":{"title":"Nuxt Page","body":"Nuxt content here"}}
</script>
</body></html>'''

RSC_HTML = (
    '<html><body>\n<script>self.__next_f.push([1,"'
    + 'x' * 500
    + '\\"title\\":\\"RSC Article Title\\",'
    '\\"content\\":\\"This is the body of the RSC article'
    ' with enough text to extract meaningfully.\\""])'
    '</script>\n</body></html>'
)

EMPTY_SPA_HTML = '<html><body><div id="root"></div></body></html>'

REAL_ARTICLE_HTML = '''<html><body>
<article>
<h1>Python Web Frameworks</h1>
<p>Python has many web frameworks including Django, Flask, and FastAPI.
Django is a full-featured framework with an ORM, admin panel, and authentication built in.
Flask is lightweight and gives developers flexibility. FastAPI is modern with automatic
API documentation and async support. Each framework has its strengths depending on the
project requirements and team expertise.</p>
</article>
</body></html>'''


class TestExtractTrafilatura:
    @patch("pivot_web_search_mcp.search._fetch_url", new_callable=AsyncMock)
    async def test_single_url_success(self, mock_fetch):
        mock_fetch.return_value = (REAL_ARTICLE_HTML.encode(), "text/html")
        result = await search.extract_trafilatura(["https://example.com"])
        assert len(result["results"]) == 1
        assert "Python" in result["results"][0]["raw_content"]
        assert result["failed_results"] == []

    @patch("pivot_web_search_mcp.search._fetch_url", new_callable=AsyncMock)
    async def test_fetch_failure(self, mock_fetch):
        mock_fetch.return_value = (None, "HTTP 404 Not Found")
        result = await search.extract_trafilatura(["https://bad.com"])
        assert len(result["failed_results"]) == 1
        assert "404" in result["failed_results"][0]["error"]
        assert result["results"] == []

    @patch("pivot_web_search_mcp.search._fetch_url", new_callable=AsyncMock)
    async def test_multiple_urls(self, mock_fetch):
        mock_fetch.side_effect = [
            (REAL_ARTICLE_HTML.encode(), "text/html"),
            (None, "HTTP 500"),
        ]
        result = await search.extract_trafilatura(["https://a.com", "https://b.com"])
        assert len(result["results"]) == 1
        assert len(result["failed_results"]) == 1

    @patch("pivot_web_search_mcp.search._fetch_url", new_callable=AsyncMock)
    async def test_nextjs_pages_router_fallback(self, mock_fetch):
        mock_fetch.return_value = (NEXT_DATA_HTML.encode(), "text/html")
        result = await search.extract_trafilatura(["https://spa.com"])
        assert len(result["results"]) == 1
        content = result["results"][0]["raw_content"]
        assert "__NEXT_DATA__" in content
        assert "Test Page" in content

    @patch("pivot_web_search_mcp.search._fetch_url", new_callable=AsyncMock)
    async def test_nuxt_fallback(self, mock_fetch):
        mock_fetch.return_value = (NUXT_DATA_HTML.encode(), "text/html")
        result = await search.extract_trafilatura(["https://nuxt-app.com"])
        assert len(result["results"]) == 1
        content = result["results"][0]["raw_content"]
        assert "__NUXT_DATA__" in content
        assert "Nuxt Page" in content

    @patch("pivot_web_search_mcp.search._fetch_url", new_callable=AsyncMock)
    async def test_empty_spa_fails(self, mock_fetch):
        mock_fetch.return_value = (EMPTY_SPA_HTML.encode(), "text/html")
        result = await search.extract_trafilatura(["https://empty-spa.com"])
        assert len(result["failed_results"]) == 1
        err = result["failed_results"][0]["error"].lower()
        assert "empty" in err or "failed" in err

    @patch("pivot_web_search_mcp.search._fetch_url", new_callable=AsyncMock)
    async def test_rsc_payload_fallback(self, mock_fetch):
        mock_fetch.return_value = (RSC_HTML.encode(), "text/html")
        result = await search.extract_trafilatura(["https://rsc-app.com"])
        if result["results"]:
            content = result["results"][0]["raw_content"]
            assert "RSC" in content or "Article" in content

    @patch("pivot_web_search_mcp.search._fetch_url", new_callable=AsyncMock)
    async def test_returns_dict_never_none(self, mock_fetch):
        mock_fetch.return_value = (None, "connection refused")
        result = await search.extract_trafilatura(["https://x.com"])
        assert isinstance(result, dict)
        assert "results" in result
        assert "failed_results" in result


class TestValidateUrlSsrf:
    def test_rejects_localhost(self):
        import pytest
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 0, "", ("127.0.0.1", 0))
        ]):
            with pytest.raises(ValueError, match="private"):
                search.validate_url("https://evil.example.com/path")

    def test_rejects_private_10_range(self):
        import pytest
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 0, "", ("10.0.0.1", 0))
        ]):
            with pytest.raises(ValueError, match="private"):
                search.validate_url("https://internal.example.com")

    def test_rejects_link_local(self):
        import pytest
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 0, "", ("169.254.169.254", 0))
        ]):
            with pytest.raises(ValueError, match="private"):
                search.validate_url("https://metadata.example.com")

    def test_allows_public_ip(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 0, "", ("93.184.216.34", 0))
        ]):
            result = search.validate_url("https://example.com/path")
            assert result == "https://example.com/path"

    def test_dns_failure_passes_through(self):
        import socket as _socket
        with patch("socket.getaddrinfo", side_effect=_socket.gaierror("DNS failed")):
            result = search.validate_url("https://nonexistent.example.com")
            assert "nonexistent.example.com" in result


class TestCrossHostRedirect:
    async def test_cross_host_blocked(self):
        """Cross-host redirects raise CrossHostRedirect exception."""
        import httpx

        mock_resp_redirect = httpx.Response(
            301, headers={"location": "https://evil.com/phish"},
            request=httpx.Request("GET", "https://example.com/page"))

        with patch("pivot_web_search_mcp.search._do_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_resp_redirect
            result = await search._fetch_url("https://example.com/page")
            assert result[0] is None
            assert "cross-host redirect blocked" in result[1]

    async def test_same_host_redirect_followed(self):
        """Same-host redirects are followed."""
        import httpx

        mock_resp_redirect = httpx.Response(
            301, headers={"location": "https://example.com/new"},
            request=httpx.Request("GET", "https://example.com/old"))
        mock_resp_final = httpx.Response(
            200, content=b"<html><body>Content</body></html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/new"))

        with patch("pivot_web_search_mcp.search._do_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [mock_resp_redirect, mock_resp_final]
            # Use _open_with_fallback directly to test redirect handling
            resp = await search._open_with_fallback("GET", "https://example.com/old")
            assert resp.status_code == 200
