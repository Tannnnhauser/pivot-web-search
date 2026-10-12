"""Cross-host packaging contract for Claude Code and Codex."""

import json
import pathlib
import re

from pivot_web_search_mcp import config, validation

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PLUGIN_ROOT = _PROJECT_ROOT / "plugins" / "pivot-web-search"


def _json(path):
    return json.loads(path.read_text())


def test_claude_and_codex_manifests_describe_one_plugin():
    claude = _json(_PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    codex = _json(_PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
    package = (_PLUGIN_ROOT / "pyproject.toml").read_text()
    package_version = re.search(r'^version = "([^"]+)"$', package, re.MULTILINE)

    assert claude["name"] == codex["name"] == "pivot-web-search"
    assert claude["version"] == codex["version"]
    assert package_version is not None
    assert package_version.group(1) == claude["version"]
    assert codex["repository"] == "https://github.com/Tannnnhauser/pivot-web-search"
    assert codex["skills"] == "./skills/"
    assert codex["hooks"] == "./hooks/hooks.json"
    assert codex["mcpServers"] == "./.mcp.json"


def test_marketplaces_point_to_the_same_payload():
    claude = _json(_PROJECT_ROOT / ".claude-plugin" / "marketplace.json")
    codex = _json(_PROJECT_ROOT / ".agents" / "plugins" / "marketplace.json")

    assert claude["name"] == codex["name"] == "pivot-web-search-plugin"
    assert claude["plugins"][0]["name"] == codex["plugins"][0]["name"]
    assert claude["plugins"][0]["source"] == "./plugins/pivot-web-search"
    assert codex["plugins"][0]["source"]["path"] == "./plugins/pivot-web-search"


def test_shared_mcp_launch_is_host_neutral():
    mcp = _json(_PLUGIN_ROOT / ".mcp.json")["mcpServers"]["pivot-web-search"]

    assert mcp["command"] == "uv"
    assert mcp["cwd"] == "."
    assert mcp["args"] == ["run", "python", "-m", "pivot_web_search_mcp"]
    launch = json.dumps({key: mcp.get(key) for key in ("command", "args", "cwd")})
    assert "CLAUDE_PLUGIN_ROOT" not in launch
    assert "user_config" not in launch


def test_unexpanded_claude_user_config_is_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(config, "USER_PROXIES_YAML", tmp_path / "missing.yaml")
    monkeypatch.setenv(
        "PIVOT_USERCONFIG_TAVILY_API_KEY",
        "${user_config.tavily_api_key}",
    )
    monkeypatch.delenv("PIVOT_WEB_SEARCH_PROXIES", raising=False)
    monkeypatch.setenv("PIVOT_USERCONFIG_PROXY_URLS", "${user_config.proxy_urls}")
    config._proxies_list = None
    config._proxies_mtime = 0

    assert validation._load_env_key("TAVILY_API_KEY") is None
    assert config.load_proxies(config_path="/nonexistent.yaml") == [None]
    assert config.get_proxy_config_source() == {"source": "default"}
