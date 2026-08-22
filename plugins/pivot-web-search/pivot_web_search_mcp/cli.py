"""Human-facing command-line interface for Pivot Web Search."""

import argparse
import asyncio
import json
import sys

from .config_service import ConfigService, ConfigServiceError
from .fetch_service import FetchRequest, FetchService, FetchServiceError
from .http_client import close_client
from .presentation import fetch_response_dict, format_fetch_markdown, format_search_markdown
from .search_service import SearchRequest, SearchService, SearchServiceError


def main():
    asyncio.run(_async_main())


async def _async_main():
    parser = argparse.ArgumentParser(description="Unified web search (DDG -> Tavily -> Brave -> Gemini)")
    sub = parser.add_subparsers(dest="cmd")

    search = sub.add_parser("search", help="Web search with auto-failover")
    search.add_argument("query", help="Search query")
    search.add_argument("--max-results", type=int, default=5)
    search.add_argument("--region", default="wt-wt", help="DDG region (cn-zh, us-en, wt-wt)")
    search.add_argument("--timelimit", choices=["d", "w", "m", "y"], help="Time filter")
    search.add_argument("--news", action="store_true", default=None, help="Search news")
    search.add_argument("--include-answer", action="store_true", help="Include an answer when supported")
    search.add_argument("--search-depth", default="basic", choices=["basic", "advanced"])
    search.add_argument("--topic", default="general", choices=["general", "news"])
    search.add_argument("--days", type=int, help="Limit news to recent N days")
    search.add_argument("--include-domains", nargs="+", help="Domain allowlist")
    search.add_argument("--exclude-domains", nargs="+", help="Domain blocklist")
    search.add_argument("--include-content", action="store_true", help="Return pre-extracted page content")
    search.add_argument("--max-content-tokens", type=int, default=8192, help="Content token budget")
    search.add_argument("--format", default="md", choices=["json", "md"])
    search.add_argument("--provider", default="auto", help="Configured provider name or auto")
    search.add_argument("--super", action="store_true", help="Query all providers in parallel")

    fetch = sub.add_parser(
        "fetch",
        aliases=["extract"],
        help="Fetch and extract full page content; extract is a compatibility alias",
    )
    fetch.add_argument("urls", nargs="+", help="URLs to fetch and extract")
    fetch.add_argument("--query", help="Optional relevance query for fallback renderers")
    fetch.add_argument("--max-chars", type=int, help="Maximum extracted characters per URL")
    fetch.add_argument("--format", default="json", choices=["json", "md"])

    config = sub.add_parser("config", help="Inspect or reload runtime configuration")
    config.add_argument("action", nargs="?", default="status", choices=["status", "reload"])

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        raise SystemExit(1)

    try:
        if args.cmd in ("fetch", "extract"):
            await _run_fetch(args)
            return
        if args.cmd == "config":
            await _run_config(args)
            return
        await _run_search(args)
    finally:
        await close_client()


async def _run_fetch(args):
    try:
        response = await FetchService().fetch(FetchRequest(urls=args.urls, query=args.query, max_chars=args.max_chars))
    except FetchServiceError as error:
        print(json.dumps({"error": str(error), "urls": args.urls}, ensure_ascii=False))
        raise SystemExit(1) from error
    if args.format == "md":
        sys.stdout.write(format_fetch_markdown(response) + "\n")
    else:
        json.dump(fetch_response_dict(response), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    if response.extracted_count == 0:
        raise SystemExit(1)


async def _run_config(args):
    search_service = SearchService()
    try:
        result = await ConfigService(search_service.registry, search_service.breaker).execute(args.action)
    except ConfigServiceError as error:
        print(json.dumps({"error": str(error), "action": args.action}, ensure_ascii=False))
        raise SystemExit(1) from error
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


async def _run_search(args):
    service = SearchService()
    try:
        response = await service.search(
            SearchRequest(
                query=args.query,
                max_results=args.max_results,
                provider=args.provider,
                mode="super" if args.super else "normal",
                news=args.news,
                timelimit=args.timelimit,
                include_answer=args.include_answer,
                search_depth=args.search_depth,
                topic=args.topic,
                days=args.days,
                allowed_domains=args.include_domains,
                blocked_domains=args.exclude_domains,
                include_content=args.include_content,
                max_content_tokens=args.max_content_tokens,
                region=args.region,
            )
        )
    except SearchServiceError as error:
        unknown = next(
            (failure for failure in error.failures if str(failure.get("error", "")).startswith("unknown provider")),
            None,
        )
        if unknown is not None:
            available = [provider.name for provider in service.registry.get_all()]
            print(json.dumps({"error": f"Unknown provider '{args.provider}'", "available": available}))
        elif error.code == "INVALID_REQUEST":
            print(json.dumps({"error": str(error), "query": args.query}))
        else:
            print(json.dumps({"error": "All providers failed", "query": args.query}))
        raise SystemExit(1) from error

    if args.format == "md":
        sys.stdout.write(format_search_markdown(response))
    else:
        output = {"query": args.query, "provider": response.provider, "results": response.results}
        if response.answer:
            output["answer"] = response.answer
        json.dump(output, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
