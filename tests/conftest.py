
import pytest

from pivot_web_search_mcp import extraction, http_client, providers, quota, search, server


@pytest.fixture(autouse=True)
def _isolate_quota(tmp_path, monkeypatch):
    """Redirect quota.json to a temp dir so tests don't touch real state."""
    monkeypatch.setattr(quota, "_QUOTA_DIR", tmp_path)
    monkeypatch.setattr(quota, "_QUOTA_FILE", tmp_path / "quota.json")
    quota._quota_cache = None
    quota._quota_cache_ts = 0


@pytest.fixture(autouse=True)
def _reset_search_caches():
    """Clear search.py global caches between tests."""
    search._proxy_cache.clear()
    search._proxy_cache_ts.clear()
    search._fetch_cache.clear()
    extraction._fetch_cache_bytes = 0
    yield
    search._proxy_cache.clear()
    search._proxy_cache_ts.clear()
    search._fetch_cache.clear()
    extraction._fetch_cache_bytes = 0


@pytest.fixture(autouse=True)
def _reset_proxy_config():
    """Reset providers.py proxy cache between tests."""
    providers._proxies_list = None
    providers._proxies_mtime = 0
    yield
    providers._proxies_list = None
    providers._proxies_mtime = 0


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """Reset routing circuit breaker state between tests."""
    server._breaker.reset_all()
    yield
    server._breaker.reset_all()


@pytest.fixture(autouse=True)
async def _close_http_client():
    """Close the httpx client after each test to prevent socket leaks."""
    yield
    await http_client.close_client()
