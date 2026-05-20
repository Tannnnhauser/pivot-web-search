# Tavily API Reference

## Search Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | yes | Search query |
| `search_depth` | string | no | `"basic"` (default) or `"advanced"` (slower, more thorough) |
| `topic` | string | no | `"general"` (default) or `"news"` |
| `days` | number | no | For news topic, limit to recent N days |
| `max_results` | number | no | Max results (default 5, max 20) |
| `include_domains` | string[] | no | Only include results from these domains |
| `exclude_domains` | string[] | no | Exclude results from these domains |
| `include_answer` | boolean | no | Include AI-generated answer summary |
