# Gemini Search Grounding API Reference

## Endpoint

`POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}`

## Model

`gemini-2.5-flash` — best quality for grounding. Free tier RPD varies by account tier (check [AI Studio](https://aistudio.google.com/rate-limit) for current limits).

## Request Payload

```json
{
  "contents": [{"parts": [{"text": "Search the web for: {query}"}]}],
  "tools": [{"google_search": {}}]
}
```

## Response Structure

| Path | Description |
|------|-------------|
| `candidates[0].content.parts[0].text` | AI-generated answer |
| `candidates[0].groundingMetadata.groundingChunks[i].web.uri` | Result URL (opaque redirect via `vertexaisearch.cloud.google.com`) |
| `candidates[0].groundingMetadata.groundingChunks[i].web.title` | Domain-only title (e.g. "realpython.com") |
| `candidates[0].groundingMetadata.groundingSupports[i].segment.text` | Snippet text |
| `candidates[0].groundingMetadata.groundingSupports[i].groundingChunkIndices` | Which chunks this snippet supports |

## Quirks

- **URLs are opaque redirects** through `vertexaisearch.cloud.google.com`, not direct page URLs.
- **Titles are domain-only**, not full page titles.
- **Snippets** must be assembled from `groundingSupports` by matching `groundingChunkIndices` to chunk positions.
- **Typical latency**: 7-20s (uses Google Search grounding via Gemini model).
- **Auth**: `GEMINI_SEARCH_API_KEY` env var.

## Quota

Free tier RPD varies by account and model (Google adjusts these periodically). Check your actual limits at [AI Studio Rate Limits](https://aistudio.google.com/rate-limit). Use `PIVOT_WEB_SEARCH_GEMINI_QUOTA` to set a local daily cap matching your account's limit.
