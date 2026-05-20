"""Result processing: deduplication, ranking, and markdown formatting."""

import urllib.parse


def _normalize_url(url):
    """Normalize a URL for deduplication."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


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


def to_markdown(results, query, answer=None, provider=None, content_downgrade_reason=None):
    lines = []
    if provider:
        lines.append(f"*Source: {provider}*\n")
    if content_downgrade_reason:
        lines.append(
            f"*Note: include_content downgraded — {content_downgrade_reason}; "
            "returning titles+snippets only.*\n"
        )
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
