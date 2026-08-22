"""Compatibility contract for the three model-visible MCP tools."""

import inspect

from fastmcp import Client

from pivot_web_search_mcp import server


async def test_raw_tool_names_and_webfetch_schema():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert set(tools) == {"WebSearch", "WebFetch", "WebSearchConfig"}

    schema = tools["WebFetch"].parameters
    assert schema["required"] == ["url"]
    assert list(schema["properties"]) == ["url", "query", "max_chars"]
    assert "prompt" not in schema["properties"]
    assert schema["properties"]["query"]["default"] is None
    assert schema["properties"]["max_chars"]["default"] is None


async def test_websearch_schema_defaults_and_python_return_type():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    schema = tools["WebSearch"].parameters
    assert schema["required"] == ["query"]
    assert schema["properties"]["max_results"]["default"] == 5
    assert schema["properties"]["provider"]["default"] == "auto"
    assert schema["properties"]["super_mode"]["default"] is False
    assert schema["properties"]["news"]["default"] is None
    assert inspect.signature(server.WebSearch).return_annotation is str
    assert isinstance(await server.WebSearch(" "), str)


async def test_fastmcp_wraps_python_string_as_text_content():
    async with Client(server.mcp) as client:
        result = await client.call_tool("WebSearch", {"query": " "})
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert isinstance(result.content[0].text, str)
