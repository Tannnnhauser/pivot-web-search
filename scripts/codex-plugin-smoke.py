#!/usr/bin/env python3
"""Install the plugin with Codex and handshake with its cached MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_ID = "pivot-web-search@pivot-web-search-plugin"
EXPECTED_TOOLS = {"WebSearch", "WebFetch", "WebSearchConfig"}
PROVIDER_KEY_ENV = {
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "GEMINI_SEARCH_API_KEY",
    "GOOGLE_STUDIO_API_KEY",
}


def run(command: list[str], *, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        output = completed.stderr or completed.stdout
        raise RuntimeError(f"{' '.join(command)} failed ({completed.returncode}):\n{output}")
    return completed


def parse_json(completed: subprocess.CompletedProcess[str]) -> object:
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"command returned invalid JSON:\n{completed.stdout}") from error


async def verify_mcp_server(plugin_root: pathlib.Path, isolated_home: pathlib.Path) -> None:
    mcp_config = json.loads((plugin_root / ".mcp.json").read_text())
    server = mcp_config["mcpServers"]["pivot-web-search"]
    server_cwd = (plugin_root / server.get("cwd", ".")).resolve()
    if not server_cwd.is_relative_to(plugin_root.resolve()):
        raise RuntimeError(f"MCP cwd escapes installed plugin: {server_cwd}")

    server_environment = os.environ.copy()
    for name in PROVIDER_KEY_ENV:
        server_environment.pop(name, None)
    server_environment.update(server.get("env", {}))
    server_environment["HOME"] = str(isolated_home)
    server_environment["USERPROFILE"] = str(isolated_home)
    parameters = StdioServerParameters(
        command=server["command"],
        args=server.get("args", []),
        cwd=server_cwd,
        env=server_environment,
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}

    if tools != EXPECTED_TOOLS:
        raise RuntimeError(f"unexpected installed MCP tools: {sorted(tools)}")


async def verify_mcp_server_with_timeout(
    plugin_root: pathlib.Path, isolated_home: pathlib.Path
) -> None:
    await asyncio.wait_for(verify_mcp_server(plugin_root, isolated_home), timeout=180)


def main() -> None:
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("codex CLI is required")

    manifest = json.loads(
        (PROJECT_ROOT / "plugins/pivot-web-search/.codex-plugin/plugin.json").read_text()
    )
    with tempfile.TemporaryDirectory(prefix="pivot-codex-plugin-") as codex_home:
        environment = {**os.environ, "CODEX_HOME": codex_home}
        marketplace = os.environ.get("PIVOT_CODEX_MARKETPLACE", str(PROJECT_ROOT))
        marketplace_command = [codex, "plugin", "marketplace", "add", marketplace]
        marketplace_ref = os.environ.get("PIVOT_CODEX_MARKETPLACE_REF")
        if marketplace_ref:
            marketplace_command.extend(["--ref", marketplace_ref])
        marketplace_command.append("--json")
        run(marketplace_command, environment=environment)
        installed = parse_json(
            run([codex, "plugin", "add", PLUGIN_ID, "--json"], environment=environment)
        )
        if not isinstance(installed, dict):
            raise RuntimeError(f"unexpected install response: {installed!r}")

        plugin_root = pathlib.Path(str(installed["installedPath"])).resolve()
        listing = parse_json(run([codex, "plugin", "list", "--json"], environment=environment))
        entries = listing.get("installed", []) if isinstance(listing, dict) else []
        matching = [entry for entry in entries if entry.get("pluginId") == PLUGIN_ID]
        if len(matching) != 1 or not matching[0].get("enabled"):
            raise RuntimeError(f"installed plugin is not enabled: {matching!r}")
        if matching[0].get("version") != manifest["version"]:
            raise RuntimeError(f"installed plugin version mismatch: {matching[0]!r}")

        prompt = run(
            [codex, "debug", "prompt-input", "Use Pivot Web Search."],
            environment=environment,
        )
        plugin_errors = (
            "failed to parse plugin manifest",
            "failed to parse plugin MCP server",
            "missing or invalid plugin.json",
            "disabling MCP",
        )
        diagnostics = prompt.stderr.lower()
        if any(message.lower() in diagnostics for message in plugin_errors):
            raise RuntimeError(f"Codex reported plugin diagnostics:\n{prompt.stderr}")
        if "pivot-web-search:pivot-web-search" not in prompt.stdout:
            raise RuntimeError("installed Pivot skill is absent from a fresh Codex prompt")

        asyncio.run(verify_mcp_server_with_timeout(plugin_root, pathlib.Path(codex_home)))
        version = run([codex, "--version"], environment=environment).stdout.strip()
        print(f"Codex plugin smoke passed with {version}: {', '.join(sorted(EXPECTED_TOOLS))}")


if __name__ == "__main__":
    main()
