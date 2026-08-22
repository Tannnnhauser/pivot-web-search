# Pivot Web Search for DeepSeek Harness

This is an out-of-tree DSH Profile Bundle maintained by Pivot Web Search. It
registers Pivot as DSH's standard `web_search` and `web_fetch` provider without
patching or rebuilding DeepSeek Harness.

## Install

Install the Pivot runtime first:

```sh
uv tool install 'git+https://github.com/Tannnnhauser/pivot-web-search.git#subdirectory=plugins/pivot-web-search'
```

Then install this bundle into a DSH profile. During local development, run the
following from this repository root:

```sh
dsh plugin --profile web add ./integrations/deepseek-harness
```

After this package is published, the equivalent command is:

```sh
dsh plugin --profile web add pivot-web-search-dsh
```

Restart the profile after adding or removing a Bundle. The model continues to
see DSH's existing `web_search` and `web_fetch` tools; it does not see a second
set of Pivot-specific tools.

Provider API keys are forwarded explicitly by `cordis.patch.yml`. Advanced
Pivot configuration remains in `~/.pivot-web-search/providers.yaml` and
`~/.pivot-web-search/proxies.yaml` on the machine running DSH.

To customize the command, environment, or search mode, override the complete
`pivot-web-search-provider` row in the profile's `cordis.patch.yml`.

## Boundary

The package uses DSH's published Profile Bundle, `ctx.web`, and
`ctx.subprocess` contracts. It contains no DSH source patches and requires no
fork of DeepSeek Harness.
