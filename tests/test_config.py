"""Configuration loading tests — env, YAML, and defaults priority."""


from pivot_web_search_mcp import config, providers


def _clear_api_keys(monkeypatch):
    for var in (
        "TAVILY_API_KEY", "PIVOT_USERCONFIG_TAVILY_API_KEY",
        "BRAVE_API_KEY", "PIVOT_USERCONFIG_BRAVE_API_KEY",
        "GEMINI_SEARCH_API_KEY", "PIVOT_USERCONFIG_GEMINI_SEARCH_API_KEY",
        "GOOGLE_STUDIO_API_KEY", "PIVOT_USERCONFIG_GOOGLE_STUDIO_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


class TestProviderRegistryAutoDetect:
    def test_ddg_always_enabled(self, monkeypatch):
        _clear_api_keys(monkeypatch)
        reg = providers.ProviderRegistry()
        reg.load(config_path="/nonexistent/path.yaml")
        names = {p.name for p in reg.get_all()}
        assert names == {"ddg"}

    def test_enables_provider_when_key_present(self, monkeypatch):
        _clear_api_keys(monkeypatch)
        monkeypatch.setenv("TAVILY_API_KEY", "test-key")
        reg = providers.ProviderRegistry()
        reg.load(config_path="/nonexistent/path.yaml")
        names = {p.name for p in reg.get_all()}
        assert names == {"ddg", "tavily"}

    def test_userconfig_prefix_also_enables(self, monkeypatch):
        _clear_api_keys(monkeypatch)
        monkeypatch.setenv("PIVOT_USERCONFIG_BRAVE_API_KEY", "test-key")
        reg = providers.ProviderRegistry()
        reg.load(config_path="/nonexistent/path.yaml")
        names = {p.name for p in reg.get_all()}
        assert names == {"ddg", "brave"}

    def test_gemini_fallback_key_also_enables(self, monkeypatch):
        _clear_api_keys(monkeypatch)
        monkeypatch.setenv("GOOGLE_STUDIO_API_KEY", "test-key")
        reg = providers.ProviderRegistry()
        reg.load(config_path="/nonexistent/path.yaml")
        names = {p.name for p in reg.get_all()}
        assert names == {"ddg", "gemini"}

    def test_smart_defaults_applied(self, monkeypatch):
        _clear_api_keys(monkeypatch)
        monkeypatch.setenv("TAVILY_API_KEY", "k")
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        monkeypatch.setenv("GEMINI_SEARCH_API_KEY", "k")
        reg = providers.ProviderRegistry()
        reg.load(config_path="/nonexistent/path.yaml")
        by_name = {p.name: p.effective_priority for p in reg.get_all()}
        assert by_name["tavily"] == 20
        assert by_name["brave"] == 20
        assert by_name["gemini"] == 20
        assert by_name["ddg"] == 90


class TestProviderRegistryFromYaml:
    def test_yaml_overrides_auto_detect(self, monkeypatch, tmp_path):
        # Even with an API key set, an explicit YAML takes over completely.
        monkeypatch.setenv("TAVILY_API_KEY", "k")
        yaml_file = tmp_path / "providers.yaml"
        yaml_file.write_text(
            "providers:\n"
            "  - name: ddg\n"
            "    type: ddg\n"
            "    enabled: true\n"
            "    priority: 5\n"
        )
        reg = providers.ProviderRegistry()
        reg.load(config_path=str(yaml_file))
        names = [p.name for p in reg.get_all()]
        assert names == ["ddg"]


class TestLoadProxies:
    def setup_method(self):
        # Cache is module-global and bleeds across tests since the cache key
        # ignores path identity — clear it explicitly for each test.
        config._proxies_list = None
        config._proxies_mtime = 0

    def test_env_appends_direct(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROXIES", "http://myproxy:8080")
        result = config.load_proxies(config_path="/nonexistent.yaml")
        assert result == ["http://myproxy:8080", None]

    def test_env_explicit_direct_still_appended(self, monkeypatch):
        # We don't try to detect duplicates — direct is always appended.
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROXIES", "direct,http://myproxy:8080")
        result = config.load_proxies(config_path="/nonexistent.yaml")
        assert result == [None, "http://myproxy:8080", None]

    def test_env_only_direct(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROXIES", "direct")
        result = config.load_proxies(config_path="/nonexistent.yaml")
        assert result == [None, None]

    def test_trailing_comma_ignored(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROXIES", "http://p:80,")
        result = config.load_proxies(config_path="/nonexistent.yaml")
        assert result == ["http://p:80", None]

    def test_spaces_stripped(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROXIES", " http://p:80 , http://q:80 ")
        result = config.load_proxies(config_path="/nonexistent.yaml")
        assert result == ["http://p:80", "http://q:80", None]

    def test_default_when_no_env_or_yaml(self, monkeypatch):
        monkeypatch.delenv("PIVOT_WEB_SEARCH_PROXIES", raising=False)
        result = config.load_proxies(config_path="/nonexistent.yaml")
        assert result == [None]

    def test_yaml_overrides_env_and_respects_user_list(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROXIES", "http://envproxy:8080")
        yaml_file = tmp_path / "proxies.yaml"
        yaml_file.write_text(
            "proxies:\n"
            "  - name: only\n"
            "    url: http://yamlproxy:9090\n"
            "    enabled: true\n"
            "    priority: 1\n"
        )
        result = config.load_proxies(config_path=str(yaml_file))
        # YAML wins; direct is NOT auto-appended (escape hatch for forced-proxy).
        assert result == ["http://yamlproxy:9090"]
