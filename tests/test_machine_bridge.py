"""Machine bridge protocol, validation, and redaction tests."""

import asyncio
import json
import subprocess
import sys

import pytest

from pivot_web_search_mcp.fetch_service import FetchItem, FetchResponse
from pivot_web_search_mcp.machine_bridge import MAX_REQUEST_BYTES, handle_payload
from pivot_web_search_mcp.search_service import SearchResponse, SearchServiceError


class Service:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None

    async def search(self, request):
        self.request = request
        if self.error:
            raise self.error
        return self.response


class FetchService:
    def __init__(self, response):
        self.response = response
        self.request = None

    async def fetch(self, request):
        self.request = request
        return self.response


def request(**params):
    return {"protocolVersion": 1, "method": "search", "params": {"query": "测试 query", **params}}


def fetch_request(**params):
    return {"protocolVersion": 1, "method": "fetch", "params": {"url": "https://example.com", **params}}


async def test_valid_request_maps_structured_response_and_mode():
    service = Service(
        SearchResponse(
            query="测试 query",
            results=[
                {
                    "url": "https://example.com/a",
                    "title": "Title",
                    "snippet": "Snippet",
                    "published_date": "2026-01-02",
                    "ignored": "not public",
                },
                {"url": "file:///etc/passwd", "title": "drop"},
            ],
            provider="secret-internal-provider",
            answer="Answer",
        )
    )
    payload, exit_code = await handle_payload(request(maxResults=7, mode="super"), search_service=service)
    assert exit_code == 0
    assert payload == {
        "protocolVersion": 1,
        "ok": True,
        "result": {
            "sources": [
                {
                    "url": "https://example.com/a",
                    "title": "Title",
                    "snippet": "Snippet",
                    "publishedAt": "2026-01-02",
                }
            ],
            "truncated": False,
            "content": "Answer",
        },
    }
    assert service.request.max_results == 7
    assert service.request.mode == "super"
    assert "provider" not in json.dumps(payload)


async def test_fetch_maps_extracted_text_to_standard_web_fetch_result():
    fetch_service = FetchService(
        FetchResponse(
            [
                FetchItem(
                    url="https://example.com/final",
                    content="# Extracted",
                    truncated=True,
                    status_code=200,
                )
            ]
        )
    )
    payload, exit_code = await handle_payload(fetch_request(), fetch_service=fetch_service)
    assert exit_code == 0
    assert payload == {
        "protocolVersion": 1,
        "ok": True,
        "result": {
            "url": "https://example.com/final",
            "statusCode": 200,
            "body": {"kind": "text", "content": "# Extracted"},
            "truncated": True,
        },
    }
    assert fetch_service.request.urls == ["https://example.com"]


async def test_fetch_preserves_non_success_http_status_as_result():
    fetch_service = FetchService(
        FetchResponse([FetchItem(url="https://example.com/missing", error="HTTP 404", status_code=404)])
    )
    payload, exit_code = await handle_payload(fetch_request(), fetch_service=fetch_service)
    assert exit_code == 0
    assert payload["result"]["url"] == "https://example.com/missing"
    assert payload["result"]["statusCode"] == 404
    assert payload["result"]["body"] == {"kind": "text", "content": ""}


async def test_fetch_failure_is_sanitized():
    fetch_service = FetchService(FetchResponse([FetchItem(url="https://example.com", error="Bearer secret")]))
    payload, exit_code = await handle_payload(fetch_request(), fetch_service=fetch_service)
    assert exit_code == 3
    assert payload["error"] == {"code": "FETCH_FAILED", "message": "Fetch failed", "retryable": True}


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "UNSUPPORTED_PROTOCOL"),
        ({"protocolVersion": 2, "method": "search", "params": {}}, "UNSUPPORTED_PROTOCOL"),
        ({"protocolVersion": 1, "method": "fetch", "params": {}}, "INVALID_REQUEST"),
        ({"protocolVersion": 1, "method": "unknown", "params": {}}, "UNSUPPORTED_METHOD"),
        (fetch_request(extra=True), "INVALID_REQUEST"),
        (request(extra=True), "INVALID_REQUEST"),
        (request(query=""), "INVALID_REQUEST"),
        (request(maxResults=True), "INVALID_REQUEST"),
        (request(maxResults=21), "INVALID_REQUEST"),
        (request(mode="auto"), "INVALID_REQUEST"),
    ],
)
async def test_invalid_requests_have_stable_sanitized_errors(payload, code):
    response, exit_code = await handle_payload(payload, search_service=Service())
    assert exit_code == 2
    assert response["ok"] is False
    assert response["error"]["code"] == code
    assert response["error"]["retryable"] is False


async def test_core_and_internal_errors_are_sanitized():
    core, core_exit = await handle_payload(
        request(),
        Service(
            error=SearchServiceError(
                "SEARCH_FAILED",
                "secret key sk-private",
                failures=[{"error": "/private/path"}],
            )
        ),
    )
    assert core_exit == 3
    assert core["error"] == {"code": "SEARCH_FAILED", "message": "Search failed", "retryable": True}

    internal, internal_exit = await handle_payload(
        request(), search_service=Service(error=RuntimeError("Bearer secret"))
    )
    assert internal_exit == 1
    assert internal["error"] == {"code": "INTERNAL_ERROR", "message": "Internal failure", "retryable": False}
    assert "secret" not in json.dumps(internal)


async def test_cancellation_is_distinct():
    response, exit_code = await handle_payload(request(), search_service=Service(error=asyncio.CancelledError()))
    assert exit_code == 130
    assert response["error"]["code"] == "CANCELLED"


def run_bridge(raw: bytes):
    return subprocess.run(
        [sys.executable, "-m", "pivot_web_search_mcp.machine_bridge"],
        input=raw,
        capture_output=True,
        check=False,
    )


def test_malformed_json_stdout_purity_and_exit_code():
    completed = run_bridge(b"not-json")
    assert completed.returncode == 2
    assert len(completed.stdout.decode().splitlines()) == 1
    response = json.loads(completed.stdout)
    assert response["error"]["code"] == "MALFORMED_JSON"


def test_input_size_limit():
    completed = run_bridge(b"x" * (MAX_REQUEST_BYTES + 1))
    assert completed.returncode == 2
    response = json.loads(completed.stdout)
    assert response["error"]["code"] == "REQUEST_TOO_LARGE"
