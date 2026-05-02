"""Failover logic tests — _search_with_registry quality-aware continuation."""

from unittest.mock import patch

from pivot_web_search_mcp import server
from pivot_web_search_mcp.providers import SearchProvider, SearchResult


class FakeProvider(SearchProvider):
    """Test provider returning configurable results."""
    provider_type = "fake"

    def __init__(self, name, results=None, priority=10, enabled=True):
        super().__init__(name, priority, enabled)
        self._results = results

    async def search(self, query, max_results=5, **kwargs):
        if self._results is None:
            return None
        return SearchResult(results=self._results[:max_results], provider=self.name)

    async def health_check(self):
        return True, None


def _make_results(n):
    return [{"title": f"R{i}", "url": f"https://r{i}.example.com", "snippet": f"Snippet {i}"} for i in range(n)]


class TestSearchWithRegistry:
    @patch.object(server._registry, 'get_ordered')
    async def test_first_provider_sufficient(self, mock_ordered):
        mock_ordered.return_value = [FakeProvider("good", _make_results(5))]
        sr = await server._search_with_registry("test", 5)
        assert sr is not None
        assert len(sr.results) == 5
        assert sr.provider == "good"

    @patch.object(server._registry, 'get_ordered')
    async def test_falls_through_on_low_quality(self, mock_ordered):
        mock_ordered.return_value = [
            FakeProvider("poor", _make_results(1), priority=10),
            FakeProvider("good", _make_results(5), priority=20),
        ]
        sr = await server._search_with_registry("test", 5)
        assert sr.provider == "good"
        assert len(sr.results) == 5

    @patch.object(server._registry, 'get_ordered')
    async def test_keeps_best_when_all_low(self, mock_ordered):
        mock_ordered.return_value = [
            FakeProvider("one", _make_results(1), priority=10),
            FakeProvider("zero", [], priority=20),
        ]
        sr = await server._search_with_registry("test", 5)
        assert sr is not None
        assert len(sr.results) == 1

    @patch.object(server._registry, 'get_ordered')
    async def test_all_fail_returns_failure_info(self, mock_ordered):
        mock_ordered.return_value = [
            FakeProvider("fail1", None, priority=10),
            FakeProvider("fail2", None, priority=20),
        ]
        sr = await server._search_with_registry("test", 5)
        assert isinstance(sr, server._FailureInfo)
        assert len(sr.failures) == 2
        assert sr.failures[0]["provider"] == "fail1"
        assert sr.failures[1]["provider"] == "fail2"

    @patch.object(server._registry, 'get_by_name')
    async def test_specific_provider(self, mock_get):
        provider = FakeProvider("tavily", _make_results(3))
        mock_get.return_value = provider
        sr = await server._search_with_registry("test", 5, provider_name="tavily")
        assert sr is not None
        assert sr.provider == "tavily"

    @patch.object(server._registry, 'get_by_name')
    async def test_specific_provider_not_found(self, mock_get):
        mock_get.return_value = None
        sr = await server._search_with_registry("test", 5, provider_name="nonexistent")
        assert sr is None

    @patch.object(server._registry, 'get_ordered')
    async def test_min_acceptable_for_small_max(self, mock_ordered):
        mock_ordered.return_value = [
            FakeProvider("one", _make_results(1), priority=10),
        ]
        sr = await server._search_with_registry("test", 1)
        assert sr is not None
        assert len(sr.results) == 1


class TestDedupAndRank:
    def test_single_provider(self):
        from pivot_web_search_mcp.search import dedup_and_rank
        results_by_provider = {"ddg": _make_results(3)}
        merged, providers_used = dedup_and_rank(results_by_provider, 10)
        assert len(merged) == 3
        assert providers_used == ["ddg"]

    def test_two_providers_overlap(self):
        from pivot_web_search_mcp.search import dedup_and_rank
        results_by_provider = {
            "ddg": [{"title": "A", "url": "https://a.com/page", "snippet": "short"}],
            "tavily": [{"title": "A Long", "url": "https://a.com/page", "snippet": "longer snippet here"}],
        }
        merged, providers_used = dedup_and_rank(results_by_provider, 10)
        assert len(merged) == 1
        assert merged[0]["snippet"] == "longer snippet here"
        assert merged[0]["title"] == "A Long"
        assert set(merged[0]["_providers"]) == {"ddg", "tavily"}

    def test_gemini_opaque_url(self):
        from pivot_web_search_mcp.search import dedup_and_rank
        results_by_provider = {
            "gemini": [{"title": "Python Docs", "url": "https://vertexaisearch.cloud.google.com/x", "snippet": "s"}],
            "ddg": [{"title": "Python Docs", "url": "https://vertexaisearch.cloud.google.com/y", "snippet": "longer"}],
        }
        merged, _ = dedup_and_rank(results_by_provider, 10)
        assert len(merged) == 1
        assert "longer" in merged[0]["snippet"]

    def test_max_results_respected(self):
        from pivot_web_search_mcp.search import dedup_and_rank
        results_by_provider = {"ddg": _make_results(10)}
        merged, _ = dedup_and_rank(results_by_provider, 3)
        assert len(merged) == 3

    def test_ranking_by_provider_count(self):
        from pivot_web_search_mcp.search import dedup_and_rank
        results_by_provider = {
            "ddg": [
                {"title": "Multi", "url": "https://multi.com", "snippet": "s"},
                {"title": "Single", "url": "https://single.com", "snippet": "s"},
            ],
            "tavily": [
                {"title": "Multi", "url": "https://multi.com", "snippet": "s"},
            ],
        }
        merged, _ = dedup_and_rank(results_by_provider, 10)
        assert merged[0]["url"] == "https://multi.com"
