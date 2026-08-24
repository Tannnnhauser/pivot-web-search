"""Shared configuration service behavior independent of host adapters."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from pivot_web_search_mcp.config_service import ConfigService, ConfigServiceError


class Provider:
    name = "provider"
    provider_type = "fake"
    effective_priority = 10
    affinity = "general"
    timeout_seconds = 5
    enabled = True
    health_check = AsyncMock(return_value=(True, "ready"))


def make_service():
    registry = Mock()
    registry.get_all.return_value = [Provider()]
    registry.config_source = "/config/providers.yaml"
    registry.get_config_sources.return_value = {"source": "yaml", "path": "/config/providers.yaml"}
    breaker = Mock()
    breaker.get_status.return_value = {"state": "closed"}
    return ConfigService(registry, breaker), registry, breaker


async def test_status_redacts_proxy_credentials_and_reports_sources():
    service, _, _ = make_service()
    with (
        patch("pivot_web_search_mcp.config_service._quota.get_quota_summary", return_value={}),
        patch(
            "pivot_web_search_mcp.config_service.load_proxies",
            return_value=["http://user:pass@proxy.test:8080", None],
        ),
        patch("pivot_web_search_mcp.config_service.get_proxy_config_source", return_value={"source": "env"}),
        patch("pivot_web_search_mcp.config_service.get_fetch_config_source", return_value={"source": "default"}),
    ):
        result = await service.execute("status")

    assert result["proxies"] == [
        {"url": "http://proxy.test:8080", "label": "http://proxy.test:8080"},
        {"url": None, "label": "direct"},
    ]
    assert result["providers"][0]["available"] is True
    assert result["config_sources"]["fetch"] == {"source": "default"}


async def test_reload_refreshes_all_config_and_resets_breaker():
    service, registry, breaker = make_service()
    with (
        patch("pivot_web_search_mcp.config_service.reload_proxies", return_value=[None]),
        patch("pivot_web_search_mcp.config_service.reload_fetch_config", return_value={"max_chars": 100}),
    ):
        result = await service.execute("reload")

    registry.reload.assert_called_once_with()
    breaker.reset_all.assert_called_once_with()
    assert result["fetch_config_loaded"] is True


async def test_unknown_action_is_rejected():
    service, _, _ = make_service()
    with pytest.raises(ConfigServiceError, match="Unsupported config action"):
        await service.execute("unknown")
