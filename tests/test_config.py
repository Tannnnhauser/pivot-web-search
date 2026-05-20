"""Configuration loading tests — env, YAML, and defaults priority."""


from pivot_web_search_mcp import config, providers


class TestProviderRegistryFromEnv:
    def test_comma_parsing(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROVIDERS", "ddg,tavily,brave")
        reg = providers.ProviderRegistry()
        reg.load()
        names = [p.name for p in reg.get_ordered()]
        assert names == ["ddg", "tavily", "brave"]

    def test_priority_by_position(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROVIDERS", "brave,ddg")
        reg = providers.ProviderRegistry()
        reg.load()
        ordered = reg.get_ordered()
        assert ordered[0].name == "brave"
        assert ordered[0].priority < ordered[1].priority

    def test_empty_string_ignored(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROVIDERS", "")
        reg = providers.ProviderRegistry()
        reg.load()
        assert len(reg.get_all()) > 0  # falls through to defaults

    def test_unknown_type_skipped(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROVIDERS", "ddg,nonexistent")
        reg = providers.ProviderRegistry()
        reg.load()
        names = [p.name for p in reg.get_all()]
        assert "ddg" in names
        assert "nonexistent" not in names


class TestProviderRegistryFromDefaults:
    def test_default_providers_loaded(self, monkeypatch):
        monkeypatch.delenv("PIVOT_WEB_SEARCH_PROVIDERS", raising=False)
        reg = providers.ProviderRegistry()
        reg.load(config_path="/nonexistent/path.yaml")
        names = [p.name for p in reg.get_all()]
        assert "ddg" in names
        assert "tavily" in names
        assert "brave" in names
        assert "gemini" in names


class TestProviderRegistryFromYaml:
    def test_yaml_loading(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PIVOT_WEB_SEARCH_PROVIDERS", raising=False)
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
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROXIES", "direct,http://myproxy:8080")
        result = config.load_proxies()
        assert result == [None, "http://myproxy:8080"]

    def test_direct_maps_to_none(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROXIES", "direct")
        result = config.load_proxies()
        assert result == [None]

    def test_trailing_comma_ignored(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROXIES", "direct,")
        result = config.load_proxies()
        assert result == [None]

    def test_spaces_stripped(self, monkeypatch):
        monkeypatch.setenv("PIVOT_WEB_SEARCH_PROXIES", " direct , http://p:80 ")
        result = config.load_proxies()
        assert result == [None, "http://p:80"]

    def test_defaults_when_no_env(self, monkeypatch):
        monkeypatch.delenv("PIVOT_WEB_SEARCH_PROXIES", raising=False)
        result = config.load_proxies(config_path="/nonexistent.yaml")
        assert len(result) > 0
        assert None in result  # direct is always included in defaults
