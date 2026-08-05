from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    published_at: str = ""
    fetch_url: str = ""


def format_search_results(
    provider_name: str,
    query: str,
    results: list[SearchResult],
    *,
    quality: str | None = None,
    core_terms: tuple[str, ...] = (),
    relevant_count: int = 0,
    fetch_candidates: tuple[SearchResult, ...] = (),
) -> str:
    if not results:
        return (
            f"Search provider: {provider_name}\n"
            f"Query: {query}\n\nNo web results were found."
        )

    lines = [
        f"Search provider: {provider_name}",
        f"Query: {query}",
        f"Results: {len(results)}",
    ]
    if quality:
        terms = ", ".join(core_terms) if core_terms else "(no stable core terms)"
        lines.extend(
            [
                f"Search quality: {quality} "
                f"({relevant_count}/{len(results)} relevant results)",
                f"Core terms: {terms}",
            ]
        )
    lines.append("")
    for index, result in enumerate(results, start=1):
        lines.append(f"{index}. {result.title or '(untitled result)'}")
        lines.append(f"   URL: {result.url}")
        if result.source:
            lines.append(f"   Source: {result.source}")
        if result.published_at:
            lines.append(f"   Published: {result.published_at}")
        if result.snippet:
            lines.append(f"   Summary: {_single_line(result.snippet)}")
        lines.append("")
    if quality == "low":
        lines.extend(
            [
                "Search guidance: Results do not match the core query terms. Do not "
                "claim that the requested fact is unavailable.",
                "Refine the entity query or use a different search source.",
            ]
        )
    elif fetch_candidates:
        lines.append(
            "Fetch guidance: For factual claims that need verification, call web_fetch "
            "for at most 3 of these relevant pages:"
        )
        for candidate in fetch_candidates[:3]:
            lines.append(f"- {candidate.fetch_url or candidate.url}")
    return "\n".join(lines).rstrip()


def _single_line(value: str) -> str:
    return " ".join(value.split())
