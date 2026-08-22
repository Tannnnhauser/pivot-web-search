"""Shared fetch service behavior independent of MCP and CLI adapters."""

from unittest.mock import AsyncMock, patch

import pytest

from pivot_web_search_mcp.fetch_service import FetchRequest, FetchService, FetchServiceError


async def test_fetch_preserves_order_fallback_errors_and_truncation():
    extraction = {
        "results": [
            {
                "url": "https://a.test",
                "final_url": "https://a.test/final",
                "status_code": 200,
                "raw_content": "abcdef",
            }
        ],
        "failed_results": [{"url": "https://b.test", "error": "timeout"}],
    }
    with (
        patch("pivot_web_search_mcp.fetch_service.validate_url", side_effect=lambda url: url),
        patch(
            "pivot_web_search_mcp.fetch_service.load_fetch_config",
            return_value={"max_chars": 100, "empty_threshold": 2},
        ),
        patch(
            "pivot_web_search_mcp.fetch_service.extract_trafilatura",
            new_callable=AsyncMock,
            return_value=extraction,
        ),
        patch(
            "pivot_web_search_mcp.fetch_service._fetch.render_with_fallback",
            new_callable=AsyncMock,
            return_value="fallback content",
        ) as fallback,
    ):
        response = await FetchService().fetch(
            FetchRequest(urls=["https://a.test", "https://b.test"], query="focus", max_chars=5)
        )

    assert response.extracted_count == 2
    assert response.items[0].content == "abcde"
    assert response.items[0].url == "https://a.test/final"
    assert response.items[0].status_code == 200
    assert response.items[0].truncated is True
    assert response.items[1].content == "fallb"
    assert response.items[1].truncated is True
    assert fallback.await_args.kwargs["query"] == "focus"


async def test_fetch_keeps_validation_failure_in_batch_result():
    def validate(url):
        if url == "bad":
            raise ValueError("unsupported URL")
        return url

    with (
        patch("pivot_web_search_mcp.fetch_service.validate_url", side_effect=validate),
        patch(
            "pivot_web_search_mcp.fetch_service.load_fetch_config",
            return_value={"max_chars": 100, "empty_threshold": 2},
        ),
        patch(
            "pivot_web_search_mcp.fetch_service.extract_trafilatura",
            new_callable=AsyncMock,
            return_value={"results": [{"url": "https://ok.test", "raw_content": "content"}], "failed_results": []},
        ),
    ):
        response = await FetchService().fetch(FetchRequest(urls=["bad", "https://ok.test"]))

    assert response.items[0].error == "unsupported URL"
    assert response.items[1].content == "content"


@pytest.mark.parametrize(
    ("fetch_request", "message"),
    [
        (FetchRequest(urls=[]), "Empty URL"),
        (FetchRequest(urls=[""], max_chars=10), "Empty URL"),
        (FetchRequest(urls=["https://a.test"], max_chars=0), "max_chars must be a positive integer"),
    ],
)
async def test_fetch_rejects_invalid_request(fetch_request, message):
    with pytest.raises(FetchServiceError, match=message):
        await FetchService().fetch(fetch_request)
