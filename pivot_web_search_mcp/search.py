"""Backward-compatibility facade — re-exports all public and semi-public symbols.

The actual implementations live in focused modules:
  http_client  — HTTP singleton, proxy cache, _open_with_fallback
  validation   — URL validation, content-type detection, key loaders
  backends     — search_ddg, search_tavily, search_brave, search_brave_llm_context
  extraction   — _fetch_url, extract_trafilatura, extract_tavily, fetch cache
  results      — dedup_and_rank, to_markdown
"""

import argparse
import asyncio
import json
import sys

from .backends import (  # noqa: F401
    _DDG_RETRY_DELAY,
    BRAVE_LLM_CONTEXT_URL,
    BRAVE_URL,
    DDG_BACKENDS,
    TAVILY_URL,
    search_brave,
    search_brave_llm_context,
    search_ddg,
    search_tavily,
)
from .extraction import (  # noqa: F401
    FETCH_CACHE_MAX,
    FETCH_CACHE_MAX_BYTES,
    FETCH_CACHE_TTL,
    TAVILY_EXTRACT_URL,
    _extract_nextjs_data,
    _fetch_cache,
    _fetch_cache_lock,
    _fetch_url,
    _FetchCacheEntry,
    extract_tavily,
    extract_trafilatura,
)
from .http_client import (  # noqa: F401
    _PROXY_CACHE_FILE,
    _PROXY_CACHE_FILE_LOCK,
    _PROXY_CACHE_MAX,
    _PROXY_CACHE_TTL,
    _SSL_VERIFY,
    PROXIES,
    CrossHostRedirect,
    _do_request,
    _get_client,
    _get_proxies,
    _load_proxy_cache,
    _open_with_fallback,
    _proxy_cache,
    _proxy_cache_lock,
    _proxy_cache_ts,
    _save_proxy_cache,
    _save_proxy_cache_sync,
    close_client,
)
from .results import (  # noqa: F401
    _normalize_url,
    dedup_and_rank,
    to_markdown,
)
from .validation import (  # noqa: F401
    BINARY_CONTENT_TYPES,
    MAX_CONTENT_CHARS,
    MAX_FETCH_BYTES,
    _is_binary_content_type,
    _load_brave_key,
    _load_tavily_key,
    validate_url,
)


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
                if isinstance(sr, BaseException) or sr is None:
                    continue
                if sr.results:
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
