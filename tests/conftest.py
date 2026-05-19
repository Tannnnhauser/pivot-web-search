
import pytest

from pivot_web_search_mcp import extraction, http_client, providers, quota, routing, search, server


@pytest.fixture(autouse=True)
def _scrub_userconfig_env(monkeypatch):
    """Strip any PIVOT_USERCONFIG_* env vars so tests see a clean slate.
    Tests that exercise the UI-config path should set these explicitly."""
    import os
    for name in [k for k in os.environ if k.startswith("PIVOT_USERCONFIG_")]:
        monkeypatch.delenv(name, raising=False)


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
    """Reset routing circuit breaker and call counter between tests."""
    server._breaker.reset_all()
    routing._call_counter.reset()
    yield
    server._breaker.reset_all()
    routing._call_counter.reset()


@pytest.fixture(autouse=True)
async def _close_http_client():
    """Close the httpx client after each test to prevent socket leaks."""
    yield
    await http_client.close_client()
