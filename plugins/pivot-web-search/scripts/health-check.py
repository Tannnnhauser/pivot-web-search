#!/usr/bin/env python3
"""Pivot Web Search — SessionStart health check.

Runs at session startup to probe provider availability and warm the proxy cache.
Output goes to stdout (shown in session start). Exits 0 always (never blocks startup).
"""

import asyncio
import os
import sys
import time

# Ensure the project root is on sys.path so the package is importable
_PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

TIMEOUT = 5


async def _check_providers():
    """Check all configured providers via the registry."""
    from pivot_web_search_mcp.providers import ProviderRegistry
    registry = ProviderRegistry()
    registry.load()

    from pivot_web_search_mcp import quota
    try:
        await quota.sync_tavily_usage()
    except Exception:
        pass

    quota_data = {}
    try:
        quota_data = quota.load_quota()
    except Exception:
        pass

    results = []
    for p in registry.get_all():
        try:
            available, detail = await p.health_check()
            # Append quota info if available
            q = quota_data.get(p.name, {})
            if q.get("limit") and q.get("limit") > 0:
                used = q.get("used", 0)
                limit = q["limit"]
                detail_suffix = f"{used}/{limit} used"
                if detail:
                    detail = f"{detail}, {detail_suffix}"
                else:
                    detail = detail_suffix
            results.append((p.name.upper(), available, detail))
        except Exception as e:
            results.append((p.name.upper(), False, str(e)[:80]))
    return results


async def _check_proxy():
    """Quick probe to warm proxy cache using the shared proxy-fallback layer."""
    from pivot_web_search_mcp import search as s
    try:
        await s._open_with_fallback("HEAD", "https://duckduckgo.com/", timeout=3)
        host = "duckduckgo.com"
        winner = s._proxy_cache.get(host)
        return ("Proxy", True, winner or "direct")
    except Exception as e:
        return ("Proxy", False, str(e)[:80])


async def _async_main():
    start = time.time()

    provider_task = asyncio.create_task(_check_providers())
    proxy_task = asyncio.create_task(_check_proxy())

    try:
        provider_results = await asyncio.wait_for(provider_task, timeout=TIMEOUT)
    except (asyncio.TimeoutError, Exception) as e:
        provider_results = [("Providers", False, str(e)[:80])]

    try:
        proxy_result = await asyncio.wait_for(proxy_task, timeout=TIMEOUT)
    except (asyncio.TimeoutError, Exception) as e:
        proxy_result = ("Proxy", False, str(e)[:80])

    all_results = provider_results + [proxy_result]
    elapsed = time.time() - start

    ok = []
    warn = []
    for name, available, detail in sorted(all_results, key=lambda x: x[0]):
        if available:
            extra = f" ({detail})" if detail else ""
            ok.append(f"{name}{extra}")
        else:
            warn.append(f"{name}: {detail}")

    parts = [f"[pivot-web-search] {len(ok)}/{len(all_results)} providers ready ({elapsed:.1f}s)"]
    if ok:
        parts.append(f"  OK: {', '.join(ok)}")
    if warn:
        parts.append(f"  WARN: {'; '.join(warn)}")

    print("\n".join(parts))

    from pivot_web_search_mcp import search as s
    await s.close_client()


def main():
    asyncio.run(_async_main())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[pivot-web-search] health check failed: {e}")
