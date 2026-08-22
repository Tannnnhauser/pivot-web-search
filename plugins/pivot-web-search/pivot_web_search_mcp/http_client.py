"""HTTP client singleton, proxy cache, and core dispatch with proxy fallback."""

import asyncio
import json
import pathlib
import re
import time
import urllib.parse

import httpx
from filelock import FileLock

from .logging import log

# ---------------------------------------------------------------------------
# SSL — use certifi CA bundle if available
# ---------------------------------------------------------------------------
try:
    import ssl

    import certifi

    _SSL_VERIFY = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_VERIFY = True

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _get_proxies():
    """Get proxy list from config (hot-reloadable)."""
    from .config import load_proxies

    return load_proxies()


# ---------------------------------------------------------------------------
# Proxy cache (disk-persisted)
# ---------------------------------------------------------------------------

_PROXY_CACHE_FILE = pathlib.Path.home() / ".cache" / "pivot-web-search-proxy-cache.json"
_PROXY_CACHE_FILE_LOCK = FileLock(str(_PROXY_CACHE_FILE) + ".lock")
_PROXY_CACHE_MAX = 128
_PROXY_CACHE_TTL = 3600  # 1 hour — stale proxy mappings expire
_proxy_cache: dict = {}  # hostname -> proxy (None for direct)
_proxy_cache_ts: dict = {}  # hostname -> timestamp of last successful use
_proxy_cache_lock = asyncio.Lock()


def _load_proxy_cache():
    """Load proxy cache from disk. Uses .clear()/.update() to preserve dict identity."""
    try:
        with _PROXY_CACHE_FILE_LOCK:
            if _PROXY_CACHE_FILE.exists():
                raw = json.loads(_PROXY_CACHE_FILE.read_text())
            else:
                raw = {}
    except Exception:
        raw = {}
    # File format: {"proxies": {host: proxy}, "timestamps": {host: ts}}
    # Backward compat: old format was just {host: proxy}
    if "proxies" in raw and isinstance(raw["proxies"], dict):
        loaded = raw["proxies"]
        loaded_ts = raw.get("timestamps", {})
    else:
        loaded = raw
        loaded_ts = {}
    _proxy_cache.clear()
    _proxy_cache.update(loaded)
    _proxy_cache_ts.clear()
    _proxy_cache_ts.update(loaded_ts)


def _save_proxy_cache_sync():
    try:
        with _PROXY_CACHE_FILE_LOCK:
            _PROXY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {"proxies": dict(_proxy_cache), "timestamps": dict(_proxy_cache_ts)}
            _PROXY_CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


async def _save_proxy_cache():
    await asyncio.to_thread(_save_proxy_cache_sync)


_load_proxy_cache()

# ---------------------------------------------------------------------------
# Async HTTP client (singleton, connection-pooled)
# ---------------------------------------------------------------------------

_async_client: httpx.AsyncClient | None = None
_async_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None or _async_client.is_closed:
        async with _async_client_lock:
            if _async_client is None or _async_client.is_closed:
                _async_client = httpx.AsyncClient(
                    verify=_SSL_VERIFY,
                    follow_redirects=False,
                    timeout=httpx.Timeout(30.0, connect=10.0),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
    return _async_client


async def close_client():
    global _async_client
    if _async_client and not _async_client.is_closed:
        await _async_client.aclose()
        _async_client = None


def _proxy_label(proxy):
    """Return a credential-free proxy label suitable for diagnostics."""
    if not proxy:
        return "direct"
    try:
        parsed = urllib.parse.urlparse(proxy)
        if not parsed.scheme or not parsed.hostname:
            return "proxy"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    except (TypeError, ValueError):
        return "proxy"


def _safe_error_text(error):
    """Remove URL userinfo from an exception before writing it to logs."""
    return re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1", str(error))


# ---------------------------------------------------------------------------
# Cross-host redirect detection
# ---------------------------------------------------------------------------


class CrossHostRedirect(Exception):
    def __init__(self, original_host, new_host, location):
        self.location = location
        super().__init__(f"Cross-host redirect blocked: {original_host} -> {new_host}")


# ---------------------------------------------------------------------------
# Core HTTP dispatch with proxy fallback
# ---------------------------------------------------------------------------


async def _try_request_with_redirect(method, url, *, headers, data, timeout, proxy):
    """Single request with cross-host redirect detection and same-host follow."""
    resp = await _do_request(method, url, headers=headers, data=data, timeout=timeout, proxy=proxy)
    if resp.is_redirect:
        location = resp.headers.get("location", "")
        if location:
            orig_host = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.")
            new_host = (urllib.parse.urlparse(location).hostname or "").removeprefix("www.")
            if new_host and orig_host != new_host:
                raise CrossHostRedirect(orig_host, new_host, location)
            resp = await _do_request(method, location, headers=headers, data=None, timeout=timeout, proxy=proxy)
    return resp


async def _record_proxy_success(host, proxy):
    async with _proxy_cache_lock:
        if _proxy_cache.get(host) != proxy:
            _proxy_cache[host] = proxy
            _proxy_cache_ts[host] = time.time()
            while len(_proxy_cache) > _PROXY_CACHE_MAX:
                evicted = next(iter(_proxy_cache))
                _proxy_cache.pop(evicted)
                _proxy_cache_ts.pop(evicted, None)
            await _save_proxy_cache()
        else:
            _proxy_cache_ts[host] = time.time()


async def _open_with_fallback(method, url, *, headers=None, data=None, timeout=30):
    """Try cached proxy first, then all others. Cache the winner per hostname.

    If the cached proxy fails, evict it and try all others so stale cache
    entries (e.g. switching from office to home) self-heal quickly.
    Retries once on transient HTTP errors (429, 503) with 1s backoff.
    """
    host = urllib.parse.urlparse(url).hostname or ""

    proxies = _get_proxies()
    if not proxies:
        proxies = [None]

    async with _proxy_cache_lock:
        cached = _proxy_cache.get(host)
        if cached is not None:
            ts = _proxy_cache_ts.get(host, 0)
            if time.time() - ts > _PROXY_CACHE_TTL:
                del _proxy_cache[host]
                _proxy_cache_ts.pop(host, None)
                cached = None
    if cached is not None and cached in proxies:
        ordered = [cached] + [p for p in proxies if p != cached]
    else:
        ordered = list(proxies)

    last_err = None
    retried_transient = False

    for proxy in ordered:
        try:
            try:
                resp = await _try_request_with_redirect(
                    method, url, headers=headers, data=data, timeout=timeout, proxy=proxy
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 503) and not retried_transient:
                    retried_transient = True
                    log(f"HTTP {e.response.status_code}, retrying once after 1s backoff")
                    await asyncio.sleep(1)
                    resp = await _try_request_with_redirect(
                        method, url, headers=headers, data=data, timeout=timeout, proxy=proxy
                    )
                    log(f"Connected via {_proxy_label(proxy)} (retry)")
                    await _record_proxy_success(host, proxy)
                    return resp
                raise

            log(f"Connected via {_proxy_label(proxy)}")
            await _record_proxy_success(host, proxy)
            return resp

        except CrossHostRedirect:
            raise

        except httpx.HTTPStatusError:
            raise

        except Exception as e:
            label = _proxy_label(proxy)
            log(f"{label} failed: {_safe_error_text(e)}")
            async with _proxy_cache_lock:
                if proxy == cached and host in _proxy_cache:
                    del _proxy_cache[host]
                    _proxy_cache_ts.pop(host, None)
                    await _save_proxy_cache()
            if proxy == cached:
                log(f"Evicted stale cache for {host}")
            last_err = e
            continue

    if last_err is None:
        raise ConnectionError("No proxy routes available")
    raise last_err


async def _do_request(method, url, *, headers=None, data=None, timeout=30, proxy=None):
    """Execute a single HTTP request, raising HTTPStatusError on 4xx/5xx."""
    req_headers = dict(headers) if headers else {}
    if proxy:
        async with httpx.AsyncClient(
            proxy=proxy,
            verify=_SSL_VERIFY,
            follow_redirects=False,
            timeout=httpx.Timeout(float(timeout), connect=10.0),
        ) as client:
            resp = await client.request(method, url, headers=req_headers, content=data)
    else:
        client = await _get_client()
        resp = await client.request(
            method,
            url,
            headers=req_headers,
            content=data,
            timeout=httpx.Timeout(float(timeout), connect=10.0),
        )
    if not resp.is_redirect:
        resp.raise_for_status()
    return resp
