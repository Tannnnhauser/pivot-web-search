"""Search backends: DDG, Tavily, Brave, and Brave LLM Context."""

import asyncio
import json
import time
import urllib.parse

import httpx

from .http_client import (
    _get_proxies,
    _open_with_fallback,
    _proxy_cache,
    _proxy_cache_lock,
    _record_proxy_success,
)
from .logging import log
from .validation import _load_env_key

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DDG_BACKENDS = ["auto", "lite", "html"]
_DDG_RETRY_DELAY = 0.3
TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_LLM_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"

# ---------------------------------------------------------------------------
# DuckDuckGo
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
    await _record_proxy_success(ddg_host, proxy)
    return [{"title": r.get("title", ""),
             "url": r.get("href", r.get("url", "")),
             "snippet": r.get("body", r.get("content", ""))}
            for r in raw_results]


# ---------------------------------------------------------------------------
# Tavily
# ---------------------------------------------------------------------------


async def search_tavily(query, max_results=5, include_answer=False, search_depth="basic",
                        topic="general", days=None, include_domains=None, exclude_domains=None):
    """Tavily Search API."""
    key = _load_env_key("TAVILY_API_KEY")
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
    except httpx.HTTPStatusError as e:
        log(f"Tavily failed: {e}")
        if e.response is not None and e.response.status_code == 429:
            raise
        return None
    except Exception as e:
        log(f"Tavily failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Brave Web Search
# ---------------------------------------------------------------------------


async def search_brave(query, max_results=5):
    """Brave Search API. Returns (results, headers_dict) or None."""
    key = _load_env_key("BRAVE_API_KEY")
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
    except httpx.HTTPStatusError as e:
        log(f"Brave failed: {e}")
        if e.response is not None and e.response.status_code == 429:
            raise
        return None
    except Exception as e:
        log(f"Brave failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Brave LLM Context API
# ---------------------------------------------------------------------------


async def search_brave_llm_context(query, max_results=20, max_tokens=8192,
                                   max_tokens_per_url=4096, max_snippets=50,
                                   context_threshold="balanced", freshness=None):
    """Search via Brave LLM Context API — returns pre-extracted content chunks.

    Returns (results, resp_headers) or None. Each result dict has:
      - url, title, snippet (joined snippets text)
      - snippets (list of individual extracted text chunks)
    """
    key = _load_env_key("BRAVE_API_KEY")
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
