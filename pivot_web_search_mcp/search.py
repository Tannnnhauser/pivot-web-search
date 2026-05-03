#!/usr/bin/env python3
"""Unified web search: DDG -> Tavily -> Brave -> Gemini (4-layer failover).

DDG automatically rotates backends (auto -> lite -> html) and retries on
transient errors. Falls back to Tavily, then Brave, then Gemini if all
prior attempts fail. Super mode queries all providers in parallel.
"""

import argparse
import asyncio
import collections
import ipaddress
import json
import os
import pathlib
import re
import socket
import sys
import time
import urllib.parse

import httpx
from filelock import FileLock

try:
    from .logging import log
except ImportError:
    def log(msg):
        print(f"[pivot-web-search] {msg}", file=sys.stderr)

# ---------------------------------------------------------------------------
# SSL — use certifi CA bundle if available
# ---------------------------------------------------------------------------
try:
    import certifi
    _SSL_VERIFY = certifi.where()
except ImportError:
    _SSL_VERIFY = True

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXIES = [None]  # legacy default, overridden by config

def _get_proxies():
    """Get proxy list from config (hot-reloadable) or fall back to hardcoded PROXIES."""
    try:
        from .providers import load_proxies
        return load_proxies()
    except Exception:
        return PROXIES
DDG_BACKENDS = ["auto", "lite", "html"]
_DDG_RETRY_DELAY = 0.3
TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_FETCH_BYTES = 10 * 1024 * 1024  # 10 MB download cap
MAX_CONTENT_CHARS = 100_000  # 100K chars markdown truncation
FETCH_CACHE_TTL = 15 * 60  # 15 minutes
FETCH_CACHE_MAX = 64  # max cached URLs
BINARY_CONTENT_TYPES = {"image/", "audio/", "video/", "application/octet-stream",
                        "application/pdf", "application/zip", "application/gzip"}

# Per-host proxy cache: remembers which proxy worked last for each hostname.
# Persisted to disk so repeat invocations skip dead connections.
_PROXY_CACHE_FILE = pathlib.Path.home() / ".cache" / "pivot-web-search-proxy-cache.json"
_PROXY_CACHE_FILE_LOCK = FileLock(str(_PROXY_CACHE_FILE) + ".lock")
_PROXY_CACHE_MAX = 128
_proxy_cache = {}  # hostname -> proxy (None for direct)
_proxy_cache_lock = asyncio.Lock()

# In-memory URL fetch cache: LRU with TTL
_FetchCacheEntry = collections.namedtuple("_FetchCacheEntry", ["content", "content_type", "ts"])
_fetch_cache = collections.OrderedDict()  # url -> _FetchCacheEntry
_fetch_cache_lock = asyncio.Lock()

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
        await _async_client.close()
        _async_client = None

# ---------------------------------------------------------------------------
# Proxy cache (disk-persisted)
# ---------------------------------------------------------------------------


def _load_proxy_cache():
    global _proxy_cache
    try:
        with _PROXY_CACHE_FILE_LOCK:
            if _PROXY_CACHE_FILE.exists():
                _proxy_cache = json.loads(_PROXY_CACHE_FILE.read_text())
            else:
                _proxy_cache = {}
    except Exception:
        _proxy_cache = {}


def _save_proxy_cache_sync():
    try:
        with _PROXY_CACHE_FILE_LOCK:
            _PROXY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PROXY_CACHE_FILE.write_text(json.dumps(_proxy_cache))
    except Exception:
        pass


async def _save_proxy_cache():
    await asyncio.to_thread(_save_proxy_cache_sync)


_load_proxy_cache()

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
    if cached is not None and cached in proxies:
        ordered = [cached] + [p for p in proxies if p != cached]
    else:
        ordered = list(proxies)

    last_err = None
    retried_transient = False

    for proxy in ordered:
        try:
            resp = await _do_request(method, url, headers=headers, data=data,
                                     timeout=timeout, proxy=proxy)
            # Check for cross-host redirects
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                if location:
                    orig_host = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.")
                    new_host = (urllib.parse.urlparse(location).hostname or "").removeprefix("www.")
                    if new_host and orig_host != new_host:
                        raise CrossHostRedirect(orig_host, new_host, location)
                    # Same-host redirect: follow it
                    resp = await _do_request(method, location, headers=headers, data=None,
                                            timeout=timeout, proxy=proxy)

            label = proxy or "direct"
            log(f"Connected via {label}")
            async with _proxy_cache_lock:
                if _proxy_cache.get(host) != proxy:
                    _proxy_cache[host] = proxy
                    while len(_proxy_cache) > _PROXY_CACHE_MAX:
                        _proxy_cache.pop(next(iter(_proxy_cache)))
                    await _save_proxy_cache()
            return resp

        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 503) and not retried_transient:
                retried_transient = True
                log(f"HTTP {e.response.status_code}, retrying once after 1s backoff")
                await asyncio.sleep(1)
                try:
                    resp = await _do_request(method, url, headers=headers, data=data,
                                            timeout=timeout, proxy=proxy)
                    if resp.is_redirect:
                        location = resp.headers.get("location", "")
                        if location:
                            orig_host = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.")
                            new_host = (urllib.parse.urlparse(location).hostname or "").removeprefix("www.")
                            if new_host and orig_host != new_host:
                                raise CrossHostRedirect(orig_host, new_host, location)
                            resp = await _do_request(method, location, headers=headers, data=None,
                                                    timeout=timeout, proxy=proxy)
                    label = proxy or "direct"
                    log(f"Connected via {label} (retry)")
                    async with _proxy_cache_lock:
                        if _proxy_cache.get(host) != proxy:
                            _proxy_cache[host] = proxy
                            while len(_proxy_cache) > _PROXY_CACHE_MAX:
                                _proxy_cache.pop(next(iter(_proxy_cache)))
                            await _save_proxy_cache()
                    return resp
                except httpx.HTTPStatusError:
                    raise
                except Exception:
                    raise e
            raise

        except CrossHostRedirect:
            raise

        except Exception as e:
            label = proxy or "direct"
            log(f"{label} failed: {e}")
            async with _proxy_cache_lock:
                if proxy == cached and host in _proxy_cache:
                    del _proxy_cache[host]
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
            proxy=proxy, verify=_SSL_VERIFY, follow_redirects=False,
            timeout=httpx.Timeout(float(timeout), connect=10.0),
        ) as client:
            resp = await client.request(method, url, headers=req_headers, content=data)
    else:
        client = await _get_client()
        resp = await client.request(
            method, url, headers=req_headers, content=data,
            timeout=httpx.Timeout(float(timeout), connect=10.0),
        )
    if not resp.is_redirect:
        resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Key loaders
# ---------------------------------------------------------------------------

def _load_tavily_key():
    return os.environ.get("TAVILY_API_KEY", "").strip() or None


def _load_brave_key():
    return os.environ.get("BRAVE_API_KEY", "").strip() or None


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def validate_url(url):
    """Validate and normalize a URL for fetching. Returns normalized URL or raises ValueError."""
    if len(url) > 2000:
        raise ValueError(f"URL too long ({len(url)} chars, max 2000)")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme '{parsed.scheme}' not allowed (only http/https)")
    if parsed.username or parsed.password:
        raise ValueError("URLs with credentials (username/password) are not allowed")
    hostname = parsed.hostname or ""
    if "." not in hostname:
        raise ValueError(f"Invalid hostname '{hostname}' (must have at least two segments)")
    # SSRF protection: block private/reserved IP ranges
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
                raise ValueError(f"URL resolves to private/reserved IP ({ip})")
    except socket.gaierror:
        pass  # DNS resolution failure; let the request fail naturally later
    except ValueError as e:
        if "private" in str(e) or "reserved" in str(e):
            raise
    # Auto-upgrade http to https
    if parsed.scheme == "http":
        url = "https" + url[4:]
    return url


def _is_binary_content_type(ct):
    """Check if a content-type header indicates binary content."""
    if not ct:
        return False
    ct = ct.lower().split(";")[0].strip()
    return any(ct.startswith(b) for b in BINARY_CONTENT_TYPES)

# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------


async def search_ddg(query, max_results=5, region="wt-wt", timelimit=None, news=False):
    """DDG with backend rotation + retries, using proxy cache for ordering."""
    try:
        from ddgs import DDGS
    except ImportError:
        log("ddgs not installed — run: uv sync")
        return None

    ddg_host = "duckduckgo.com"
    proxies = _get_proxies()
    async with _proxy_cache_lock:
        cached = _proxy_cache.get(ddg_host)
    if cached is not None and cached in proxies:
        ordered_proxies = [cached] + [p for p in proxies if p != cached]
    else:
        ordered_proxies = list(proxies)

    def _ddg_sync():
        """Run DDG search synchronously (ddgs library is sync)."""
        for proxy in ordered_proxies:
            for backend in DDG_BACKENDS:
                try:
                    ddgs_kwargs = {"proxy": proxy} if proxy else {}
                    with DDGS(**ddgs_kwargs) as ddgs:
                        if news:
                            results = list(ddgs.news(query, max_results=max_results, region=region))
                        else:
                            kwargs = {"query": query, "max_results": max_results,
                                      "region": region, "backend": backend}
                            if timelimit:
                                kwargs["timelimit"] = timelimit
                            results = list(ddgs.text(**kwargs))
                        if results:
                            return (proxy, backend, results)
                except (ConnectionError, TimeoutError, OSError) as e:
                    label = proxy or "direct"
                    log(f"DDG {label} backend={backend} connection failed: {e}")
                    break
                except Exception as e:
                    label = proxy or "direct"
                    log(f"DDG {label} backend={backend} failed: {e}")
                    time.sleep(_DDG_RETRY_DELAY)
        return None

    result = await asyncio.to_thread(_ddg_sync)
    if result is None:
        return None

    proxy, backend, raw_results = result
    label = proxy or "direct"
    log(f"DDG connected via {label} backend={backend}")
    async with _proxy_cache_lock:
        if _proxy_cache.get(ddg_host) != proxy:
            _proxy_cache[ddg_host] = proxy
            await _save_proxy_cache()
    return [{"title": r.get("title", ""),
             "url": r.get("href", r.get("url", "")),
             "snippet": r.get("body", r.get("content", ""))}
            for r in raw_results]


async def search_tavily(query, max_results=5, include_answer=False, search_depth="basic",
                        topic="general", days=None, include_domains=None, exclude_domains=None):
    """Tavily Search API."""
    key = _load_tavily_key()
    if not key:
        log("No TAVILY_API_KEY, skipping Tavily")
        return None

    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": include_answer,
        "include_images": False,
        "include_raw_content": False,
        "topic": topic,
    }
    if days and topic == "news":
        payload["days"] = days
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains

    data = json.dumps(payload).encode("utf-8")
    try:
        resp = await _open_with_fallback(
            "POST", TAVILY_URL,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            data=data)
        obj = resp.json()
        results = [{"title": r.get("title", ""), "url": r.get("url", ""),
                     "snippet": r.get("content", "")}
                    for r in (obj.get("results") or [])[:max_results]]
        return results, obj.get("answer")
    except Exception as e:
        log(f"Tavily failed: {e}")
        return None


async def _fetch_url(url, timeout=30):
    """Fetch raw content from a URL using the proxy-fallback layer.

    Returns (body_bytes, content_type) or (None, error_string) on failure.
    Enforces: scheme validation, size cap, binary detection, redirect safety, caching.
    """
    # Check cache first
    async with _fetch_cache_lock:
        cached_entry = _fetch_cache.get(url)
        if cached_entry and (time.time() - cached_entry.ts) < FETCH_CACHE_TTL:
            _fetch_cache.move_to_end(url)
            log(f"cache hit: {url}")
            return cached_entry.content, cached_entry.content_type

    try:
        url = validate_url(url)
    except ValueError as e:
        return None, str(e)

    try:
        resp = await _open_with_fallback(
            "GET", url, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; pivot-web-search/1.0)"})
        ct = resp.headers.get("content-type", "")
        if _is_binary_content_type(ct):
            return None, f"binary content ({ct.split(';')[0].strip()}), skipping extraction"

        body = resp.content
        if len(body) > MAX_FETCH_BYTES:
            body = body[:MAX_FETCH_BYTES]
            log(f"truncated download to {MAX_FETCH_BYTES} bytes: {url}")

        # Store in cache
        async with _fetch_cache_lock:
            _fetch_cache[url] = _FetchCacheEntry(body, ct, time.time())
            _fetch_cache.move_to_end(url)
            while len(_fetch_cache) > FETCH_CACHE_MAX:
                _fetch_cache.popitem(last=False)

        return body, ct

    except CrossHostRedirect as e:
        return None, f"cross-host redirect blocked; re-request with: {e.location}"
    except httpx.HTTPStatusError as e:
        log(f"fetch HTTP {e.response.status_code} for {url}")
        return None, f"HTTP {e.response.status_code}"
    except Exception as e:
        log(f"fetch failed for {url}: {e}")
        return None, str(e)


def _extract_nextjs_data(html):
    """Try to extract structured data from Next.js embedded JSON.

    Works on three patterns:
    1. __NEXT_DATA__ (pages router) — full JSON in <script id="__NEXT_DATA__">
    2. RSC payload (app router) — escaped JSON in <script> tags
    3. __NUXT_DATA__ (Nuxt.js) — JSON in <script id="__NUXT_DATA__">
    """
    # Pattern 1: Classic __NEXT_DATA__
    match = re.search(
        r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            props = data.get("props", {}).get("pageProps", {})
            if props:
                text = json.dumps(props, indent=2, ensure_ascii=False)
                return f"[Extracted from __NEXT_DATA__]\n\n{text}"
        except json.JSONDecodeError:
            pass

    # Pattern 2: RSC payload — look for meaningful content in script tags
    scripts_with_data = re.findall(
        r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    for script in scripts_with_data:
        if len(script) < 500:
            continue
        if '\\"title\\"' in script or '\\"content\\"' in script or '\\"body\\"' in script:
            try:
                unescaped = script.replace('\\"', '"').replace('\\\\', '\\')
                titles = re.findall(r'"title"\s*:\s*"([^"]{10,})"', unescaped)
                bodies = re.findall(
                    r'"(?:body|content|summary|description|text)"\s*:\s*"([^"]{20,})"', unescaped)
                if titles or bodies:
                    parts = []
                    for t in titles[:10]:
                        clean = re.sub(r'\\n', '\n', t)
                        parts.append(f"## {clean}")
                    for b in bodies[:10]:
                        clean = re.sub(r'\\n', '\n', b)
                        parts.append(clean)
                    if parts:
                        return "[Extracted from Next.js RSC data]\n\n" + "\n\n".join(parts)
            except Exception:
                pass

    # Pattern 3: __NUXT_DATA__
    match = re.search(
        r'<script\s+id="__NUXT_DATA__"[^>]*>(.*?)</script>',
        html, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, (dict, list)):
                text = json.dumps(data, indent=2, ensure_ascii=False)
                return f"[Extracted from __NUXT_DATA__]\n\n{text[:10000]}"
        except json.JSONDecodeError:
            pass

    return None


async def extract_trafilatura(urls):
    """Local content extraction using trafilatura + proxy-fallback layer.

    Multiple URLs are fetched and extracted concurrently.
    Falls back to Next.js data extraction for SPA pages where trafilatura fails.
    Always returns {"results": [...], "failed_results": [...]}, never None.
    """
    try:
        import trafilatura
    except ImportError:
        log("trafilatura not installed — run: uv sync")
        return {"results": [], "failed_results": [{"url": u, "error": "trafilatura not installed"} for u in urls]}

    async def _extract_one(url):
        """Fetch and extract a single URL. Returns (url, text, error)."""
        try:
            raw, ct_or_err = await _fetch_url(url)
            if raw is None:
                return (url, None, ct_or_err)
            html = raw.decode("utf-8", errors="replace")

            # trafilatura is CPU-bound — run in thread
            text = await asyncio.to_thread(
                trafilatura.extract, html, output_format="markdown",
                include_links=True, include_tables=True)
            if text:
                log(f"extracted: {url}")
                return (url, text, None)

            # Fallback: try Next.js/Nuxt.js data extraction
            nextjs_text = _extract_nextjs_data(html)
            if nextjs_text:
                log(f"extracted via Next.js fallback: {url}")
                return (url, nextjs_text, None)

            return (url, None, "extraction returned empty (trafilatura + nextjs both failed)")
        except Exception as e:
            log(f"extract failed for {url}: {e}")
            return (url, None, str(e))

    extractions = await asyncio.gather(*[_extract_one(u) for u in urls])

    results = []
    failed = []
    for url, text, err in extractions:
        if text:
            results.append({"url": url, "raw_content": text})
        else:
            failed.append({"url": url, "error": err})

    return {"results": results, "failed_results": failed}


async def search_brave(query, max_results=5):
    """Brave Search API. Returns (results, headers_dict) or None."""
    key = _load_brave_key()
    if not key:
        log("No Brave API key, skipping Brave")
        return None

    params = urllib.parse.urlencode({"q": query, "count": max_results})
    try:
        resp = await _open_with_fallback(
            "GET", f"{BRAVE_URL}?{params}",
            headers={"Accept": "application/json", "Accept-Encoding": "gzip",
                     "X-Subscription-Token": key})
        resp_headers = dict(resp.headers)
        obj = resp.json()
        results = [{"title": r.get("title", ""), "url": r.get("url", ""),
                     "snippet": r.get("description", "")}
                    for r in (obj.get("web", {}).get("results") or [])[:max_results]]
        return (results, resp_headers) if results else None
    except Exception as e:
        log(f"Brave failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Tavily Extract API
# ---------------------------------------------------------------------------

TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"


async def extract_tavily(urls, extract_depth="advanced", fmt="markdown", timeout=30,
                         query=None, chunks_per_source=None):
    """Extract content from URLs via Tavily Extract API.

    Returns same shape as extract_trafilatura: {"results": [...], "failed_results": [...]}.
    Each result has "url" and "raw_content" keys.
    """
    key = _load_tavily_key()
    if not key:
        return {"results": [], "failed_results": [{"url": u, "error": "no TAVILY_API_KEY"} for u in urls]}

    payload = {
        "urls": urls,
        "extract_depth": extract_depth,
        "format": fmt,
        "timeout": timeout,
    }
    if query:
        payload["query"] = query
        if chunks_per_source:
            payload["chunks_per_source"] = min(max(int(chunks_per_source), 1), 5)

    data = json.dumps(payload).encode("utf-8")
    try:
        resp = await _open_with_fallback(
            "POST", TAVILY_EXTRACT_URL,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            data=data, timeout=max(timeout + 5, 35))
        obj = resp.json()

        results = []
        for r in obj.get("results", []):
            results.append({"url": r.get("url", ""), "raw_content": r.get("raw_content", "")})

        failed = []
        for f in obj.get("failed_results", []):
            failed.append({"url": f.get("url", ""), "error": f.get("error", "extraction failed")})

        return {"results": results, "failed_results": failed}
    except Exception as e:
        log(f"Tavily Extract failed: {e}")
        return {"results": [], "failed_results": [{"url": u, "error": str(e)} for u in urls]}


# ---------------------------------------------------------------------------
# Brave LLM Context API
# ---------------------------------------------------------------------------

BRAVE_LLM_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"


async def search_brave_llm_context(query, max_results=20, max_tokens=8192,
                                   max_tokens_per_url=4096, max_snippets=50,
                                   context_threshold="balanced", freshness=None):
    """Search via Brave LLM Context API — returns pre-extracted content chunks.

    Returns (results, resp_headers) or None. Each result dict has:
      - url, title, snippet (joined snippets text)
      - snippets (list of individual extracted text chunks)
    """
    key = _load_brave_key()
    if not key:
        log("No Brave API key, skipping Brave LLM Context")
        return None

    params = {
        "q": query,
        "count": min(max_results, 50),
        "maximum_number_of_tokens": min(max(max_tokens, 1024), 32768),
        "maximum_number_of_tokens_per_url": min(max(max_tokens_per_url, 512), 8192),
        "maximum_number_of_snippets": min(max(max_snippets, 1), 100),
        "context_threshold_mode": context_threshold,
    }
    if freshness:
        params["freshness"] = freshness

    qs = urllib.parse.urlencode(params)
    try:
        resp = await _open_with_fallback(
            "GET", f"{BRAVE_LLM_CONTEXT_URL}?{qs}",
            headers={"Accept": "application/json", "Accept-Encoding": "gzip",
                     "X-Subscription-Token": key},
            timeout=30)
        resp_headers = dict(resp.headers)
        obj = resp.json()

        grounding = obj.get("grounding", {})
        generic = grounding.get("generic", [])

        results = []
        for entry in generic:
            snippets = entry.get("snippets", [])
            results.append({
                "title": entry.get("title", ""),
                "url": entry.get("url", ""),
                "snippet": "\n\n".join(snippets[:3]) if snippets else "",
                "snippets": snippets,
            })

        return (results, resp_headers) if results else None
    except Exception as e:
        log(f"Brave LLM Context failed: {e}")
        return None


def _normalize_url(url):
    """Normalize a URL for deduplication."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        host = host.lower().removeprefix("www.")
        path = parsed.path.rstrip("/")
        return f"{host}{path}"
    except Exception:
        return url.lower()


def dedup_and_rank(results_by_provider, max_results):
    """Deduplicate and rank results from multiple providers.

    Results appearing in more providers rank higher. Ties broken by first-seen order.
    Keeps the longest snippet and most descriptive title for each URL.

    Returns (merged_results, providers_used) tuple.
    """
    seen = {}
    order = 0
    for prov_name, results in results_by_provider.items():
        for r in results:
            url = r.get("url", "")
            if "vertexaisearch.cloud.google.com" in url:
                key = r.get("title", "").lower().strip()
            else:
                key = _normalize_url(url)
            if not key:
                key = f"_unknown_{order}"
            if key in seen:
                seen[key]["providers"].add(prov_name)
                if len(r.get("snippet", "")) > len(seen[key]["result"].get("snippet", "")):
                    seen[key]["result"]["snippet"] = r["snippet"]
                if len(r.get("title", "")) > len(seen[key]["result"].get("title", "")):
                    seen[key]["result"]["title"] = r["title"]
            else:
                seen[key] = {"result": dict(r), "providers": {prov_name}, "order": order}
                order += 1

    ranked = sorted(seen.values(), key=lambda x: (-len(x["providers"]), x["order"]))
    merged = []
    for entry in ranked[:max_results]:
        r = entry["result"]
        r["_providers"] = sorted(entry["providers"])
        merged.append(r)
    providers_used = sorted(results_by_provider.keys())
    return merged, providers_used


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def to_markdown(results, query, answer=None, provider=None):
    lines = []
    if provider:
        lines.append(f"*Source: {provider}*\n")
    if answer:
        lines.append(f"{answer.strip()}\n")
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip() or r.get("url") or "(no title)"
        url = r.get("url", "")
        snippet = (r.get("snippet") or "").strip()
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")
    # Append sources section for easy citation
    source_links = [f"- [{(r.get('title') or r.get('url', ''))[:60]}]({r.get('url', '')})"
                    for r in results if r.get("url")]
    if source_links:
        lines.append("Sources:")
        lines.extend(source_links)
        lines.append("")
    return "\n".join(lines).strip() + "\n"

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    asyncio.run(_async_main())


async def _async_main():
    ap = argparse.ArgumentParser(description="Unified web search (DDG -> Tavily -> Brave -> Gemini)")
    sub = ap.add_subparsers(dest="cmd")

    # --- search subcommand ---
    sp = sub.add_parser("search", help="Web search with auto-failover")
    sp.add_argument("query", help="Search query")
    sp.add_argument("--max-results", type=int, default=5)
    sp.add_argument("--region", default="wt-wt", help="DDG region (cn-zh, us-en, wt-wt)")
    sp.add_argument("--timelimit", choices=["d", "w", "m", "y"], help="Time filter")
    sp.add_argument("--news", action="store_true", help="Search news")
    sp.add_argument("--include-answer", action="store_true", help="Tavily AI answer")
    sp.add_argument("--search-depth", default="basic", choices=["basic", "advanced"])
    sp.add_argument("--topic", default="general", choices=["general", "news"])
    sp.add_argument("--days", type=int, help="Limit news to recent N days (Tavily)")
    sp.add_argument("--include-domains", nargs="+", help="Tavily domain filter")
    sp.add_argument("--exclude-domains", nargs="+", help="Tavily domain exclusion")
    sp.add_argument("--format", default="md", choices=["json", "md"])
    sp.add_argument("--provider", choices=["ddg", "tavily", "brave", "gemini", "auto"], default="auto")
    sp.add_argument("--super", action="store_true", help="Query all providers in parallel (uses quota on all)")

    # --- extract subcommand ---
    ep = sub.add_parser("extract", help="Extract full page content from URLs (trafilatura)")
    ep.add_argument("urls", nargs="+", help="URLs to extract")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        sys.exit(1)

    try:
        # --- extract ---
        if args.cmd == "extract":
            result = await extract_trafilatura(args.urls)
            if result is None:
                print(json.dumps({"error": "Extraction failed", "urls": args.urls}))
                sys.exit(1)
            json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return

        # --- search ---
        max_results = max(1, min(args.max_results, 10))
        results = None
        answer = None
        provider_used = None

        from .providers import ProviderRegistry
        _registry = ProviderRegistry()
        _registry.load()

        # Super mode: all providers in parallel
        if getattr(args, "super", False):
            max_results = max(1, min(args.max_results, 20))
            search_kwargs = {
                "region": args.region, "timelimit": args.timelimit,
                "news": args.news, "include_answer": True,
                "search_depth": args.search_depth, "topic": args.topic,
                "days": args.days, "include_domains": args.include_domains,
                "exclude_domains": args.exclude_domains,
            }
            providers = _registry.get_ordered()
            tasks = [p.search(args.query, max_results, **search_kwargs) for p in providers]
            search_results = await asyncio.gather(*tasks, return_exceptions=True)
            results_by_provider = {}
            for p, sr in zip(providers, search_results):
                if isinstance(sr, Exception):
                    continue
                if sr and sr.results:
                    results_by_provider[p.name] = sr.results
                    if sr.answer and not answer:
                        answer = sr.answer
            if results_by_provider:
                results, providers_used_list = dedup_and_rank(results_by_provider, max_results)
                provider_used = ",".join(providers_used_list)
        else:
            search_kwargs = {
                "region": args.region, "timelimit": args.timelimit,
                "news": args.news, "include_answer": args.include_answer,
                "search_depth": args.search_depth, "topic": args.topic,
                "days": args.days, "include_domains": args.include_domains,
                "exclude_domains": args.exclude_domains,
            }
            if args.provider and args.provider != "auto":
                p = _registry.get_by_name(args.provider)
                if p and p.enabled:
                    sr = await p.search(args.query, max_results, **search_kwargs)
                    if sr:
                        results = sr.results
                        answer = sr.answer
                        provider_used = sr.provider
            else:
                for p in _registry.get_ordered():
                    sr = await p.search(args.query, max_results, **search_kwargs)
                    if sr is not None:
                        results = sr.results
                        answer = sr.answer
                        provider_used = sr.provider
                        break

        if results is None:
            print(json.dumps({"error": "All providers failed", "query": args.query}))
            sys.exit(1)

        if args.format == "md":
            sys.stdout.write(to_markdown(results, args.query, answer, provider_used))
        else:
            out = {"query": args.query, "provider": provider_used, "results": results}
            if answer:
                out["answer"] = answer
            json.dump(out, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
    finally:
        await close_client()


if __name__ == "__main__":
    main()
