"""Server intelligence tests — smart defaults, quality detection."""

from pivot_web_search_mcp import quota, server


class TestApplySmartDefaults:
    def test_latest_sets_timelimit(self):
        kwargs = {"timelimit": None}
        server._apply_smart_defaults("latest React features", kwargs)
        assert kwargs["timelimit"] == "m"

    def test_recent_sets_timelimit(self):
        kwargs = {"timelimit": None}
        server._apply_smart_defaults("recent AI breakthroughs", kwargs)
        assert kwargs["timelimit"] == "m"

    def test_year_sets_timelimit(self):
        kwargs = {"timelimit": None}
        server._apply_smart_defaults("Python 2026 features", kwargs)
        assert kwargs["timelimit"] == "m"

    def test_chinese_sets_timelimit(self):
        kwargs = {"timelimit": None}
        server._apply_smart_defaults("最新的 AI 技术", kwargs)
        assert kwargs["timelimit"] == "m"

    def test_no_pattern_no_change(self):
        kwargs = {"timelimit": None}
        server._apply_smart_defaults("React features", kwargs)
        assert kwargs["timelimit"] is None

    def test_explicit_param_wins(self):
        kwargs = {"timelimit": "y"}
        server._apply_smart_defaults("latest React features", kwargs)
        assert kwargs["timelimit"] == "y"

    def test_news_detection(self):
        kwargs = {"news": False}
        server._apply_smart_defaults("news about AI", kwargs)
        assert kwargs["news"] is True

    def test_news_explicit_false_overridden(self):
        kwargs = {"news": False}
        server._apply_smart_defaults("breaking news AI", kwargs)
        assert kwargs["news"] is True

    def test_released_sets_news(self):
        kwargs = {"news": False}
        server._apply_smart_defaults("Apple released new iPhone", kwargs)
        assert kwargs["news"] is True

    def test_no_news_pattern(self):
        kwargs = {"news": False}
        server._apply_smart_defaults("Python tutorial", kwargs)
        assert kwargs["news"] is False


class TestFilterByDomains:
    def test_allow_list(self):
        results = [
            {"url": "https://example.com/page"},
            {"url": "https://other.com/page"},
        ]
        filtered = server._filter_by_domains(results, ["example.com"], None)
        assert len(filtered) == 1
        assert filtered[0]["url"] == "https://example.com/page"

    def test_block_list(self):
        results = [
            {"url": "https://example.com/page"},
            {"url": "https://other.com/page"},
        ]
        filtered = server._filter_by_domains(results, None, ["example.com"])
        assert len(filtered) == 1
        assert filtered[0]["url"] == "https://other.com/page"

    def test_www_prefix_stripped(self):
        results = [{"url": "https://www.example.com/page"}]
        filtered = server._filter_by_domains(results, ["example.com"], None)
        assert len(filtered) == 1

    def test_empty_filters_passthrough(self):
        results = [{"url": "https://example.com"}]
        filtered = server._filter_by_domains(results, None, None)
        assert len(filtered) == 1

    def test_combined_filters(self):
        results = [
            {"url": "https://good.com/page"},
            {"url": "https://bad.com/page"},
            {"url": "https://other.com/page"},
        ]
        filtered = server._filter_by_domains(results, ["good.com", "bad.com"], ["bad.com"])
        assert len(filtered) == 1
        assert filtered[0]["url"] == "https://good.com/page"
