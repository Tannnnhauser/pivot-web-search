"""Content extraction fallback for JavaScript-rendered pages.

Provides configurable JS rendering backends that activate when trafilatura
returns empty content (detected as SPA shell or below threshold).

Backends:
  - none: no fallback (default)
  - playwright: local headless browser (requires optional dep)
  - tavily: remote extraction via Tavily Extract API
"""

import asyncio
import os
import re
from typing import Literal

from .logging import log

_SPA_SHELL_PATTERNS = [
    re.compile(r'<div\s+id="(?:app|root|__next|__nuxt)"[^>]*>\s*</div>', re.IGNORECASE),
    re.compile(r'<body[^>]*>\s*<(?:div|noscript)[^>]*>\s*</(?:div|noscript)>\s*</body>', re.IGNORECASE),
    re.compile(r'^\s*(?:Loading\.{0,3}|Please enable JavaScript)\s*$', re.IGNORECASE | re.MULTILINE),
]


def is_empty_content(content: str | None, threshold: int = 200) -> bool:
    """Detect if extraction result is empty or an SPA shell.

    Returns True if content should trigger a JS renderer fallback.
    """
    if not content:
        return True
    stripped = content.strip()
    if len(stripped) < threshold:
        return True
    for pat in _SPA_SHELL_PATTERNS:
        if pat.search(stripped):
            return True
    return False


WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


async def render_playwright(url: str, timeout: int = 30000, wait_until: WaitUntil = "networkidle") -> str | None:
    """Render a URL with Playwright headless browser, then extract with trafilatura.

    Returns extracted markdown content or None on failure.
    Raises RuntimeError if playwright is not installed.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright not installed. Install with: uv sync --extra browser\n"
            "(or: pip install pivot-web-search-mcp[browser])\n"
            "Then run: playwright install chromium"
        )

    try:
        import trafilatura
    except ImportError:
        raise RuntimeError("trafilatura is required for content extraction")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            html = await page.content()
            await browser.close()

        content = await asyncio.to_thread(
            trafilatura.extract, html, output_format="markdown",
            include_links=True, include_tables=True)
        return content if content else None
    except Exception as e:
        log(f"Playwright render failed for {url}: {e}")
        return None


async def render_tavily(url: str, api_key: str, extract_depth: str = "advanced",
                        fmt: str = "markdown", timeout: int = 30,
                        query: str | None = None, chunks_per_source: int | None = None) -> str | None:
    """Extract content via Tavily Extract API.

    Returns extracted content string or None on failure.
    """
    if not api_key:
        log("No TAVILY_API_KEY for fetch fallback")
        return None

    from . import search
    result = await search.extract_tavily(
        [url], extract_depth=extract_depth, fmt=fmt, timeout=timeout,
        query=query, chunks_per_source=chunks_per_source,
    )

    extracted = result.get("results", [])
    if extracted and extracted[0].get("raw_content"):
        return extracted[0]["raw_content"]
    return None


async def render_with_fallback(url: str, config: dict, query: str | None = None) -> str | None:
    """Dispatch to the configured JS renderer. Returns content or None.

    Args:
        url: The URL to extract content from
        config: Fetch config dict (from load_fetch_config)
        query: Optional query for relevance-aware extraction (Tavily only)
    """
    renderer = config.get("js_renderer", "none")

    if renderer == "none":
        return None

    if renderer == "playwright":
        pw_conf = config.get("playwright", {})
        return await render_playwright(
            url,
            timeout=pw_conf.get("timeout", 30000),
            wait_until=pw_conf.get("wait_until", "networkidle"),
        )

    if renderer == "tavily":
        tv_conf = config.get("tavily", {})
        api_key = os.environ.get("TAVILY_API_KEY", "").strip()
        return await render_tavily(
            url, api_key,
            extract_depth=tv_conf.get("extract_depth", "advanced"),
            fmt=tv_conf.get("format", "markdown"),
            timeout=tv_conf.get("timeout", 30),
            query=query,
            chunks_per_source=tv_conf.get("chunks_per_source"),
        )

    log(f"Unknown js_renderer: {renderer}")
    return None
