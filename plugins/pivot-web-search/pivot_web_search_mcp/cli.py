"""Command-line interface for pivot-web-search.

Usage:
  python -m pivot_web_search_mcp.cli search <query> [...flags]
  python -m pivot_web_search_mcp.cli extract <urls...>
"""

import argparse
import asyncio
import json
import sys

from .extraction import extract_trafilatura
from .http_client import close_client
from .providers import ProviderRegistry
from .results import dedup_and_rank, to_markdown


def main():
    asyncio.run(_async_main())


async def _async_main():
    ap = argparse.ArgumentParser(description="Unified web search (DDG -> Tavily -> Brave -> Gemini)")
    sub = ap.add_subparsers(dest="cmd")

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
    sp.add_argument("--provider", default="auto",
                    help="Provider name (auto, ddg, tavily, brave, gemini, or any name from providers.yaml)")
    sp.add_argument("--super", action="store_true", help="Query all providers in parallel (uses quota on all)")

    ep = sub.add_parser("extract", help="Extract full page content from URLs (trafilatura)")
    ep.add_argument("urls", nargs="+", help="URLs to extract")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        sys.exit(1)

    try:
        if args.cmd == "extract":
            result = await extract_trafilatura(args.urls)
            if result is None:
                print(json.dumps({"error": "Extraction failed", "urls": args.urls}))
                sys.exit(1)
            json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return

        max_results = max(1, min(args.max_results, 10))
        results = None
        answer = None
        provider_used = None

        registry = ProviderRegistry()
        registry.load()

        if getattr(args, "super", False):
            max_results = max(1, min(args.max_results, 20))
            search_kwargs = {
                "region": args.region, "timelimit": args.timelimit,
                "news": args.news, "include_answer": True,
                "search_depth": args.search_depth, "topic": args.topic,
                "days": args.days, "include_domains": args.include_domains,
                "exclude_domains": args.exclude_domains,
            }
            providers = registry.get_ordered()
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
                p = registry.get_by_name(args.provider)
                if not p:
                    available = [x.name for x in registry.get_all()]
                    print(json.dumps({"error": f"Unknown provider '{args.provider}'", "available": available}))
                    sys.exit(1)
                if p.enabled:
                    sr = await p.search(args.query, max_results, **search_kwargs)
                    if sr:
                        results = sr.results
                        answer = sr.answer
                        provider_used = sr.provider
            else:
                for p in registry.get_ordered():
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
