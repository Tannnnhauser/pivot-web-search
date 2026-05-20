"""Search provider adapters and registry for hot-pluggable search sources.

Public API:
  - SearchResult: unified result dataclass
  - SearchProvider: base class for all adapters
  - Built-in adapters: Ddg, Tavily, Brave, LlmSearch, Searxng, JsonApi
  - ProviderRegistry: config-driven provider management with mtime reload
"""

from .adapters import (
    BraveProvider,
    DdgProvider,
    JsonApiProvider,
    LlmSearchProvider,
    SearxngProvider,
    TavilyProvider,
)
from .base import SearchProvider, SearchResult
from .registry import ProviderRegistry

__all__ = [
    "BraveProvider",
    "DdgProvider",
    "JsonApiProvider",
    "LlmSearchProvider",
    "ProviderRegistry",
    "SearchProvider",
    "SearchResult",
    "SearxngProvider",
    "TavilyProvider",
]
