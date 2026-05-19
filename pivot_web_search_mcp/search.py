"""Backward-compatibility facade — re-exports symbols still referenced by tests.

The actual implementations live in focused modules:
  http_client  — HTTP singleton, proxy cache, _open_with_fallback
  validation   — URL validation, content-type detection, key loaders
  backends     — search_ddg, search_tavily, search_brave, search_brave_llm_context
  extraction   — _fetch_url, extract_trafilatura, extract_tavily, fetch cache
  results      — dedup_and_rank, to_markdown
  cli          — argparse front-end (run via `python -m pivot_web_search_mcp.cli`)
"""

from .backends import (  # noqa: F401
    search_brave,
    search_brave_llm_context,
    search_ddg,
    search_tavily,
)
from .extraction import (  # noqa: F401
    _fetch_cache,
    _fetch_url,
    extract_tavily,
    extract_trafilatura,
)
from .http_client import (  # noqa: F401
    CrossHostRedirect,
    _open_with_fallback,
    _proxy_cache,
    _proxy_cache_ts,
)
from .results import (  # noqa: F401
    _normalize_url,
    dedup_and_rank,
    to_markdown,
)
from .validation import (  # noqa: F401
    MAX_CONTENT_CHARS,
    _is_binary_content_type,
    validate_url,
)
