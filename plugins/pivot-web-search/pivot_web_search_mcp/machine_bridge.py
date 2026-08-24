"""One-shot, host-neutral JSON bridge for external adapters.

The bridge is deliberately not an MCP tool. It gives host plugins a small
structured interface without making them parse Pivot's Markdown presentation.
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Literal

from .fetch_service import FetchRequest, FetchResponse, FetchService, FetchServiceError
from .http_client import close_client
from .logging import log
from .search_service import SearchRequest, SearchResponse, SearchService, SearchServiceError

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_QUERY_CHARS = 4096
MAX_RESULTS = 20

_search_service = SearchService()
_fetch_service = FetchService()


@dataclass(frozen=True)
class ProtocolError(Exception):
    code: str
    message: str
    retryable: bool
    exit_code: int


def _validate_request(payload) -> tuple[Literal["search"], SearchRequest] | tuple[Literal["fetch"], FetchRequest]:
    if not isinstance(payload, dict):
        raise ProtocolError("INVALID_REQUEST", "Request must be a JSON object", False, 2)
    if set(payload) - {"protocolVersion", "method", "params"}:
        raise ProtocolError("INVALID_REQUEST", "Request contains unsupported fields", False, 2)
    if payload.get("protocolVersion") != PROTOCOL_VERSION:
        raise ProtocolError("UNSUPPORTED_PROTOCOL", "Unsupported protocol version", False, 2)

    method = payload.get("method")
    if method not in ("search", "fetch"):
        raise ProtocolError("UNSUPPORTED_METHOD", "Unsupported method", False, 2)
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ProtocolError("INVALID_REQUEST", "params must be a JSON object", False, 2)

    if method == "fetch":
        if set(params) - {"url"}:
            raise ProtocolError("INVALID_REQUEST", "params contains unsupported fields", False, 2)
        url = params.get("url")
        if not isinstance(url, str) or not url.strip() or len(url) > 8192:
            raise ProtocolError(
                "INVALID_REQUEST",
                "url must be a non-empty string of at most 8192 characters",
                False,
                2,
            )
        return "fetch", FetchRequest(urls=[url])

    if set(params) - {"query", "maxResults", "mode"}:
        raise ProtocolError("INVALID_REQUEST", "params contains unsupported fields", False, 2)
    query = params.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ProtocolError("INVALID_REQUEST", "query must be a non-empty string of at most 4096 characters", False, 2)
    max_results = params.get("maxResults", 5)
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= MAX_RESULTS:
        raise ProtocolError("INVALID_REQUEST", "maxResults must be an integer from 1 to 20", False, 2)
    mode = params.get("mode", "normal")
    if mode not in ("normal", "super"):
        raise ProtocolError("INVALID_REQUEST", "mode must be normal or super", False, 2)
    return "search", SearchRequest(query=query, max_results=max_results, mode=mode)


def _optional_text(source: dict, *names: str, limit: int) -> str | None:
    for name in names:
        value = source.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:limit]
    return None


def _normalize_source(source) -> dict | None:
    if not isinstance(source, dict):
        return None
    url = source.get("url")
    if not isinstance(url, str) or len(url) > 8192:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None

    normalized = {"url": url}
    title = _optional_text(source, "title", limit=1024)
    snippet = _optional_text(source, "snippet", limit=20_000)
    published_at = _optional_text(source, "publishedAt", "published_at", "published_date", limit=128)
    if title is not None:
        normalized["title"] = title
    if snippet is not None:
        normalized["snippet"] = snippet
    if published_at is not None:
        normalized["publishedAt"] = published_at
    return normalized


def _search_success(response: SearchResponse) -> dict:
    result = {
        "sources": [source for item in response.results if (source := _normalize_source(item)) is not None],
        "truncated": False,
    }
    if isinstance(response.answer, str) and response.answer.strip():
        result["content"] = response.answer.strip()[:100_000]
    return {"protocolVersion": PROTOCOL_VERSION, "ok": True, "result": result}


def _fetch_success(response: FetchResponse) -> dict:
    item = response.items[0]
    if item.error is not None:
        match = re.fullmatch(r"HTTP (\d{3})", item.error)
        status_code = item.status_code or (int(match.group(1)) if match is not None else None)
        if status_code is None:
            raise ProtocolError("FETCH_FAILED", "Fetch failed", True, 3)
        content = ""
    else:
        status_code = item.status_code or 200
        content = item.content or ""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "ok": True,
        "result": {
            "url": item.final_url or item.url,
            "statusCode": status_code,
            "body": {"kind": "text", "content": content},
            "truncated": item.truncated,
        },
    }


def _failure(error: ProtocolError) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "ok": False,
        "error": {"code": error.code, "message": error.message, "retryable": error.retryable},
    }


async def handle_payload(
    payload,
    search_service: SearchService | None = None,
    fetch_service: FetchService | None = None,
) -> tuple[dict, int]:
    """Validate and execute one decoded protocol request."""
    try:
        method, request = _validate_request(payload)
        if method == "search":
            response = await (search_service or _search_service).search(request)
            return _search_success(response), 0
        response = await (fetch_service or _fetch_service).fetch(request)
        return _fetch_success(response), 0
    except ProtocolError as error:
        return _failure(error), error.exit_code
    except SearchServiceError:
        error = ProtocolError("SEARCH_FAILED", "Search failed", True, 3)
        return _failure(error), error.exit_code
    except FetchServiceError:
        error = ProtocolError("FETCH_FAILED", "Fetch failed", True, 3)
        return _failure(error), error.exit_code
    except asyncio.CancelledError:
        error = ProtocolError("CANCELLED", "Operation cancelled", True, 130)
        return _failure(error), error.exit_code
    except Exception as error:
        log(f"Machine bridge internal failure: {type(error).__name__}")
        protocol_error = ProtocolError("INTERNAL_ERROR", "Internal failure", False, 1)
        return _failure(protocol_error), protocol_error.exit_code


async def _run() -> int:
    current_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if current_task is not None and hasattr(loop, "add_signal_handler"):
        try:
            loop.add_signal_handler(signal.SIGTERM, current_task.cancel)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        raw = await asyncio.to_thread(sys.stdin.buffer.read, MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            error = ProtocolError("REQUEST_TOO_LARGE", "Request exceeds 65536 bytes", False, 2)
            response, exit_code = _failure(error), error.exit_code
        else:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error = ProtocolError("MALFORMED_JSON", "Request is not valid UTF-8 JSON", False, 2)
                response, exit_code = _failure(error), error.exit_code
            else:
                response, exit_code = await handle_payload(payload)
    except asyncio.CancelledError:
        error = ProtocolError("CANCELLED", "Operation cancelled", True, 130)
        response, exit_code = _failure(error), error.exit_code
    finally:
        await close_client()

    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return exit_code


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
