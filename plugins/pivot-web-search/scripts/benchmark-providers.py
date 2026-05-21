#!/usr/bin/env python3
"""Comprehensive benchmark: latency, quality, freshness, source diversity.

Runs each configured provider against a fixed query set and produces a
comparison table + JSON report at ~/.cache/pivot-web-search/benchmark-results.json.

Primary use case: sanity-check the smart-default priorities in
``defaults.SMART_DEFAULT_PRIORITY`` after provider behavior changes
(model updates, ranker swaps, latency regressions).

Usage: uv run python scripts/benchmark-providers.py

KNOWN LIMITATIONS — read before drawing conclusions:
  * Each query runs once per provider. Latency numbers are noisy;
    treat p50/p95 as ballpark only. Bump to 3+ runs for serious comparison.
  * Sample size is 7 queries. Statistically thin — fine for "is provider
    X obviously broken?" but not for SLA-grade ranking.
  * ``keyword_overlap_score`` mirrors the heuristic in ``quality_gate.py``,
    so a provider that the gate would penalize is also penalized here
    (circular). Don't use this score to justify changes to the gate itself.
  * Freshness check is "snippet/URL mentions current or last year" —
    legitimately evergreen results score 0 and that's not a defect.
  * Every run burns real API quota. Brave's 1k/month budget loses 7
    requests per run. Don't loop this in CI.
"""

import asyncio
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pivot_web_search_mcp.providers import ProviderRegistry  # noqa: E402, I001


# Diverse query set covering different intent types
QUERIES = [
    # Time-sensitive (results should be recent)
    ("Python 3.13 new features release", "time_sensitive"),
    ("Claude Code latest update May 2026", "time_sensitive"),
    # Factual / evergreen
    ("how does TCP three-way handshake work", "factual"),
    ("Python asyncio best practices", "factual"),
    # Technical comparison
    ("rust vs go performance benchmark 2025", "comparison"),
    # Niche / specific
    ("SAP BTP cloud foundry buildpack configuration", "niche"),
    # Broad / popular
    ("transformer architecture explained simply", "broad"),
]

MAX_RESULTS = 5

# For keyword overlap scoring
def extract_keywords(text):
    """Extract meaningful words from text for relevance scoring."""
    words = re.findall(r'[a-zA-Z]{3,}', text.lower())
    stop_words = {'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'had',
                  'her', 'was', 'one', 'our', 'out', 'has', 'have', 'been', 'some', 'them',
                  'than', 'its', 'over', 'also', 'that', 'this', 'with', 'from', 'will',
                  'what', 'when', 'how', 'who', 'which', 'their', 'there', 'where', 'about'}
    return set(words) - stop_words


def keyword_overlap_score(query, results):
    """Score 0-1: how many query keywords appear in results."""
    query_kw = extract_keywords(query)
    if not query_kw:
        return 1.0

    combined_text = " ".join(
        f"{r.get('title', '')} {r.get('snippet', '')}" for r in results
    ).lower()

    result_kw = extract_keywords(combined_text)
    overlap = query_kw & result_kw
    return len(overlap) / len(query_kw)


def assess_freshness(results):
    """Heuristic freshness assessment from snippets and URLs."""
    current_year = datetime.now().year
    recent_years = {str(current_year), str(current_year - 1)}

    fresh_indicators = 0
    for r in results:
        text = f"{r.get('title', '')} {r.get('snippet', '')} {r.get('url', '')}"
        if any(year in text for year in recent_years):
            fresh_indicators += 1

    return fresh_indicators / len(results) if results else 0


def domain_quality_score(results):
    """Assess domain diversity and quality."""
    domains = []
    for r in results:
        url = r.get("url", "")
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            # Remove www prefix
            if domain.startswith("www."):
                domain = domain[4:]
            domains.append(domain)
        except Exception:
            pass

    unique_domains = set(domains)
    tld_variety = set(d.split(".")[-1] for d in unique_domains if "." in d)

    # Known high-quality domains for technical queries
    quality_domains = {
        "github.com", "stackoverflow.com", "docs.python.org", "developer.mozilla.org",
        "arxiv.org", "medium.com", "dev.to", "rust-lang.org", "go.dev",
        "kubernetes.io", "docker.com", "nginx.org", "aws.amazon.com",
        "cloud.google.com", "learn.microsoft.com", "wiki.archlinux.org",
    }
    quality_hits = len(unique_domains & quality_domains)

    return {
        "unique_domains": len(unique_domains),
        "tld_variety": len(tld_variety),
        "quality_domain_hits": quality_hits,
        "domains": sorted(unique_domains),
    }


async def benchmark_provider(provider, queries, max_results=5):
    """Run a provider against each query, collect comprehensive metrics."""
    results = []

    for query_text, query_type in queries:
        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                provider.search(query_text, max_results=max_results),
                timeout=120,
            )
            elapsed = time.perf_counter() - start

            if result is None:
                results.append({
                    "query": query_text,
                    "query_type": query_type,
                    "latency_ms": round(elapsed * 1000),
                    "status": "null_response",
                    "result_count": 0,
                    "has_answer": False,
                })
            else:
                res_list = result.results if result.results else []
                has_answer = bool(getattr(result, "answer", None))
                answer_len = len(result.answer) if has_answer else 0

                # Quality metrics
                kw_score = keyword_overlap_score(query_text, res_list)
                freshness = assess_freshness(res_list)
                domain_info = domain_quality_score(res_list)

                avg_snippet_len = (
                    statistics.mean(len(r.get("snippet", "")) for r in res_list)
                    if res_list else 0
                )

                results.append({
                    "query": query_text,
                    "query_type": query_type,
                    "latency_ms": round(elapsed * 1000),
                    "status": "success",
                    "result_count": len(res_list),
                    "has_answer": has_answer,
                    "answer_length": answer_len,
                    "keyword_overlap": round(kw_score, 2),
                    "freshness_ratio": round(freshness, 2),
                    "avg_snippet_len": round(avg_snippet_len),
                    "unique_domains": domain_info["unique_domains"],
                    "tld_variety": domain_info["tld_variety"],
                    "quality_domain_hits": domain_info["quality_domain_hits"],
                    "domains": domain_info["domains"],
                })

        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - start
            results.append({
                "query": query_text,
                "query_type": query_type,
                "latency_ms": round(elapsed * 1000),
                "status": "timeout",
                "result_count": 0,
                "has_answer": False,
            })
        except Exception as e:
            elapsed = time.perf_counter() - start
            results.append({
                "query": query_text,
                "query_type": query_type,
                "latency_ms": round(elapsed * 1000),
                "status": f"error: {type(e).__name__}: {str(e)[:100]}",
                "result_count": 0,
                "has_answer": False,
            })

        # Delay between requests to avoid rate limiting
        await asyncio.sleep(1.0)

    return results


def summarize(provider_name, results):
    """Compute comprehensive summary stats."""
    successful = [r for r in results if r["status"] == "success"]
    latencies = [r["latency_ms"] for r in successful]

    summary = {
        "provider": provider_name,
        "total_runs": len(results),
        "success": len(successful),
        "null_response": len([r for r in results if r["status"] == "null_response"]),
        "errors": len([r for r in results if r["status"].startswith("error")]),
        "timeouts": len([r for r in results if r["status"] == "timeout"]),
    }

    if latencies:
        latencies_sorted = sorted(latencies)
        n = len(latencies_sorted)
        summary["latency_ms"] = {
            "min": latencies_sorted[0],
            "p25": latencies_sorted[n // 4],
            "p50": latencies_sorted[n // 2],
            "p75": latencies_sorted[3 * n // 4],
            "p95": latencies_sorted[min(int(n * 0.95), n - 1)],
            "max": latencies_sorted[-1],
            "mean": round(statistics.mean(latencies)),
            "stdev": round(statistics.stdev(latencies)) if n > 1 else 0,
        }

    if successful:
        summary["quality"] = {
            "result_count_mean": round(statistics.mean(r["result_count"] for r in successful), 1),
            "result_count_min": min(r["result_count"] for r in successful),
            "keyword_overlap_mean": round(statistics.mean(r["keyword_overlap"] for r in successful), 2),
            "keyword_overlap_min": round(min(r["keyword_overlap"] for r in successful), 2),
            "freshness_ratio_mean": round(statistics.mean(r["freshness_ratio"] for r in successful), 2),
            "unique_domains_mean": round(statistics.mean(r["unique_domains"] for r in successful), 1),
            "quality_domain_hits_mean": round(statistics.mean(r["quality_domain_hits"] for r in successful), 1),
            "avg_snippet_len_mean": round(statistics.mean(r["avg_snippet_len"] for r in successful)),
            "has_answer_count": len([r for r in successful if r["has_answer"]]),
            "answer_len_mean": round(statistics.mean(
                r["answer_length"] for r in successful if r.get("answer_length", 0) > 0
            )) if any(r.get("answer_length", 0) > 0 for r in successful) else 0,
        }

    # Per query-type breakdown
    type_groups = {}
    for r in successful:
        qt = r["query_type"]
        type_groups.setdefault(qt, []).append(r)

    summary["by_query_type"] = {}
    for qt, group in type_groups.items():
        summary["by_query_type"][qt] = {
            "count": len(group),
            "latency_p50": sorted(r["latency_ms"] for r in group)[len(group) // 2],
            "keyword_overlap_mean": round(statistics.mean(r["keyword_overlap"] for r in group), 2),
            "freshness_ratio_mean": round(statistics.mean(r["freshness_ratio"] for r in group), 2),
        }

    # Error details
    errors = [r for r in results if r["status"].startswith("error") or r["status"] == "null_response"]
    if errors:
        summary["failures"] = [{"query": r["query"], "status": r["status"]} for r in errors]

    return summary


async def main():
    print("=" * 80)
    print("PIVOT WEB SEARCH — COMPREHENSIVE PROVIDER BENCHMARK")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Queries: {len(QUERIES)} | Max results: {MAX_RESULTS}")
    print("Query types: time_sensitive, factual, comparison, niche, broad")
    print("=" * 80)

    registry = ProviderRegistry()
    registry.load()
    providers = registry.get_ordered()

    all_summaries = []
    all_raw_results = {}

    for provider in providers:
        if not provider.enabled:
            print(f"\n[SKIP] {provider.name} (disabled)")
            continue

        ok, detail = await provider.health_check()
        if not ok:
            print(f"\n[SKIP] {provider.name} (health check failed: {detail})")
            continue

        print(f"\n{'━' * 80}")
        print(f"  BENCHMARKING: {provider.name} (type={provider.provider_type})")
        print(f"{'━' * 80}")

        results = await benchmark_provider(provider, QUERIES)
        all_raw_results[provider.name] = results

        # Print detailed per-query results
        for r in results:
            if r["status"] == "success":
                freshness_bar = "█" * int(r["freshness_ratio"] * 5)
                relevance_bar = "█" * int(r["keyword_overlap"] * 5)
                print(f"  ✓ [{r['query_type']:<14}] {r['query'][:35]:<35} "
                      f"{r['latency_ms']:>5}ms  n={r['result_count']}  "
                      f"rel={r['keyword_overlap']:.0%} {relevance_bar:<5}  "
                      f"fresh={r['freshness_ratio']:.0%} {freshness_bar:<5}  "
                      f"dom={r['unique_domains']}")
            else:
                print(f"  ✗ [{r['query_type']:<14}] {r['query'][:35]:<35} "
                      f"{r['latency_ms']:>5}ms  {r['status']}")

        summary = summarize(provider.name, results)
        all_summaries.append(summary)

        # Print summary
        print(f"\n  {'─' * 60}")
        print(f"  SUMMARY: {provider.name}")
        print(f"  {'─' * 60}")
        print(f"  Reliability:    {summary['success']}/{summary['total_runs']} success"
              f"  ({summary['null_response']} null, {summary['errors']} errors, {summary['timeouts']} timeouts)")
        if "latency_ms" in summary:
            lat = summary["latency_ms"]
            print(f"  Latency:        p50={lat['p50']}ms  p95={lat['p95']}ms  max={lat['max']}ms  (σ={lat['stdev']}ms)")
        if "quality" in summary:
            q = summary["quality"]
            print(f"  Results:        mean={q['result_count_mean']}  min={q['result_count_min']}")
            print(f"  Relevance:      mean={q['keyword_overlap_mean']:.0%}  min={q['keyword_overlap_min']:.0%}")
            print(f"  Freshness:      {q['freshness_ratio_mean']:.0%} of results mention current/recent year")
            print(f"  Domains:        {q['unique_domains_mean']} unique  |  "
                  f"{q['quality_domain_hits_mean']} quality hits")
            print(f"  Snippet length: mean={q['avg_snippet_len_mean']} chars")
            if q["has_answer_count"]:
                print(f"  AI Answers:     {q['has_answer_count']}/{summary['success']}  "
                      f"(mean length={q['answer_len_mean']} chars)")

    # Write full results to JSON
    output_path = os.path.expanduser("~/.cache/pivot-web-search/benchmark-results.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "queries": [{"text": q, "type": t} for q, t in QUERIES],
            "summaries": all_summaries,
            "raw_results": all_raw_results,
        }, f, indent=2)

    # Print final comparison table
    print(f"\n\n{'=' * 80}")
    print("FINAL COMPARISON")
    print(f"{'=' * 80}")
    print(f"{'Provider':<10} {'OK':<5} {'p50':<7} {'p95':<7} {'σ':<6} "
          f"{'Relev':<7} {'Fresh':<7} {'Doms':<6} {'Snip':<6} {'Answer':<6}")
    print("─" * 80)
    for s in all_summaries:
        lat = s.get("latency_ms", {})
        q = s.get("quality", {})
        print(f"{s['provider']:<10} "
              f"{s['success']}/{s['total_runs']:<3} "
              f"{str(lat.get('p50', '-'))+'ms':<7} "
              f"{str(lat.get('p95', '-'))+'ms':<7} "
              f"{str(lat.get('stdev', '-'))+'ms':<6} "
              f"{str(round(q.get('keyword_overlap_mean', 0)*100))+'%':<7} "
              f"{str(round(q.get('freshness_ratio_mean', 0)*100))+'%':<7} "
              f"{str(q.get('unique_domains_mean', '-')):<6} "
              f"{str(q.get('avg_snippet_len_mean', '-')):<6} "
              f"{q.get('has_answer_count', 0)}")

    print(f"\n  Full results: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
