"""Text and JSON-ready projections for shared application responses."""

from __future__ import annotations

from .fetch_service import FetchResponse
from .results import to_markdown
from .search_service import SearchResponse


def format_search_markdown(response: SearchResponse) -> str:
    if not response.content_included:
        return to_markdown(
            response.results,
            response.query,
            response.answer,
            response.provider,
            response.content_downgrade_reason,
        )

    parts = ["*Source: brave-llm-context*\n"]
    for index, result in enumerate(response.results, 1):
        title = result.get("title", "Untitled")
        url = result.get("url", "")
        snippets = result.get("snippets", [])
        content = "\n\n".join(snippets) if snippets else result.get("snippet", "")
        parts.append(f"{index}. **{title}**\n   {url}\n\n{content}\n")
    parts.append("\nSources:")
    for result in response.results:
        parts.append(f"- [{result.get('title', 'Untitled')}]({result.get('url', '')})")
    return "\n".join(parts)


def fetch_response_dict(response: FetchResponse) -> dict:
    return {
        "results": [
            {
                "url": item.url,
                **({"content": item.content} if item.content is not None else {}),
                **({"error": item.error} if item.error is not None else {}),
                "truncated": item.truncated,
            }
            for item in response.items
        ],
        "extracted": response.extracted_count,
        "requested": len(response.items),
    }


def format_fetch_markdown(response: FetchResponse) -> str:
    if len(response.items) == 1:
        item = response.items[0]
        if item.error is not None:
            return ""
        suffix = "\n\n[Content truncated due to length...]" if item.truncated else ""
        return f"Source: {item.url}\n\n---\n\n{item.content}{suffix}"

    parts = []
    for item in response.items:
        if item.error is not None:
            parts.append(f"## {item.url}\n\n[Error: {item.error}]")
        else:
            suffix = "\n\n[Content truncated due to length...]" if item.truncated else ""
            parts.append(f"## {item.url}\n\n{item.content}{suffix}")
    header = f"URLs extracted: {response.extracted_count}/{len(response.items)}"
    return f"{header}\n\n---\n\n" + "\n\n---\n\n".join(parts)
