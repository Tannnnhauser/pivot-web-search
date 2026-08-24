# Pivot Web Search for DeepSeek Harness

This is an out-of-tree DSH Profile Bundle maintained by Pivot Web Search. It
registers Pivot as DSH's standard `web_search` and `web_fetch` provider without
patching or rebuilding DeepSeek Harness.

## Install

From a local Pivot checkout, install the runtime and Bundle with:

```sh
uv tool install --force ./plugins/pivot-web-search
dsh plugin --profile web add ./integrations/deepseek-harness
```

For normal adoption after the Bundle is published:

```sh
uv tool install 'git+https://github.com/Tannnnhauser/pivot-web-search.git@v1.1.0#subdirectory=plugins/pivot-web-search'
dsh plugin --profile web add pivot-web-search-dsh
```

Restart the profile after adding or removing a Bundle. The model continues to
see DSH's existing `web_search` and `web_fetch` tools; it does not see a second
set of Pivot-specific tools.

The shipped DSH web profile disables `tool-web`; this Bundle explicitly enables
that existing entry, then selects provider ID `pivot` for both web capabilities.
Verify the final composition with:

```sh
dsh --profile web --dump-config
```

It should contain `searchProvider: pivot`, `fetchProvider: pivot`, an enabled
`tool-web`, and `pivot-web-search-provider`.

Provider API keys are forwarded explicitly by `cordis.patch.yml`. Advanced
Pivot configuration remains in `~/.pivot-web-search/providers.yaml` and
`~/.pivot-web-search/proxies.yaml` on the machine running DSH.

To customize the command, environment, or search mode, override the complete
`pivot-web-search-provider` row in the profile's `cordis.patch.yml`.

Remove the Bundle with:

```sh
dsh plugin --profile web remove pivot-web-search-dsh
```

This changes only the profile dependency and Bundle list, never the DSH source
tree.

## Boundary

The package uses DSH's published Profile Bundle, `ctx.web`, and
`ctx.subprocess` contracts. It contains no DSH source patches and requires no
fork of DeepSeek Harness.
