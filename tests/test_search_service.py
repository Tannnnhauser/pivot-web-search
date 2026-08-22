"""Shared structured search orchestration tests."""

import asyncio

import pytest

from pivot_web_search_mcp.providers import SearchProvider, SearchResult
from pivot_web_search_mcp.search_service import (
    SearchRequest,
    SearchService,
    SearchServiceError,
)


class FakeProvider(SearchProvider):
    provider_type = "fake"

    def __init__(self, name="fake", *, results=None, enabled=True, error=None, delay=0):
        super().__init__(name, 10, enabled, {"timeout": 1})
        self.results = results
        self.error = error
        self.delay = delay
        self.calls = []

    async def health_check(self):
        return True, None

    async def search(self, query, max_results=5, **kwargs):
        self.calls.append((query, max_results, kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return SearchResult(results=list(self.results or []), provider=self.name)


class FakeRegistry:
    def __init__(self, providers):
        self.providers = providers

    def get_ordered(self):
        return [provider for provider in self.providers if provider.enabled]

    def get_by_name(self, name):
        return next((provider for provider in self.providers if provider.name == name), None)

    def get_all(self):
        return list(self.providers)


def result(url="https://example.com", title="Example"):
    return {"title": title, "url": url, "snippet": "snippet"}


async def test_returns_structured_response_and_smart_defaults():
    provider = FakeProvider(results=[result(), result("https://two.example")])
    service = SearchService(FakeRegistry([provider]))
    response = await service.search(SearchRequest(query="latest product news"))
    assert response.provider == "fake"
    assert response.results[0]["url"] == "https://example.com"
    assert provider.calls[0][2]["timelimit"] == "m"
    assert provider.calls[0][2]["news"] is True
    assert not hasattr(response, "markdown")


@pytest.mark.parametrize("query", ["", "   "])
async def test_rejects_empty_query(query):
    service = SearchService(FakeRegistry([]))
    with pytest.raises(SearchServiceError, match="Empty query") as raised:
        await service.search(SearchRequest(query=query))
    assert raised.value.code == "INVALID_REQUEST"


async def test_explicit_unknown_and_disabled_provider_are_structured_failures():
    disabled = FakeProvider(name="off", enabled=False)
    service = SearchService(FakeRegistry([disabled]))
    with pytest.raises(SearchServiceError) as unknown:
        await service.search(SearchRequest(query="q", provider="missing"))
    assert unknown.value.failures[0]["provider"] == "missing"
    with pytest.raises(SearchServiceError) as off:
        await service.search(SearchRequest(query="q", provider="off"))
    assert off.value.failures[0]["error"] == "provider is disabled"


async def test_normal_and_super_clamp_result_limits():
    normal = FakeProvider(results=[result(), result("https://two.example")])
    service = SearchService(FakeRegistry([normal]))
    await service.search(SearchRequest(query="q", max_results=999))
    assert normal.calls[0][1] == 10

    first = FakeProvider("first", results=[result("https://one.example"), result("https://one2.example")])
    second = FakeProvider("second", results=[result("https://two.example"), result("https://two2.example")])
    service = SearchService(FakeRegistry([first, second]))
    response = await service.search(SearchRequest(query="q", max_results=999, mode="super"))
    assert first.calls[0][1] == 20
    assert second.calls[0][1] == 20
    assert response.provider == "first,second"


async def test_domain_filter_forces_tavily_and_maps_native_filters():
    tavily = FakeProvider("tavily", results=[result("https://good.example/a")])
    other = FakeProvider("other", results=[result("https://bad.example/a")])
    service = SearchService(FakeRegistry([other, tavily]))
    response = await service.search(SearchRequest(query="q", allowed_domains=["good.example"]))
    assert response.provider == "tavily"
    assert not other.calls
    assert tavily.calls[0][2]["include_domains"] == ["good.example"]


async def test_all_providers_failed_is_business_error():
    provider = FakeProvider(error=RuntimeError("boom"))
    service = SearchService(FakeRegistry([provider]))
    with pytest.raises(SearchServiceError) as raised:
        await service.search(SearchRequest(query="q"))
    assert raised.value.code == "SEARCH_FAILED"
    assert raised.value.failures


async def test_cancellation_propagates():
    provider = FakeProvider(results=[result()], delay=1)
    service = SearchService(FakeRegistry([provider]))
    task = asyncio.create_task(service.search(SearchRequest(query="q")))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_include_content_success_and_downgrade(monkeypatch):
    async def content_success(*args, **kwargs):
        return ([{"title": "Page", "url": "https://page.example", "snippets": ["full chunk"]}], {})

    monkeypatch.setattr("pivot_web_search_mcp.search_service.search_brave_llm_context", content_success)
    service = SearchService(FakeRegistry([]))
    response = await service.search(SearchRequest(query="q", include_content=True))
    assert response.content_included is True
    assert response.results[0]["snippets"] == ["full chunk"]

    async def unavailable(*args, **kwargs):
        return None

    fallback = FakeProvider(results=[result(), result("https://two.example")])
    monkeypatch.setattr("pivot_web_search_mcp.search_service.search_brave_llm_context", unavailable)
    service = SearchService(FakeRegistry([fallback]))
    response = await service.search(SearchRequest(query="q", include_content=True))
    assert response.content_included is False
    assert response.content_downgrade_reason == "Brave LLM Context unavailable"
