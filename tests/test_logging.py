"""Tests for pivot_web_search_mcp.logging module."""

import os
import sys
from unittest.mock import patch

import pytest


class TestLog:
    def test_log_writes_stderr(self, capsys):
        with patch.dict(os.environ, {"PIVOT_WEB_SEARCH_DEBUG": ""}, clear=False):
            # Re-import to pick up env
            import importlib
            from pivot_web_search_mcp import logging as wsl
            importlib.reload(wsl)
            wsl.log("hello stderr")
            captured = capsys.readouterr()
            assert "[pivot-web-search] hello stderr" in captured.err

    def test_debug_silent_when_not_set(self, capsys):
        with patch.dict(os.environ, {"PIVOT_WEB_SEARCH_DEBUG": ""}, clear=False):
            import importlib
            from pivot_web_search_mcp import logging as wsl
            importlib.reload(wsl)
            wsl.debug("should not appear")
            captured = capsys.readouterr()
            assert captured.err == ""

    def test_debug_mode_creates_logfile(self, tmp_path, monkeypatch):
        with patch.dict(os.environ, {"PIVOT_WEB_SEARCH_DEBUG": "1"}, clear=False):
            import importlib
            from pivot_web_search_mcp import logging as wsl
            log_file = tmp_path / "server.log"
            monkeypatch.setattr(wsl, "_LOG_FILE", log_file)
            monkeypatch.setattr(wsl, "_log_fh", None)
            monkeypatch.setattr(wsl, "_DEBUG", True)
            wsl.log("debug test message")
            assert log_file.exists()
            content = log_file.read_text()
            assert "debug test message" in content
