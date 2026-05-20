"""Tests for LlmSearchProvider and format strategies."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pivot_web_search_mcp.llm_search_formats import (
    ChatCompletionsFormat,
    GeminiFormat,
    ResponsesFormat,
)
from pivot_web_search_mcp.providers import LlmSearchProvider

_FETCH_PATH = "pivot_web_search_mcp.providers.adapters._open_with_fallback"

# ---------------------------------------------------------------------------
# ChatCompletionsFormat tests
# ---------------------------------------------------------------------------


class TestChatCompletionsFormat:
    def setup_method(self):
        self.fmt = ChatCompletionsFormat()

    def test_build_request_basic(self):
        config = {
            "endpoint": "https://api.example.com/chat/completions",
            "model": "sonar-pro",
            "max_tokens": 500,
        }
        url, headers, body = self.fmt.build_request("test query", 5, config)
        assert url == "https://api.example.com/chat/completions"
        assert headers["Content-Type"] == "application/json"
        payload = json.loads(body)
        assert payload["model"] == "sonar-pro"
        assert payload["stream"] is False
        assert payload["max_tokens"] == 500
        assert payload["messages"][1]["content"] == "test query"

    def test_build_request_custom_system_prompt(self):
        config = {
            "endpoint": "https://api.example.com/v1",
            "model": "test",
            "system_prompt": "Custom prompt",
        }
        _, _, body = self.fmt.build_request("q", 5, config)
        payload = json.loads(body)
        assert payload["messages"][0]["content"] == "Custom prompt"

    def test_build_request_extra_headers(self):
        config = {
            "endpoint": "https://api.example.com/v1",
            "model": "test",
            "headers": {"AI-Resource-Group": "default", "X-Custom": "val"},
        }
        _, headers, _ = self.fmt.build_request("q", 5, config)
        assert headers["AI-Resource-Group"] == "default"
        assert headers["X-Custom"] == "val"

    def test_parse_search_results_priority_1(self):
        obj = {
            "choices": [{"message": {"content": "AI summary here", "role": "assistant"}}],
            "search_results": [
                {"title": "Result 1", "url": "https://a.com", "snippet": "Snip 1"},
                {"title": "Result 2", "url": "https://b.com", "snippet": "Snip 2"},
            ],
            "citations": ["https://a.com", "https://b.com"],
        }
        results, answer = self.fmt.parse_response(obj, 5)
        assert len(results) == 2
        assert results[0]["title"] == "Result 1"
        assert results[0]["url"] == "https://a.com"
        assert answer == "AI summary here"

    def test_parse_annotations_priority_2(self):
        obj = {
            "choices": [{
                "message": {
                    "content": "Answer with citations",
                    "annotations": [
                        {"type": "url_citation", "url_citation": {"url": "https://x.com", "title": "X"}},
                        {"type": "url_citation", "url_citation": {"url": "https://y.com", "title": "Y"}},
                        {"type": "file_citation", "file_id": "abc"},
                    ],
                },
            }],
        }
        results, answer = self.fmt.parse_response(obj, 5)
        assert len(results) == 2
        assert results[0]["url"] == "https://x.com"
        assert results[1]["url"] == "https://y.com"
        assert answer == "Answer with citations"

    def test_parse_annotations_flat_format(self):
        """Some APIs put url/title directly on the annotation, not nested."""
        obj = {
            "choices": [{
                "message": {
                    "content": "Answer",
                    "annotations": [
                        {"type": "url_citation", "url": "https://z.com", "title": "Z"},
                    ],
                },
            }],
        }
        results, answer = self.fmt.parse_response(obj, 5)
        assert len(results) == 1
        assert results[0]["url"] == "https://z.com"

    def test_parse_citations_priority_3(self):
        obj = {
            "choices": [{"message": {"content": "Summary"}}],
            "citations": ["https://c1.com", "https://c2.com", "https://c3.com"],
        }
        results, answer = self.fmt.parse_response(obj, 5)
        assert len(results) == 3
        assert results[0]["url"] == "https://c1.com"
        assert answer == "Summary"

    def test_empty_search_results_falls_through(self):
        obj = {
            "choices": [{"message": {"content": "Answer"}}],
            "search_results": [],
            "citations": ["https://fallback.com"],
        }
        results, answer = self.fmt.parse_response(obj, 5)
        assert len(results) == 1
        assert results[0]["url"] == "https://fallback.com"

    def test_no_results_no_citations(self):
        obj = {
            "choices": [{"message": {"content": "Plain answer without search"}}],
        }
        results, answer = self.fmt.parse_response(obj, 5)
        assert results == []
        assert answer == "Plain answer without search"

    def test_max_results_limit(self):
        obj = {
            "choices": [{"message": {"content": "Answer"}}],
            "search_results": [
                {"title": f"R{i}", "url": f"https://{i}.com", "snippet": ""}
                for i in range(20)
            ],
        }
        results, _ = self.fmt.parse_response(obj, 5)
        assert len(results) == 5

    def test_deduplicates_annotations(self):
        obj = {
            "choices": [{
                "message": {
                    "content": "Answer",
                    "annotations": [
                        {"type": "url_citation", "url": "https://dup.com", "title": "A"},
                        {"type": "url_citation", "url": "https://dup.com", "title": "A"},
                        {"type": "url_citation", "url": "https://other.com", "title": "B"},
                    ],
                },
            }],
        }
        results, _ = self.fmt.parse_response(obj, 10)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# ResponsesFormat tests
# ---------------------------------------------------------------------------


class TestResponsesFormat:
    def setup_method(self):
        self.fmt = ResponsesFormat()

    def test_build_request_basic(self):
        config = {
            "endpoint": "https://api.openai.com/v1/responses",
            "model": "gpt-5.4",
            "max_tokens": 4000,
            "search_tool": "web_search",
            "search_context_size": "medium",
        }
        url, headers, body = self.fmt.build_request("test query", 5, config)
        assert url == "https://api.openai.com/v1/responses"
        payload = json.loads(body)
        assert payload["model"] == "gpt-5.4"
        assert payload["max_output_tokens"] == 4000
        assert payload["input"] == "test query"
        assert payload["tools"][0]["type"] == "web_search"
        assert payload["tools"][0]["search_context_size"] == "medium"

    def test_build_request_with_filters(self):
        config = {
            "endpoint": "https://api.openai.com/v1/responses",
            "model": "gpt-5.4",
            "search_tool": "web_search",
            "filters": {"allowed_domains": ["example.com"]},
        }
        _, _, body = self.fmt.build_request("q", 5, config)
        payload = json.loads(body)
        assert payload["tools"][0]["filters"] == {"allowed_domains": ["example.com"]}

    def test_build_request_with_user_location(self):
        config = {
            "endpoint": "https://api.openai.com/v1/responses",
            "model": "gpt-5.4",
            "search_tool": "web_search",
            "user_location": {"type": "approximate", "country": "US"},
        }
        _, _, body = self.fmt.build_request("q", 5, config)
        payload = json.loads(body)
        assert payload["tools"][0]["user_location"]["country"] == "US"

    def test_parse_response_with_annotations(self):
        obj = {
            "output": [
                {"type": "web_search_call", "action": {"type": "search"}},
                {
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": "Here is the answer...",
                        "annotations": [
                            {"type": "url_citation", "url": "https://a.com", "title": "A"},
                            {"type": "url_citation", "url": "https://b.com", "title": "B"},
                        ],
                    }],
                },
            ],
        }
        results, answer = self.fmt.parse_response(obj, 5)
        assert len(results) == 2
        assert results[0]["url"] == "https://a.com"
        assert results[1]["title"] == "B"
        assert answer == "Here is the answer..."

    def test_parse_response_deduplicates_urls(self):
        obj = {
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": "Answer",
                    "annotations": [
                        {"type": "url_citation", "url": "https://same.com", "title": "A"},
                        {"type": "url_citation", "url": "https://same.com", "title": "A"},
                    ],
                }],
            }],
        }
        results, _ = self.fmt.parse_response(obj, 10)
        assert len(results) == 1

    def test_parse_response_filters_non_url_citations(self):
        obj = {
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": "Answer",
                    "annotations": [
                        {"type": "file_citation", "file_id": "abc"},
                        {"type": "url_citation", "url": "https://real.com", "title": "Real"},
                    ],
                }],
            }],
        }
        results, _ = self.fmt.parse_response(obj, 10)
        assert len(results) == 1
        assert results[0]["url"] == "https://real.com"

    def test_parse_response_fallback_output_text(self):
        obj = {
            "output": [{"type": "reasoning"}],
            "output_text": "Fallback answer text",
        }
        results, answer = self.fmt.parse_response(obj, 5)
        assert results == []
        assert answer == "Fallback answer text"

    def test_parse_response_max_results(self):
        annotations = [
            {"type": "url_citation", "url": f"https://{i}.com", "title": f"T{i}"}
            for i in range(20)
        ]
        obj = {
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "A", "annotations": annotations}],
            }],
        }
        results, _ = self.fmt.parse_response(obj, 3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# GeminiFormat tests
# ---------------------------------------------------------------------------


class TestGeminiFormat:
    def setup_method(self):
        self.fmt = GeminiFormat()

    def test_build_request(self):
        config = {"model": "gemini-2.5-flash"}
        url, headers, body = self.fmt.build_request("test query", 5, config)
        assert "gemini-2.5-flash:generateContent" in url
        assert headers["Content-Type"] == "application/json"
        payload = json.loads(body)
        assert payload["contents"][0]["parts"][0]["text"] == "Search the web for: test query"
        assert payload["tools"] == [{"google_search": {}}]

    def test_parse_response_with_grounding(self):
        obj = {
            "candidates": [{
                "content": {"parts": [{"text": "Gemini answer"}]},
                "groundingMetadata": {
                    "groundingChunks": [
                        {"web": {"title": "Chunk 1", "uri": "https://g1.com"}},
                        {"web": {"title": "Chunk 2", "uri": "https://g2.com"}},
                    ],
                    "groundingSupports": [
                        {
                            "segment": {"text": "Support text 1"},
                            "groundingChunkIndices": [0],
                        },
                        {
                            "segment": {"text": "Support text 2"},
                            "groundingChunkIndices": [1],
                        },
                    ],
                },
            }],
        }
        results, answer = self.fmt.parse_response(obj, 5)
        assert len(results) == 2
        assert results[0]["title"] == "Chunk 1"
        assert results[0]["url"] == "https://g1.com"
        assert results[0]["snippet"] == "Support text 1"
        assert results[1]["snippet"] == "Support text 2"
        assert answer == "Gemini answer"

    def test_parse_response_no_chunks(self):
        obj = {
            "candidates": [{
                "content": {"parts": [{"text": "Answer"}]},
                "groundingMetadata": {"groundingChunks": []},
            }],
        }
        results, answer = self.fmt.parse_response(obj, 5)
        assert results == []
        assert answer is None

    def test_parse_response_empty_candidates(self):
        obj = {"candidates": [{}]}
        results, answer = self.fmt.parse_response(obj, 5)
        assert results == []
        assert answer is None


# ---------------------------------------------------------------------------
# LlmSearchProvider integration tests
# ---------------------------------------------------------------------------


class TestLlmSearchProvider:
    def test_init_chat_completions(self):
        p = LlmSearchProvider("test", config={"api_format": "chat_completions", "endpoint": "http://x"})
        assert p.provider_type == "llm_search"
        from pivot_web_search_mcp.llm_search_formats import ChatCompletionsFormat
        assert isinstance(p._format, ChatCompletionsFormat)

    def test_init_responses(self):
        p = LlmSearchProvider("test", config={"api_format": "responses", "endpoint": "http://x"})
        from pivot_web_search_mcp.llm_search_formats import ResponsesFormat
        assert isinstance(p._format, ResponsesFormat)

    def test_init_unknown_format_falls_back(self):
        p = LlmSearchProvider("test", config={"api_format": "unknown", "endpoint": "http://x"})
        from pivot_web_search_mcp.llm_search_formats import ChatCompletionsFormat
        assert isinstance(p._format, ChatCompletionsFormat)

    @pytest.mark.asyncio
    async def test_search_no_endpoint(self):
        p = LlmSearchProvider("test", config={"api_format": "chat_completions"})
        result = await p.search("query")
        assert result is None

    @pytest.mark.asyncio
    async def test_search_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Answer"}}],
            "search_results": [
                {"title": "T1", "url": "https://t1.com", "snippet": "S1"},
            ],
        }

        p = LlmSearchProvider("test", config={
            "api_format": "chat_completions",
            "endpoint": "https://api.example.com/chat/completions",
            "model": "test-model",
        })

        with patch(_FETCH_PATH, new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_resp
            result = await p.search("test query")

        assert result is not None
        assert result.provider == "test"
        assert len(result.results) == 1
        assert result.results[0]["title"] == "T1"
        assert result.answer == "Answer"

    @pytest.mark.asyncio
    async def test_search_http_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"error": {"message": "Unauthorized"}}

        p = LlmSearchProvider("test", config={
            "api_format": "chat_completions",
            "endpoint": "https://api.example.com/v1",
            "model": "m",
        })

        with patch(_FETCH_PATH, new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_resp
            result = await p.search("query")

        assert result is None

    @pytest.mark.asyncio
    async def test_search_adds_bearer_token(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "A"}}],
            "search_results": [{"title": "T", "url": "https://t.com", "snippet": ""}],
        }

        p = LlmSearchProvider("test", config={
            "api_format": "chat_completions",
            "endpoint": "https://api.example.com/v1",
            "model": "m",
            "api_key_env": "TEST_LLM_KEY",
        })

        with patch.dict("os.environ", {"TEST_LLM_KEY": "sk-test123"}):
            with patch(_FETCH_PATH, new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = mock_resp
                await p.search("query")
                call_kwargs = mock_fetch.call_args
                headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
                assert headers.get("Authorization") == "Bearer sk-test123"

    @pytest.mark.asyncio
    async def test_health_check_no_endpoint(self):
        p = LlmSearchProvider("test", config={"api_format": "chat_completions"})
        ok, msg = await p.health_check()
        assert not ok
        assert msg is not None and "endpoint" in msg

    @pytest.mark.asyncio
    async def test_health_check_no_key(self):
        p = LlmSearchProvider("test", config={
            "api_format": "chat_completions",
            "endpoint": "http://x",
            "api_key_env": "NONEXISTENT_KEY",
        })
        ok, msg = await p.health_check()
        assert not ok

    @pytest.mark.asyncio
    async def test_health_check_ok(self):
        p = LlmSearchProvider("test", config={
            "api_format": "chat_completions",
            "endpoint": "http://x",
        })
        ok, _ = await p.health_check()
        assert ok


# ---------------------------------------------------------------------------
# Gemini-as-LlmSearchProvider tests (gemini is now an llm_search alias)
# ---------------------------------------------------------------------------


def _make_gemini(**overrides):
    config = {
        "api_format": "gemini",
        "api_key_env": "GEMINI_SEARCH_API_KEY",
        "api_key_env_fallback": "GOOGLE_STUDIO_API_KEY",
    }
    config.update(overrides)
    return LlmSearchProvider("gemini", config=config)


class TestGeminiProviderRefactored:
    def test_uses_gemini_format(self):
        p = _make_gemini()
        assert isinstance(p._format, GeminiFormat)

    def test_dual_key_fallback(self):
        p = _make_gemini()
        with patch.dict("os.environ", {"GEMINI_SEARCH_API_KEY": ""}, clear=False):
            with patch.dict("os.environ", {"GOOGLE_STUDIO_API_KEY": "fallback-key"}, clear=False):
                assert p._get_key() == "fallback-key"

    def test_primary_key(self):
        p = _make_gemini()
        with patch.dict("os.environ", {"GEMINI_SEARCH_API_KEY": "primary-key"}, clear=False):
            assert p._get_key() == "primary-key"

    @pytest.mark.asyncio
    async def test_search_no_key(self):
        p = _make_gemini()
        with patch.dict("os.environ", {"GEMINI_SEARCH_API_KEY": "", "GOOGLE_STUDIO_API_KEY": ""}, clear=False):
            result = await p.search("query")
            assert result is None

    @pytest.mark.asyncio
    async def test_search_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {"parts": [{"text": "Gemini says"}]},
                "groundingMetadata": {
                    "groundingChunks": [
                        {"web": {"title": "G1", "uri": "https://g1.com"}},
                    ],
                    "groundingSupports": [
                        {"segment": {"text": "snippet"}, "groundingChunkIndices": [0]},
                    ],
                },
            }],
        }

        p = _make_gemini()
        with patch.dict("os.environ", {"GEMINI_SEARCH_API_KEY": "test-key"}, clear=False):
            with patch(_FETCH_PATH, new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = mock_resp
                result = await p.search("test query")

        assert result is not None
        assert result.provider == "gemini"
        assert len(result.results) == 1
        assert result.results[0]["title"] == "G1"
        assert result.answer == "Gemini says"

    @pytest.mark.asyncio
    async def test_search_sets_goog_api_key_header(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {"parts": [{"text": "A"}]},
                "groundingMetadata": {
                    "groundingChunks": [{"web": {"title": "T", "uri": "https://t.com"}}],
                    "groundingSupports": [],
                },
            }],
        }

        p = _make_gemini()
        with patch.dict("os.environ", {"GEMINI_SEARCH_API_KEY": "my-key"}, clear=False):
            with patch(_FETCH_PATH, new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = mock_resp
                await p.search("q")
                call_kwargs = mock_fetch.call_args
                headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
                assert headers.get("x-goog-api-key") == "my-key"
                assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_search_http_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {"error": {"message": "Quota exceeded"}}

        p = _make_gemini()
        with patch.dict("os.environ", {"GEMINI_SEARCH_API_KEY": "key"}, clear=False):
            with patch(_FETCH_PATH, new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = mock_resp
                result = await p.search("query")

        assert result is None


# ---------------------------------------------------------------------------
# Regression tests for review findings
# ---------------------------------------------------------------------------


class TestRegressionFixes:
    """Tests for bugs found during code review."""

    def test_responses_dedup_across_content_blocks(self):
        """seen_urls must be global across all output_text blocks."""
        fmt = ResponsesFormat()
        obj = {
            "output": [{
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "First block",
                        "annotations": [
                            {"type": "url_citation", "url": "https://dup.com", "title": "A"},
                            {"type": "url_citation", "url": "https://unique1.com", "title": "B"},
                        ],
                    },
                    {
                        "type": "output_text",
                        "text": "Second block",
                        "annotations": [
                            {"type": "url_citation", "url": "https://dup.com", "title": "A"},
                            {"type": "url_citation", "url": "https://unique2.com", "title": "C"},
                        ],
                    },
                ],
            }],
        }
        results, _ = fmt.parse_response(obj, 10)
        urls = [r["url"] for r in results]
        assert urls.count("https://dup.com") == 1
        assert len(results) == 3

    def test_responses_dedup_across_message_items(self):
        """seen_urls must be global across multiple message items in output."""
        fmt = ResponsesFormat()
        obj = {
            "output": [
                {
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": "Msg 1",
                        "annotations": [
                            {"type": "url_citation", "url": "https://shared.com", "title": "S"},
                        ],
                    }],
                },
                {
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": "Msg 2",
                        "annotations": [
                            {"type": "url_citation", "url": "https://shared.com", "title": "S"},
                            {"type": "url_citation", "url": "https://new.com", "title": "N"},
                        ],
                    }],
                },
            ],
        }
        results, _ = fmt.parse_response(obj, 10)
        urls = [r["url"] for r in results]
        assert urls.count("https://shared.com") == 1
        assert len(results) == 2

    def test_chat_completions_annotations_dedup_before_truncate(self):
        """Deduplication must happen before max_results truncation."""
        fmt = ChatCompletionsFormat()
        obj = {
            "choices": [{
                "message": {
                    "content": "Answer",
                    "annotations": [
                        {"type": "url_citation", "url": "https://dup.com", "title": "D"},
                        {"type": "url_citation", "url": "https://dup.com", "title": "D"},
                        {"type": "url_citation", "url": "https://dup.com", "title": "D"},
                        {"type": "url_citation", "url": "https://a.com", "title": "A"},
                        {"type": "url_citation", "url": "https://b.com", "title": "B"},
                        {"type": "url_citation", "url": "https://c.com", "title": "C"},
                    ],
                },
            }],
        }
        results, _ = fmt.parse_response(obj, 3)
        assert len(results) == 3
        urls = [r["url"] for r in results]
        assert "https://dup.com" in urls
        assert "https://a.com" in urls
        assert "https://b.com" in urls

    @pytest.mark.asyncio
    async def test_llm_search_skips_when_key_env_set_but_missing(self):
        """Provider should skip cleanly when api_key_env is configured but env var absent."""
        p = LlmSearchProvider("test", config={
            "api_format": "chat_completions",
            "endpoint": "https://api.example.com/v1",
            "model": "m",
            "api_key_env": "DEFINITELY_NOT_SET_XYZ",
        })

        with patch.dict("os.environ", {}, clear=False):
            if "DEFINITELY_NOT_SET_XYZ" in __import__("os").environ:
                del __import__("os").environ["DEFINITELY_NOT_SET_XYZ"]
            result = await p.search("query")

        assert result is None

    @pytest.mark.asyncio
    async def test_llm_search_answer_only_returns_none(self):
        """Provider returns None when response has answer but no results."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Answer without any search results"}}],
        }

        p = LlmSearchProvider("test", config={
            "api_format": "chat_completions",
            "endpoint": "https://api.example.com/v1",
            "model": "m",
        })

        with patch(_FETCH_PATH, new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_resp
            result = await p.search("query")

        assert result is None

    def test_parse_error_extracts_message(self):
        """parse_error should extract error message from response body."""
        from pivot_web_search_mcp.llm_search_formats import ChatCompletionsFormat
        fmt = ChatCompletionsFormat()
        assert fmt.parse_error(401, {"error": {"message": "Unauthorized"}}) == "Unauthorized"
        assert fmt.parse_error(500, {"error": "Server error"}) == "Server error"
        assert fmt.parse_error(503, "plain text") == "HTTP 503"
