"""Tests for the quality gate module."""

from pivot_web_search_mcp.quality_gate import Verdict, _extract_terms, quality_gate


class TestQualityGate:
    def test_accept_on_ai_answer(self):
        answer = "This is a detailed AI-generated answer with enough content to pass."
        result = quality_gate("test query", [], answer=answer)
        assert result == Verdict.ACCEPT

    def test_fail_on_short_answer_no_results(self):
        result = quality_gate("test", [], answer="Short")
        assert result == Verdict.FAIL

    def test_fail_on_empty_results(self):
        result = quality_gate("python tutorial", [])
        assert result == Verdict.FAIL

    def test_partial_on_single_url(self):
        results = [{"url": "https://example.com", "title": "Python tutorial", "snippet": "Learn python"}]
        result = quality_gate("python tutorial", results)
        assert result == Verdict.PARTIAL

    def test_accept_on_two_urls_with_keyword(self):
        results = [
            {"url": "https://a.com", "title": "Python tutorial", "snippet": "Learn python"},
            {"url": "https://b.com", "title": "Python docs", "snippet": "Official docs"},
        ]
        result = quality_gate("python tutorial", results)
        assert result == Verdict.ACCEPT

    def test_partial_on_two_urls_no_keyword_overlap(self):
        results = [
            {"url": "https://a.com", "title": "Random page", "snippet": "Unrelated content"},
            {"url": "https://b.com", "title": "Another page", "snippet": "Nothing relevant"},
        ]
        result = quality_gate("quantum computing advances", results)
        assert result == Verdict.PARTIAL

    def test_accept_on_answer_exactly_40_chars(self):
        answer = "A" * 40
        result = quality_gate("test", [], answer=answer)
        assert result == Verdict.ACCEPT

    def test_fail_on_answer_39_chars_no_results(self):
        answer = "A" * 39
        result = quality_gate("test", [], answer=answer)
        assert result == Verdict.FAIL

    def test_accept_with_answer_whitespace_stripped(self):
        answer = "   " + "A" * 40 + "   "
        result = quality_gate("test", [], answer=answer)
        assert result == Verdict.ACCEPT

    def test_duplicate_urls_count_once(self):
        results = [
            {"url": "https://a.com", "title": "Python", "snippet": "python stuff"},
            {"url": "https://a.com", "title": "Same URL", "snippet": "duplicate"},
        ]
        result = quality_gate("python", results)
        assert result == Verdict.PARTIAL

    def test_none_answer_falls_through(self):
        results = [
            {"url": "https://a.com", "title": "Python guide", "snippet": "python basics"},
            {"url": "https://b.com", "title": "Python ref", "snippet": "python reference"},
        ]
        result = quality_gate("python", results, answer=None)
        assert result == Verdict.ACCEPT

    def test_empty_query_accepts_any_results(self):
        results = [
            {"url": "https://a.com", "title": "Page", "snippet": "content"},
            {"url": "https://b.com", "title": "Page 2", "snippet": "more"},
        ]
        result = quality_gate("", results)
        assert result == Verdict.ACCEPT


class TestExtractTerms:
    def test_removes_stopwords(self):
        terms = _extract_terms("what is the best python tutorial for beginners")
        assert "what" not in terms
        assert "is" not in terms
        assert "the" not in terms
        assert "for" not in terms
        assert "python" in terms
        assert "tutorial" in terms

    def test_max_five_terms(self):
        terms = _extract_terms("alpha bravo charlie delta echo foxtrot golf hotel")
        assert len(terms) <= 5

    def test_sorted_by_length_descending(self):
        terms = _extract_terms("go python javascript")
        assert terms == sorted(terms, key=len, reverse=True)

    def test_single_char_excluded(self):
        terms = _extract_terms("I want a python tutorial")
        assert "I" not in terms
        assert "a" not in terms

    def test_empty_query(self):
        terms = _extract_terms("")
        assert terms == []


class TestKeywordTokenMatch:
    def test_keyword_substring_no_longer_matches(self):
        """Token-based match: query 'ai' should not match a result titled 'available'."""
        results = [
            {"url": "https://a.com", "title": "Available products", "snippet": "Stuff in stock"},
            {"url": "https://b.com", "title": "Going somewhere", "snippet": "Travel content"},
        ]
        # "ai" is not a stopword and length > 1
        result = quality_gate("ai", results)
        assert result == Verdict.PARTIAL


class TestNonEnglishFallback:
    def test_non_english_query_uses_url_count_fallback(self):
        """Chinese query (no English terms after stopword filter) with 2 URLs → ACCEPT via fallback."""
        results = [
            {"url": "https://a.com", "title": "page", "snippet": "content"},
            {"url": "https://b.com", "title": "another", "snippet": "more"},
        ]
        result = quality_gate("人工智能", results)
        assert result == Verdict.ACCEPT
