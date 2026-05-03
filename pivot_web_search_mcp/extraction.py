"""Content extraction: URL fetching, trafilatura, Tavily Extract, and fetch cache."""

import asyncio
import collections
import json
import re
import time

from .validation import (
    MAX_FETCH_BYTES,
    _is_binary_content_type,
    _load_tavily_key,
    validate_url,
)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    from .logging import log
except ImportError:
    import sys

    def log(msg):
        print(f"[pivot-web-search] {msg}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Fetch cache (in-memory LRU with TTL)
# ---------------------------------------------------------------------------

FETCH_CACHE_TTL = 15 * 60  # 15 minutes
FETCH_CACHE_MAX = 64  # max cached URLs
FETCH_CACHE_MAX_BYTES = 50 * 1024 * 1024  # 50 MB total memory budget

_FetchCacheEntry = collections.namedtuple("_FetchCacheEntry", ["content", "content_type", "ts"])
_fetch_cache: collections.OrderedDict = collections.OrderedDict()
_fetch_cache_lock = asyncio.Lock()
_fetch_cache_bytes: int = 0

# ---------------------------------------------------------------------------
# Tavily Extract API
# ---------------------------------------------------------------------------

TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"

# ---------------------------------------------------------------------------
# URL fetching
# ---------------------------------------------------------------------------


async def _fetch_url(url, timeout=30):
    """Fetch raw content from a URL using the proxy-fallback layer.

    Returns (body_bytes, content_type) or (None, error_string) on failure.
    Enforces: scheme validation, size cap, binary detection, redirect safety, caching.
    """
    from . import search as s

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
        resp = await s._open_with_fallback(
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
            global _fetch_cache_bytes
            entry = _FetchCacheEntry(body, ct, time.time())
            entry_size = len(body)
            _fetch_cache[url] = entry
            _fetch_cache.move_to_end(url)
            _fetch_cache_bytes += entry_size
            # Evict by count
            while len(_fetch_cache) > FETCH_CACHE_MAX:
                _, evicted = _fetch_cache.popitem(last=False)
                _fetch_cache_bytes -= len(evicted.content) if evicted.content else 0
            # Evict by size budget
            while _fetch_cache_bytes > FETCH_CACHE_MAX_BYTES and _fetch_cache:
                _, evicted = _fetch_cache.popitem(last=False)
                _fetch_cache_bytes -= len(evicted.content) if evicted.content else 0

        return body, ct

    except s.CrossHostRedirect as e:
        return None, f"cross-host redirect blocked; re-request with: {e.location}"
    except httpx.HTTPStatusError as e:
        log(f"fetch HTTP {e.response.status_code} for {url}")
        return None, f"HTTP {e.response.status_code}"
    except Exception as e:
        log(f"fetch failed for {url}: {e}")
        return None, str(e)


# ---------------------------------------------------------------------------
# Next.js / Nuxt.js data extraction
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Trafilatura extraction
# ---------------------------------------------------------------------------


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
            from . import search as s
            raw, ct_or_err = await s._fetch_url(url)
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


# ---------------------------------------------------------------------------
# Tavily Extract API
# ---------------------------------------------------------------------------


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
        from . import search as s
        resp = await s._open_with_fallback(
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
