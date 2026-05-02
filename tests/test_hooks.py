"""PreToolUse hook blocking verification."""

import json
import pathlib
import subprocess

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_HOOKS_JSON = _PROJECT_ROOT / "hooks" / "hooks.json"


def _get_hook_command():
    """Extract the full shell command for the PreToolUse hook from hooks.json."""
    data = json.loads(_HOOKS_JSON.read_text())
    pre_tool_use = data.get("hooks", {}).get("PreToolUse", [])
    for entry in pre_tool_use:
        for hook in entry.get("hooks", []):
            if hook.get("type") == "command":
                return hook["command"]
    raise RuntimeError("No PreToolUse hook command found in hooks.json")


def _run_hook(tool_name):
    """Run the hook command via shell with the given tool_name as JSON stdin."""
    cmd = _get_hook_command().replace("${CLAUDE_PLUGIN_ROOT}", str(_PROJECT_ROOT))
    result = subprocess.run(
        cmd, shell=True,
        input=json.dumps({"tool_name": tool_name}),
        capture_output=True, text=True,
    )
    return result


class TestPreToolUseHook:
    def test_blocks_websearch(self):
        result = _run_hook("WebSearch")
        assert result.returncode == 2
        assert "BLOCKED" in result.stdout

    def test_blocks_webfetch(self):
        result = _run_hook("WebFetch")
        assert result.returncode == 2
        assert "BLOCKED" in result.stdout

    def test_allows_bash(self):
        result = _run_hook("Bash")
        assert result.returncode == 0

    def test_allows_read(self):
        result = _run_hook("Read")
        assert result.returncode == 0

    def test_allows_mcp_websearch(self):
        result = _run_hook("mcp__pivot-web-search__WebSearch")
        assert result.returncode == 0

    def test_fail_open_on_malformed_json(self):
        """Hook should exit 0 (allow) when given invalid JSON."""
        cmd = _get_hook_command().replace("${CLAUDE_PLUGIN_ROOT}", str(_PROJECT_ROOT))
        result = subprocess.run(
            cmd, shell=True, input="not valid json",
            capture_output=True, text=True,
        )
        assert result.returncode == 0

    def test_fail_open_on_empty_stdin(self):
        """Hook should exit 0 (allow) when stdin is empty."""
        cmd = _get_hook_command().replace("${CLAUDE_PLUGIN_ROOT}", str(_PROJECT_ROOT))
        result = subprocess.run(
            cmd, shell=True, input="",
            capture_output=True, text=True,
        )
        assert result.returncode == 0
