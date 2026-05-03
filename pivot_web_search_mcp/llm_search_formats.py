"""LLM Search format strategies for building requests and parsing responses.

Implements the Strategy pattern for different LLM search API formats:
- ChatCompletionsFormat: OpenAI-compatible /chat/completions with built-in search
- ResponsesFormat: OpenAI Responses API (/responses) with web_search tool
- GeminiFormat: Google Gemini generateContent with Search grounding
"""

import json
from abc import ABC, abstractmethod

from .logging import log

DEFAULT_SYSTEM_PROMPT = "You are a search assistant. Provide a concise answer with source citations."
DEFAULT_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


class LlmSearchFormat(ABC):
    """Abstract base for LLM search request/response formats."""

    @abstractmethod
    def build_request(self, query, max_results, config):
        """Build HTTP request components.

        Returns (url, headers, body_bytes).
        """

    @abstractmethod
    def parse_response(self, obj, max_results, provider_name=""):
        """Parse API response into (results_list, answer_text).

        results_list: list of {title, url, snippet} dicts
        answer_text: AI-generated summary or None
        """

    def parse_error(self, status_code, body):
        """Extract error message from failed response."""
        if isinstance(body, dict):
            err = body.get("error", {})
            if isinstance(err, dict):
                return err.get("message", f"HTTP {status_code}")
            return str(err)
        return f"HTTP {status_code}"


class ChatCompletionsFormat(LlmSearchFormat):
    """Format for OpenAI-compatible /chat/completions with built-in search."""

    def build_request(self, query, max_results, config):
        endpoint = config.get("endpoint", "")
        model = config.get("model", "")
        max_tokens = config.get("max_tokens", 500)
        system_prompt = config.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        headers = dict(config.get("headers", {}))

        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]

        body = {
            "model": model,
            "stream": False,
            "max_tokens": max_tokens,
            "messages": messages,
        }

        web_search_options = config.get("web_search_options")
        if web_search_options:
            body["web_search_options"] = web_search_options

        return endpoint, headers, json.dumps(body).encode("utf-8")

    def parse_response(self, obj, max_results, provider_name=""):
        results = []
        answer = None

        choices = obj.get("choices")
        if choices:
            message = choices[0].get("message", {})
            answer = message.get("content", "")

        # Priority 1: structured search_results
        search_results = obj.get("search_results")
        if search_results:
            for r in search_results[:max_results]:
                if not isinstance(r, dict):
                    continue
                results.append({
                    "title": str(r.get("title", "")),
                    "url": str(r.get("url", "")),
                    "snippet": str(r.get("snippet", "")),
                })
            return results, answer

        if search_results is not None:
            log(f"{provider_name}: search_results is empty, trying annotations")

        # Priority 2: annotations in message (gpt-5-search-api style)
        if choices:
            message = choices[0].get("message", {})
            annotations = message.get("annotations", [])
            url_citations = [
                a for a in annotations
                if isinstance(a, dict) and a.get("type") == "url_citation"
            ]
            if url_citations:
                seen_urls = set()
                for a in url_citations:
                    cite = a.get("url_citation", a)
                    url = str(cite.get("url", ""))
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        results.append({
                            "title": str(cite.get("title", "")),
                            "url": url,
                            "snippet": "",
                        })
                        if len(results) >= max_results:
                            break
                return results, answer

            if annotations:
                log(f"{provider_name}: annotations found but none are url_citation")

        # Priority 3: top-level citations array
        citations = obj.get("citations")
        if citations:
            for url in citations[:max_results]:
                if isinstance(url, str) and url:
                    results.append({"title": "", "url": url, "snippet": ""})
            return results, answer

        if answer and not results:
            log(f"{provider_name}: response has content but no extractable search results"
                " — check if model supports search")

        return results, answer


class ResponsesFormat(LlmSearchFormat):
    """Format for OpenAI Responses API (/responses) with web_search tool."""

    def build_request(self, query, max_results, config):
        endpoint = config.get("endpoint", "")
        model = config.get("model", "")
        max_tokens = config.get("max_tokens", 4000)
        headers = dict(config.get("headers", {}))
        search_tool = config.get("search_tool", "web_search")
        search_context_size = config.get("search_context_size", "medium")

        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"

        tool_config = {"type": search_tool}
        if search_context_size:
            tool_config["search_context_size"] = search_context_size

        filters = config.get("filters")
        if filters:
            tool_config["filters"] = filters

        user_location = config.get("user_location")
        if user_location:
            tool_config["user_location"] = user_location

        body = {
            "model": model,
            "max_output_tokens": max_tokens,
            "input": query,
            "tools": [tool_config],
        }

        return endpoint, headers, json.dumps(body).encode("utf-8")

    def parse_response(self, obj, max_results, provider_name=""):
        results = []
        answer = None
        seen_urls = set()

        output = obj.get("output", [])

        # Find the message item with annotations
        for item in output:
            if item.get("type") != "message":
                continue
            content_list = item.get("content", [])
            for content in content_list:
                if content.get("type") != "output_text":
                    continue
                if not answer:
                    answer = content.get("text", "")
                annotations = content.get("annotations", [])
                for a in annotations:
                    if a.get("type") != "url_citation":
                        continue
                    url = str(a.get("url", ""))
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        results.append({
                            "title": str(a.get("title", "")),
                            "url": url,
                            "snippet": "",
                        })

        # Fallback: output_text at top level
        if not answer:
            answer = obj.get("output_text") or ""

        if max_results and len(results) > max_results:
            results = results[:max_results]

        if answer and not results:
            log(f"{provider_name}: response has content but no extractable search results"
                " — check if model supports search")

        return results, answer


class GeminiFormat(LlmSearchFormat):
    """Format for Google Gemini generateContent with Search grounding."""

    def build_request(self, query, max_results, config):
        base_url = config.get("gemini_url", DEFAULT_GEMINI_URL)
        model = config.get("model", DEFAULT_GEMINI_MODEL)
        endpoint = f"{base_url}/{model}:generateContent"

        headers = {"Content-Type": "application/json"}

        body = {
            "contents": [{"parts": [{"text": f"Search the web for: {query}"}]}],
            "tools": [{"google_search": {}}],
        }

        return endpoint, headers, json.dumps(body).encode("utf-8")

    def parse_response(self, obj, max_results, provider_name=""):
        cand = (obj.get("candidates") or [{}])[0]
        gm = cand.get("groundingMetadata", {})
        chunks = gm.get("groundingChunks", [])
        supports = gm.get("groundingSupports", [])

        if not chunks:
            log(f"{provider_name}: no grounding chunks in response")
            return [], None

        chunk_snippets = {i: [] for i in range(len(chunks))}
        for s in supports:
            text = s.get("segment", {}).get("text", "").strip()
            if text:
                for idx in s.get("groundingChunkIndices", []):
                    if idx in chunk_snippets:
                        chunk_snippets[idx].append(text)

        results = []
        for i, c in enumerate(chunks[:max_results]):
            web = c.get("web", {})
            snippet_parts = chunk_snippets.get(i, [])
            results.append({
                "title": web.get("title", ""),
                "url": web.get("uri", ""),
                "snippet": " ".join(snippet_parts[:2]) if snippet_parts else "",
            })

        answer = (cand.get("content", {}).get("parts") or [{}])[0].get("text", "")
        return results, answer or None


FORMAT_REGISTRY = {
    "chat_completions": ChatCompletionsFormat,
    "responses": ResponsesFormat,
    "gemini": GeminiFormat,
}
