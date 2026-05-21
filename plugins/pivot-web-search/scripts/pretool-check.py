#!/usr/bin/env python3
"""PreToolUse hook: block built-in WebSearch/WebFetch, redirect to MCP tools.

Fail-open: on any parse error or unexpected input format, exits 0 (allow).
Exit codes:
  0 = allow (tool is not WebSearch/WebFetch, or parse error)
  2 = block with reason JSON on stdout
"""
import json
import sys


def main():
    try:
        data = json.load(sys.stdin)
        tool_name = data.get("tool_name", "")
        if tool_name in ("WebSearch", "WebFetch"):
            reason = (
                f"BLOCKED: Do not use the built-in {tool_name} tool. "
                "Use the Pivot Web Search MCP server tools instead "
                "(mcp__pivot-web-search__WebSearch or mcp__pivot-web-search__WebFetch)."
            )
            print(json.dumps({"reason": reason}))
            sys.exit(2)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
