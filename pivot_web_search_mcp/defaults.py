"""Pure data: smart-default priorities and per-provider timeouts.

Lives in its own module so `providers` and `routing` can both import these
constants without forming a dependency cycle. No behavior here.

Priority numbers follow design Sec 7.2:
  10 = tier-1 (premium / latency-budget owners)
  20 = tier-2 (paid quota-managed)
  30 = tier-3 (self-hosted / json_api)
  90 = tier-4 (free, possibly unreliable)
"""

from __future__ import annotations

SMART_DEFAULT_PRIORITY: dict[str, int] = {
    "llm_search": 10,
    "tavily": 20,
    "brave": 20,
    "gemini": 20,
    "searxng": 30,
    "json_api": 30,
    "ddg": 90,
}

DEFAULT_TIMEOUT: dict[str, float] = {
    "brave": 4,
    "tavily": 4,
    "ddg": 6,
    "searxng": 6,
    "json_api": 6,
    "llm_search": 15,
    "gemini": 20,
}
