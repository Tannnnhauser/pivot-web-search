"""3-tier quality gate for search result evaluation.

Determines whether a provider's response is good enough to return or
whether routing should continue to the next priority group.
"""

import re
from enum import Enum

_STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might can could of in on at to for "
    "with by from and or not no nor but if then else when where how "
    "what which who whom this that these those it its i me my we our "
    "you your he him his she her they them their".split()
)


class Verdict(str, Enum):
    ACCEPT = "accept"
    PARTIAL = "partial"
    FAIL = "fail"


def quality_gate(
    query: str,
    results: list[dict],
    answer: str | None = None,
) -> Verdict:
    """Evaluate search results against the 3-tier quality gate.

    Gate 0: AI answer with 40+ non-whitespace chars → ACCEPT
    Gate 1: At least 2 unique URLs required
    Gate 2: At least 1 query term in any result title/snippet → ACCEPT
    """
    # Gate 0: AI answer presence
    if answer and len(answer.strip()) >= 40:
        return Verdict.ACCEPT

    if not results:
        return Verdict.FAIL

    # Gate 1: URL count
    unique_urls = {r.get("url") for r in results if r.get("url")}
    if len(unique_urls) < 2:
        return Verdict.PARTIAL if unique_urls else Verdict.FAIL

    # Gate 2: Keyword overlap (word-boundary token match)
    terms = _extract_terms(query)
    if not terms:
        # Non-English / all-stopword queries fall back to URL-count gate
        return Verdict.ACCEPT if len(unique_urls) >= 2 else Verdict.PARTIAL

    term_set = set(terms)
    for r in results:
        text = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
        tokens = set(re.findall(r"\w+", text))
        if term_set & tokens:
            return Verdict.ACCEPT

    return Verdict.PARTIAL


def _extract_terms(query: str) -> list[str]:
    """Extract significant ASCII terms from query, longest first. Max 5."""
    words = re.findall(r"[a-z]+", query.lower())
    terms = [w for w in words if w not in _STOPWORDS and len(w) > 1]
    terms.sort(key=len, reverse=True)
    return terms[:5]
