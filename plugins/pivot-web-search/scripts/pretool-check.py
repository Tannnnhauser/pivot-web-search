#!/usr/bin/env python3
"""PreToolUse hook: block built-in WebSearch/WebFetch, redirect to MCP tools.

Fail-open: on any parse error or unexpected input format, exits 0 (allow).

Uses the structured PreToolUse hook response (exit 0, JSON on stdout) so the
deny reason is shown cleanly to the user and the model. Exiting non-zero
without anything on stderr is reported by Claude Code as a hook crash
("hook error: No stderr output") rather than a clean tool block.
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
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }))
            sys.exit(0)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
