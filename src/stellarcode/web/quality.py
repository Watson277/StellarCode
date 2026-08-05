from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from stellarcode.web.model import SearchResult


_ASCII_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_-]*|\d+(?:\.\d+)+")
_KOREAN_TERM = re.compile(r"[\uac00-\ud7a3]{2,}")
_HAN_SEQUENCE = re.compile(r"[\u4e00-\u9fff]{2,}")
_INTENT_TERMS = {
    "a",
    "about",
    "and",
    "are",
    "birthday",
    "birth",
    "current",
    "date",
    "find",
    "latest",
    "members",
    "news",
    "of",
    "official",
    "profile",
    "release",
    "releases",
    "search",
    "the",
    "today",
    "what",
    "when",
    "who",
    "whois",
    "\u662f\u8c01",
    "\u4ec0\u4e48",
    "\u54ea\u4e9b",
    "\u6240\u6709",
    "\u6210\u5458",
    "\u751f\u65e5",
    "\u51fa\u751f",
    "\u51fa\u751f\u65e5\u671f",
    "\u5b98\u65b9",
    "\u6700\u65b0",
    "\u8d44\u6599",
    "\u7b80\u4ecb",
    "\uac80\uc0c9",
    "\ub204\uad6c",
    "\uc0dd\ub144\uc6d4\uc77c",
    "\ud504\ub85c\ud544",
}
_TRUSTED_HOST_PARTS = (
    "official",
    "wikipedia.org",
    "wikidata.org",
    "github.com",
    "docs.",
    ".gov",
    ".edu",
)


@dataclass(frozen=True)
class SearchAssessment:
    quality: str
    core_terms: tuple[str, ...]
    relevant_count: int
    total_count: int
    fetch_candidates: tuple[SearchResult, ...]

    @property
    def has_relevant_results(self) -> bool:
        return self.relevant_count > 0


def assess_search_results(
    query: str,
    results: list[SearchResult],
) -> SearchAssessment:
    terms = core_query_terms(query)
    scored = [(_result_score(result, terms), result) for result in results]
    scored.sort(key=lambda item: item[0], reverse=True)
    relevant = [
        result
        for score, result in scored
        if _is_relevant(score, len(terms))
    ]
    relevance_ratio = len(relevant) / max(len(results), 1)
    if relevant and (
        len(results) == 1
        or (len(relevant) >= 2 and relevance_ratio >= 0.5)
    ):
        quality = "high"
    elif relevant:
        quality = "medium"
    else:
        quality = "low"
    return SearchAssessment(
        quality=quality,
        core_terms=tuple(terms),
        relevant_count=len(relevant),
        total_count=len(results),
        fetch_candidates=tuple(relevant[:3]),
    )


def rank_search_results(
    query: str,
    results: list[SearchResult],
) -> list[SearchResult]:
    terms = core_query_terms(query)
    return sorted(
        results,
        key=lambda result: _result_score(result, terms),
        reverse=True,
    )


def core_query_terms(query: str) -> list[str]:
    candidates: list[str] = []
    candidates.extend(_ASCII_TERM.findall(query))
    candidates.extend(_KOREAN_TERM.findall(query))
    for sequence in _HAN_SEQUENCE.findall(query):
        try:
            import jieba

            jieba.setLogLevel(logging.WARNING)
            candidates.extend(jieba.lcut(sequence, cut_all=False))
        except ModuleNotFoundError:
            candidates.append(sequence)

    terms: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = candidate.strip().lower()
        if len(normalized) < 2 or normalized in _INTENT_TERMS or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
    return terms[:8]


def fallback_entity_query(query: str) -> str:
    terms = core_query_terms(query)
    return " ".join(terms) if terms else query.strip()


def _result_score(result: SearchResult, terms: list[str]) -> float:
    title = result.title.lower()
    text = f"{result.title} {result.snippet} {result.url}".lower()
    if not terms:
        coverage = 0.5
        title_coverage = 0.0
    else:
        matched = sum(1 for term in terms if term in text)
        title_matched = sum(1 for term in terms if term in title)
        coverage = matched / len(terms)
        title_coverage = title_matched / len(terms)
    exact_entity_bonus = 0.12 if _normalized_title(title) in terms else 0.0
    return (
        coverage * 0.75
        + title_coverage * 0.15
        + _trust_score(result.url) * 0.1
        + exact_entity_bonus
    )


def _is_relevant(score: float, term_count: int) -> bool:
    if term_count == 0:
        return score >= 0.4
    if term_count == 1:
        return score >= 0.75
    return score >= 0.60


def _trust_score(url: str) -> float:
    host = (urlsplit(url).hostname or "").lower()
    if any(part in host for part in _TRUSTED_HOST_PARTS):
        return 1.0
    if host:
        return 0.4
    return 0.0


def _normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff\uac00-\ud7a3_-]+", "", title.lower())
