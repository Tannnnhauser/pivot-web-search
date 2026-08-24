"""CLI adapter mapping, rendering, exit, and cleanup tests."""

import json
import sys
from importlib.metadata import distribution
from unittest.mock import AsyncMock, patch

import pytest

from pivot_web_search_mcp import cli
from pivot_web_search_mcp.fetch_service import FetchItem, FetchResponse
from pivot_web_search_mcp.search_service import SearchResponse, SearchServiceError


def test_installed_package_exposes_cli_console_script():
    scripts = {
        entry.name: entry.value
        for entry in distribution("pivot-web-search-mcp").entry_points
        if entry.group == "console_scripts"
    }
    assert scripts["pivot-web-search"] == "pivot_web_search_mcp.cli:main"


class Service:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None
        self.registry = type("Registry", (), {"get_all": lambda self: []})()

    async def search(self, request):
        self.request = request
        if self.error:
            raise self.error
        return self.response


async def run_cli(argv, service):
    with (
        patch.object(sys, "argv", ["pivot"] + argv),
        patch.object(cli, "SearchService", return_value=service),
        patch.object(cli, "close_client", new_callable=AsyncMock) as close,
    ):
        await cli._async_main()
    return close


async def test_search_maps_flags_to_shared_core_and_renders_json(capsys):
    service = Service(
        SearchResponse(
            query="你好",
            results=[{"title": "结果", "url": "https://example.com", "snippet": "摘要"}],
            provider="ddg,tavily",
            answer="答案",
        )
    )
    close = await run_cli(
        [
            "search",
            "你好",
            "--super",
            "--max-results",
            "19",
            "--news",
            "--include-domains",
            "example.com",
            "--include-content",
            "--max-content-tokens",
            "4096",
            "--format",
            "json",
        ],
        service,
    )
    output = json.loads(capsys.readouterr().out)
    assert output["query"] == "你好"
    assert output["provider"] == "ddg,tavily"
    assert output["answer"] == "答案"
    assert service.request.mode == "super"
    assert service.request.max_results == 19
    assert service.request.news is True
    assert service.request.allowed_domains == ["example.com"]
    assert service.request.include_content is True
    assert service.request.max_content_tokens == 4096
    close.assert_awaited_once()


async def test_search_failure_keeps_public_error_and_exit_code(capsys):
    service = Service(error=SearchServiceError("SEARCH_FAILED", "provider secret"))
    with pytest.raises(SystemExit) as raised:
        await run_cli(["search", "query"], service)
    assert raised.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"error": "All providers failed", "query": "query"}


async def test_extract_alias_uses_shared_fetch_service_and_cleanup(capsys):
    fetch_service = AsyncMock()
    fetch_service.fetch.return_value = FetchResponse([FetchItem(url="https://example.com", content="text")])
    with (
        patch.object(sys, "argv", ["pivot", "extract", "https://example.com"]),
        patch.object(cli, "FetchService", return_value=fetch_service),
        patch.object(cli, "close_client", new_callable=AsyncMock) as close,
    ):
        await cli._async_main()
    assert json.loads(capsys.readouterr().out) == {
        "results": [{"url": "https://example.com", "content": "text", "truncated": False}],
        "extracted": 1,
        "requested": 1,
    }
    assert fetch_service.fetch.await_args.args[0].urls == ["https://example.com"]
    close.assert_awaited_once()


async def test_fetch_maps_query_limit_and_markdown(capsys):
    fetch_service = AsyncMock()
    fetch_service.fetch.return_value = FetchResponse(
        [FetchItem(url="https://example.com", content="text", truncated=True)]
    )
    with (
        patch.object(
            sys,
            "argv",
            ["pivot", "fetch", "https://example.com", "--query", "focus", "--max-chars", "10", "--format", "md"],
        ),
        patch.object(cli, "FetchService", return_value=fetch_service),
        patch.object(cli, "close_client", new_callable=AsyncMock),
    ):
        await cli._async_main()
    request = fetch_service.fetch.await_args.args[0]
    assert request.query == "focus"
    assert request.max_chars == 10
    assert "Content truncated" in capsys.readouterr().out


async def test_config_uses_shared_service(capsys):
    search_service = Service()
    search_service.breaker = object()
    config_service = AsyncMock()
    config_service.execute.return_value = {"action": "status", "providers": []}
    with (
        patch.object(sys, "argv", ["pivot", "config", "status"]),
        patch.object(cli, "SearchService", return_value=search_service),
        patch.object(cli, "ConfigService", return_value=config_service) as config_factory,
        patch.object(cli, "close_client", new_callable=AsyncMock),
    ):
        await cli._async_main()
    assert json.loads(capsys.readouterr().out) == {"action": "status", "providers": []}
    config_factory.assert_called_once_with(search_service.registry, search_service.breaker)
    config_service.execute.assert_awaited_once_with("status")
