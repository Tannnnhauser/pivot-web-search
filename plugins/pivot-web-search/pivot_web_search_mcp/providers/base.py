"""Base classes for search providers."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..defaults import DEFAULT_TIMEOUT


@dataclass
class SearchResult:
    """Unified result from any search provider."""
    results: list = field(default_factory=list)
    provider: str = ""
    answer: str | None = None


class SearchProvider:
    """Base class for search provider adapters."""

    name: str = ""
    provider_type: str = ""
    priority: int = 100
    enabled: bool = True

    def __init__(self, name, priority=100, enabled=True, config=None):
        self.name = name
        self.priority = priority
        self.enabled = enabled
        self.config = config or {}
        self._effective_priority: int | None = None
        self._rr_seed: int = 0

    async def search(self, query, max_results=5, **kwargs):
        """Returns SearchResult or None on failure."""
        raise NotImplementedError

    async def health_check(self) -> tuple[bool, str | None]:
        """Returns (available: bool, detail_or_error: str | None)."""
        return False, "not implemented"

    @property
    def affinity(self) -> str:
        """Provider affinity: 'general' or 'deep'."""
        val = self.config.get("affinity", "general")
        return val if val in ("general", "deep") else "general"

    @property
    def timeout_seconds(self) -> float:
        """Per-provider timeout in seconds."""
        explicit = self.config.get("timeout")
        if explicit is not None:
            return float(explicit)
        return DEFAULT_TIMEOUT.get(self.provider_type, 6)

    @property
    def effective_priority(self) -> int:
        """Priority used for routing (smart default or explicit)."""
        if self._effective_priority is not None:
            return self._effective_priority
        return self.priority

    def __repr__(self):
        state = "on" if self.enabled else "off"
        return f"<{self.__class__.__name__} {self.name!r} pri={self.priority} {state}>"
