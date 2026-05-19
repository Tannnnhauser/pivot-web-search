"""Pure function tests — zero mocking, zero network."""

from pivot_web_search_mcp import search
from pivot_web_search_mcp.validation import _load_env_key


class TestValidateUrl:
    def test_http_upgrades_to_https(self):
        assert search.validate_url("http://example.com") == "https://example.com"

    def test_https_passthrough(self):
        assert search.validate_url("https://example.com") == "https://example.com"

    def test_rejects_ftp(self):
        try:
            search.validate_url("ftp://example.com")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "not allowed" in str(e)

    def test_rejects_credentials(self):
        try:
            search.validate_url("https://user:pass@example.com")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "credentials" in str(e).lower()

    def test_rejects_short_hostname(self):
        try:
            search.validate_url("https://localhost")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "hostname" in str(e).lower()

    def test_rejects_long_url(self):
        long_url = "https://example.com/" + "a" * 2000
        try:
            search.validate_url(long_url)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "too long" in str(e).lower()


class TestNormalizeUrl:
    def test_strips_www(self):
        assert search._normalize_url("https://www.example.com/page") == "example.com/page"

    def test_lowercases(self):
        assert search._normalize_url("https://EXAMPLE.COM/Page") == "example.com/Page"

    def test_strips_trailing_slash(self):
        assert search._normalize_url("https://example.com/page/") == "example.com/page"

    def test_handles_empty(self):
        result = search._normalize_url("")
        assert isinstance(result, str)


class TestIsBinaryContentType:
    def test_image_png(self):
        assert search._is_binary_content_type("image/png") is True

    def test_text_html(self):
        assert search._is_binary_content_type("text/html") is False

    def test_application_json(self):
        assert search._is_binary_content_type("application/json") is False

    def test_application_pdf(self):
        assert search._is_binary_content_type("application/pdf") is True

    def test_none(self):
        assert search._is_binary_content_type(None) is False

    def test_with_charset(self):
        assert search._is_binary_content_type("image/png; charset=utf-8") is True


class TestToMarkdown:
    def test_numbered_results(self):
        results = [
            {"title": "Example", "url": "https://example.com", "snippet": "A snippet"},
            {"title": "Other", "url": "https://other.com", "snippet": "Another"},
        ]
        md = search.to_markdown(results, "test query")
        assert "1. Example" in md
        assert "2. Other" in md
        assert "https://example.com" in md

    def test_sources_section(self):
        results = [{"title": "Example", "url": "https://example.com", "snippet": "text"}]
        md = search.to_markdown(results, "query")
        assert "Sources:" in md
        assert "[Example](https://example.com)" in md

    def test_with_answer(self):
        results = [{"title": "T", "url": "https://t.com", "snippet": "s"}]
        md = search.to_markdown(results, "query", answer="The answer is 42.")
        assert "The answer is 42." in md

    def test_with_provider(self):
        results = [{"title": "T", "url": "https://t.com", "snippet": "s"}]
        md = search.to_markdown(results, "query", provider="ddg")
        assert "*Source: ddg*" in md

    def test_missing_fields(self):
        results = [{"url": "https://t.com"}]
        md = search.to_markdown(results, "query")
        assert "https://t.com" in md

    def test_empty_results(self):
        md = search.to_markdown([], "query")
        assert isinstance(md, str)


class TestLoadEnvKey:
    """Plan J: standard env var wins over PIVOT_USERCONFIG_*; missing returns None."""

    def test_standard_var_wins(self, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY", "shell-value")
        monkeypatch.setenv("PIVOT_USERCONFIG_TEST_API_KEY", "ui-value")
        assert _load_env_key("TEST_API_KEY") == "shell-value"

    def test_userconfig_fallback(self, monkeypatch):
        monkeypatch.delenv("TEST_API_KEY", raising=False)
        monkeypatch.setenv("PIVOT_USERCONFIG_TEST_API_KEY", "ui-value")
        assert _load_env_key("TEST_API_KEY") == "ui-value"

    def test_missing_returns_none(self, monkeypatch):
        monkeypatch.delenv("TEST_API_KEY", raising=False)
        monkeypatch.delenv("PIVOT_USERCONFIG_TEST_API_KEY", raising=False)
        assert _load_env_key("TEST_API_KEY") is None

    def test_empty_string_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY", "   ")
        monkeypatch.setenv("PIVOT_USERCONFIG_TEST_API_KEY", "ui-value")
        assert _load_env_key("TEST_API_KEY") == "ui-value"
