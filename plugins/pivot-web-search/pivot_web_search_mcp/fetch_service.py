"""URL extraction orchestration shared by every Pivot interface."""

from __future__ import annotations

from dataclasses import dataclass

from . import fetch as _fetch
from .config import load_fetch_config
from .extraction import extract_trafilatura
from .validation import MAX_CONTENT_CHARS, validate_url


@dataclass(frozen=True)
class FetchRequest:
    """Complete interface-neutral input for one fetch operation."""

    urls: list[str]
    query: str | None = None
    max_chars: int | None = None


@dataclass(frozen=True)
class FetchItem:
    """Extracted content or a sanitized failure for one requested URL."""

    url: str
    content: str | None = None
    error: str | None = None
    truncated: bool = False
    status_code: int | None = None


@dataclass(frozen=True)
class FetchResponse:
    """Ordered results for one fetch request."""

    items: list[FetchItem]

    @property
    def extracted_count(self) -> int:
        return sum(item.content is not None for item in self.items)


class FetchServiceError(Exception):
    """Stable request or execution failure raised by the shared fetch service."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FetchService:
    """Validate, extract, fall back, and truncate URL content."""

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        if not request.urls or all(not isinstance(url, str) or not url.strip() for url in request.urls):
            raise FetchServiceError("INVALID_REQUEST", "Empty URL")
        if request.max_chars is not None and request.max_chars <= 0:
            raise FetchServiceError("INVALID_REQUEST", "max_chars must be a positive integer")

        normalized: list[tuple[str, str | None]] = []
        valid_urls: list[str] = []
        for raw_url in request.urls:
            if not isinstance(raw_url, str) or not raw_url.strip():
                normalized.append((str(raw_url), "Empty URL"))
                continue
            try:
                url = validate_url(raw_url)
            except ValueError as error:
                normalized.append((raw_url, str(error)))
                continue
            normalized.append((url, None))
            valid_urls.append(url)

        config = load_fetch_config()
        truncation_limit = request.max_chars or config.get("max_chars", MAX_CONTENT_CHARS)
        empty_threshold = config.get("empty_threshold", 200)
        extraction = await extract_trafilatura(valid_urls) if valid_urls else {"results": [], "failed_results": []}
        extracted = {
            item["url"]: item for item in extraction.get("results", []) if item.get("url") and item.get("raw_content")
        }
        failures = {item["url"]: item for item in extraction.get("failed_results", []) if item.get("url")}

        items: list[FetchItem] = []
        for url, validation_error in normalized:
            if validation_error is not None:
                items.append(FetchItem(url=url, error=validation_error))
                continue

            extracted_item = extracted.get(url, {})
            failed_item = failures.get(url, {})
            content = extracted_item.get("raw_content", "")
            if _fetch.is_empty_content(content, threshold=empty_threshold) or url in failures:
                fallback = await _fetch.render_with_fallback(url, config, query=request.query)
                if fallback:
                    content = fallback

            if not content:
                items.append(
                    FetchItem(
                        url=failed_item.get("final_url") or url,
                        error=failed_item.get("error", "extraction returned empty"),
                        status_code=failed_item.get("status_code"),
                    )
                )
                continue

            truncated = len(content) > truncation_limit
            if truncated:
                content = content[:truncation_limit]
            items.append(
                FetchItem(
                    url=extracted_item.get("final_url") or url,
                    content=content,
                    truncated=truncated,
                    status_code=extracted_item.get("status_code") or 200,
                )
            )

        return FetchResponse(items=items)
