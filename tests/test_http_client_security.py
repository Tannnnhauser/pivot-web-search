"""Security-sensitive HTTP diagnostics."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from pivot_web_search_mcp import http_client


async def test_authenticated_proxy_credentials_never_reach_logs():
    proxy = "http://alice:secret-password@proxy.example:8080"
    response = httpx.Response(200, request=httpx.Request("GET", "https://target.example"))
    with (
        patch.object(http_client, "_get_proxies", return_value=[proxy]),
        patch.object(http_client, "_try_request_with_redirect", new_callable=AsyncMock, return_value=response),
        patch.object(http_client, "_record_proxy_success", new_callable=AsyncMock),
        patch.object(http_client, "log") as logged,
    ):
        await http_client._open_with_fallback("GET", "https://target.example")

    messages = " ".join(str(call.args[0]) for call in logged.call_args_list)
    assert "http://proxy.example:8080" in messages
    assert "alice" not in messages
    assert "secret-password" not in messages


async def test_authenticated_proxy_credentials_are_redacted_from_failure_logs():
    proxy = "http://alice:secret-password@proxy.example:8080"
    with (
        patch.object(http_client, "_get_proxies", return_value=[proxy]),
        patch.object(
            http_client,
            "_try_request_with_redirect",
            new_callable=AsyncMock,
            side_effect=RuntimeError(f"connection failed via {proxy}"),
        ),
        patch.object(http_client, "log") as logged,
        pytest.raises(RuntimeError),
    ):
        await http_client._open_with_fallback("GET", "https://failure-log.example")

    messages = " ".join(str(call.args[0]) for call in logged.call_args_list)
    assert "http://proxy.example:8080" in messages
    assert "alice" not in messages
    assert "secret-password" not in messages
