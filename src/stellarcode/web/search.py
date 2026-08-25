"""Search-provider selection and graceful fallback for public-web retrieval."""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from threading import local
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from stellarcode.web.model import SearchResult
from stellarcode.web.quality import (
    assess_search_results,
    fallback_entity_query,
    rank_search_results,
)


JsonRequest = Callable[..., dict[str, Any]]


class SearchError(RuntimeError):
    """Raised when a configured search provider cannot complete a request."""


class SearchProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def is_ready(self) -> bool:
        ...

    @abstractmethod
    def unavailable_hint(self) -> str:
        ...

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        ...


class ZhipuSearchProvider(SearchProvider):
    ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/web_search"
    SUPPORTED_ENGINES = frozenset(
        {"search_std", "search_pro", "search_pro_sogou", "search_pro_quark"}
    )

    def __init__(
        self,
        api_key: str | None = None,
        search_engine: str | None = None,
        request_json: JsonRequest | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("GLM_API_KEY", "")).strip()
        self.search_engine = (
            search_engine
            if search_engine is not None
            else os.getenv("ZHIPU_SEARCH_ENGINE", "search_std")
        ).strip()
        self._request_json = request_json or _request_json

    @property
    def name(self) -> str:
        return "zhipu"

    def is_ready(self) -> bool:
        return bool(self.api_key)

    def unavailable_hint(self) -> str:
        return "Set GLM_API_KEY in .env to enable Zhipu web search."

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        _require_query(query)
        if not self.is_ready():
            raise SearchError(self.unavailable_hint())
        if self.search_engine not in self.SUPPORTED_ENGINES:
            supported = ", ".join(sorted(self.SUPPORTED_ENGINES))
            raise SearchError(
                f"Unsupported ZHIPU_SEARCH_ENGINE={self.search_engine!r}. Use one of: {supported}."
            )

        data = self._request_json(
            "POST",
            self.ENDPOINT,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "search_engine": self.search_engine,
                "search_query": query.strip()[:70],
                "search_intent": False,
                "count": _clamp_top_k(top_k, maximum=50),
                "content_size": "medium",
            },
        )
        raw_results = data.get("search_result")
        if not isinstance(raw_results, list):
            return []
        return [
            SearchResult(
                title=_text(item.get("title")),
                url=_text(item.get("link")),
                snippet=_text(item.get("content")),
                source=_text(item.get("media")),
                published_at=_text(item.get("publish_date")),
            )
            for item in raw_results[: _clamp_top_k(top_k, maximum=50)]
            if isinstance(item, dict) and _text(item.get("link"))
        ]


class SerpApiSearchProvider(SearchProvider):
    ENDPOINT = "https://serpapi.com/search.json"

    def __init__(
        self,
        api_key: str | None = None,
        request_json: JsonRequest | None = None,
    ) -> None:
        self.api_key = (
            api_key if api_key is not None else os.getenv("SERPAPI_KEY", "")
        ).strip()
        self._request_json = request_json or _request_json

    @property
    def name(self) -> str:
        return "serpapi"

    def is_ready(self) -> bool:
        return bool(self.api_key)

    def unavailable_hint(self) -> str:
        return "Set SERPAPI_KEY in .env to enable SerpAPI web search."

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        _require_query(query)
        if not self.is_ready():
            raise SearchError(self.unavailable_hint())

        limit = _clamp_top_k(top_k, maximum=20)
        data = self._request_json(
            "GET",
            self.ENDPOINT,
            params={
                "engine": "google",
                "q": query.strip(),
                "api_key": self.api_key,
                "num": limit,
                "hl": "zh-cn",
            },
        )
        raw_results = data.get("organic_results")
        results = []
        if isinstance(raw_results, list):
            results = [
                SearchResult(
                    title=_text(item.get("title")),
                    url=_text(item.get("link")),
                    snippet=_text(item.get("snippet")),
                    source="Google via SerpAPI",
                    published_at=_text(item.get("date")),
                )
                for item in raw_results[:limit]
                if isinstance(item, dict) and _text(item.get("link"))
            ]
        if results:
            return results

        answer_box = data.get("answer_box")
        if not isinstance(answer_box, dict):
            return []
        url = _text(answer_box.get("link"))
        snippet = _text(
            answer_box.get("answer")
            or answer_box.get("snippet")
            or answer_box.get("result")
        )
        if not url and not snippet:
            return []
        return [
            SearchResult(
                title=_text(answer_box.get("title")) or "Direct answer",
                url=url or self.ENDPOINT,
                snippet=snippet,
                source="SerpAPI answer box",
            )
        ]


class SearxngSearchProvider(SearchProvider):
    def __init__(
        self,
        base_url: str | None = None,
        request_json: JsonRequest | None = None,
    ) -> None:
        self.base_url = (
            base_url if base_url is not None else os.getenv("SEARXNG_URL", "")
        ).strip().rstrip("/")
        self._request_json = request_json or _request_json

    @property
    def name(self) -> str:
        return "searxng"

    def is_ready(self) -> bool:
        return self.base_url.startswith(("http://", "https://"))

    def unavailable_hint(self) -> str:
        return "Set SEARXNG_URL to an instance with JSON search enabled."

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        _require_query(query)
        if not self.is_ready():
            raise SearchError(self.unavailable_hint())

        limit = _clamp_top_k(top_k, maximum=20)
        data = self._request_json(
            "GET",
            f"{self.base_url}/search",
            params={
                "q": query.strip(),
                "format": "json",
                "language": "zh-CN",
                "safesearch": 1,
            },
        )
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return []
        return [
            SearchResult(
                title=_text(item.get("title")),
                url=_text(item.get("url")),
                snippet=_text(item.get("content")),
                source=_text(item.get("engine")) or "SearXNG",
                published_at=_text(item.get("publishedDate")),
            )
            for item in raw_results[:limit]
            if isinstance(item, dict) and _text(item.get("url"))
        ]


class WikipediaSearchProvider(SearchProvider):
    ENDPOINT_TEMPLATE = (
        "https://api.wikimedia.org/core/v1/wikipedia/{language}/search/page"
    )

    def __init__(self, request_json: JsonRequest | None = None) -> None:
        self._request_json = request_json or _request_json

    @property
    def name(self) -> str:
        return "wikipedia"

    def is_ready(self) -> bool:
        return True

    def unavailable_hint(self) -> str:
        return ""

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        _require_query(query)
        limit = _clamp_top_k(top_k, maximum=20)
        collected: list[SearchResult] = []
        failures: list[str] = []
        for language in _wikipedia_languages(query):
            try:
                data = self._request_json(
                    "GET",
                    self.ENDPOINT_TEMPLATE.format(language=language),
                    headers={
                        "User-Agent": (
                            "StellarCode-Python/0.1 "
                            "(contact: stellarcode-local@example.invalid)"
                        ),
                        "Api-User-Agent": "StellarCode-Python/0.1",
                    },
                    params={
                        "q": fallback_entity_query(query),
                        "limit": limit,
                    },
                )
            except SearchError as exc:
                failures.append(f"{language}: {exc}")
                continue
            results = _parse_wikimedia_results(data, language, limit)
            collected.extend(results)
            if assess_search_results(query, results).has_relevant_results:
                return rank_search_results(query, results)[:limit]
        if collected:
            return _deduplicate_results(rank_search_results(query, collected))[:limit]
        if failures:
            raise SearchError("Wikimedia search failed. " + "; ".join(failures))
        return []


def _parse_wikimedia_results(
    data: dict[str, Any],
    language: str,
    limit: int,
) -> list[SearchResult]:
        raw_results = data.get("pages")
        if not isinstance(raw_results, list):
            return []
        results: list[SearchResult] = []
        for item in raw_results[:limit]:
            if not isinstance(item, dict):
                continue
            title = _text(item.get("title"))
            if not title:
                continue
            key = _text(item.get("key")) or title.replace(" ", "_")
            snippet = BeautifulSoup(
                " ".join(
                    filter(
                        None,
                        (
                            _text(item.get("description")),
                            _text(item.get("excerpt")),
                        ),
                    )
                ),
                "html.parser",
            ).get_text(" ", strip=True)
            results.append(
                SearchResult(
                    title=title,
                    url=f"https://{language}.wikipedia.org/wiki/{quote(key)}",
                    snippet=re.sub(r"\s+", " ", snippet),
                    source="Wikipedia",
                    fetch_url=(
                        "https://api.wikimedia.org/core/v1/wikipedia/"
                        f"{language}/page/"
                        f"{quote(key)}/html"
                    ),
                )
            )
        return results


class SmartSearchProvider(SearchProvider):
    """Try configured providers in order until one returns relevant results."""

    def __init__(self, providers: list[SearchProvider]) -> None:
        self.providers = _deduplicate_providers(providers)
        if not self.providers:
            raise ValueError("SmartSearchProvider requires at least one provider.")
        self._state = local()

    @property
    def name(self) -> str:
        last_provider_name = getattr(self._state, "last_provider_name", "")
        if last_provider_name:
            return f"smart/{last_provider_name}"
        names = ",".join(provider.name for provider in self.providers)
        return f"smart[{names}]"

    def is_ready(self) -> bool:
        return any(provider.is_ready() for provider in self.providers)

    def unavailable_hint(self) -> str:
        hints = [
            provider.unavailable_hint()
            for provider in self.providers
            if not provider.is_ready() and provider.unavailable_hint()
        ]
        return " ".join(hints) or "No web search provider is ready."

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        _require_query(query)
        limit = _clamp_top_k(top_k, maximum=20)
        collected: list[SearchResult] = []
        failures: list[str] = []

        for provider in self.providers:
            if not provider.is_ready():
                continue
            try:
                results = provider.search(query, limit)
            except SearchError as exc:
                failures.append(f"{provider.name}: {exc}")
                continue

            collected.extend(results)
            assessment = assess_search_results(query, results)
            if assessment.quality == "high":
                self._state.last_provider_name = provider.name
                return rank_search_results(query, results)[:limit]

        merged = _deduplicate_results(rank_search_results(query, collected))
        if merged:
            self._state.last_provider_name = "mixed"
            return merged[:limit]
        if failures:
            raise SearchError("All search providers failed. " + "; ".join(failures))
        return []


class SearchProviderFactory:
    PROVIDERS = {"zhipu", "serpapi", "searxng"}

    @classmethod
    def create(
        cls,
        provider_name: str | None = None,
        request_json: JsonRequest | None = None,
    ) -> SearchProvider:
        selected = (
            provider_name if provider_name is not None else os.getenv("SEARCH_PROVIDER", "")
        ).strip().lower()
        if not selected:
            if os.getenv("GLM_API_KEY", "").strip():
                selected = "zhipu"
            elif os.getenv("SERPAPI_KEY", "").strip():
                selected = "serpapi"
            elif os.getenv("SEARXNG_URL", "").strip():
                selected = "searxng"
            else:
                selected = "zhipu"

        if selected == "zhipu":
            return ZhipuSearchProvider(request_json=request_json)
        if selected == "serpapi":
            return SerpApiSearchProvider(request_json=request_json)
        if selected == "searxng":
            return SearxngSearchProvider(request_json=request_json)
        supported = ", ".join(sorted(cls.PROVIDERS))
        raise ValueError(f"Unknown SEARCH_PROVIDER={selected!r}. Use one of: {supported}.")

    @classmethod
    def create_smart(cls) -> SearchProvider:
        primary = cls.create()
        providers: list[SearchProvider] = [primary]
        alternatives: list[SearchProvider] = [
            SerpApiSearchProvider(),
            SearxngSearchProvider(),
        ]
        providers.extend(
            provider
            for provider in alternatives
            if provider.name != primary.name and provider.is_ready()
        )
        providers.append(WikipediaSearchProvider())
        return SmartSearchProvider(providers)


def _request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=30) as client:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchError(f"Search request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise SearchError("Search provider returned a non-object JSON response.")
    return data


def _require_query(query: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise SearchError("Web search query cannot be empty.")


def _clamp_top_k(top_k: int, maximum: int) -> int:
    return max(1, min(int(top_k), maximum))


def _text(value: object) -> str:
    return str(value or "").strip()


def _deduplicate_providers(providers: list[SearchProvider]) -> list[SearchProvider]:
    deduplicated: list[SearchProvider] = []
    seen: set[str] = set()
    for provider in providers:
        if provider.name in seen:
            continue
        seen.add(provider.name)
        deduplicated.append(provider)
    return deduplicated


def _deduplicate_results(results: list[SearchResult]) -> list[SearchResult]:
    deduplicated: list[SearchResult] = []
    seen: set[str] = set()
    for result in results:
        key = result.url.rstrip("/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduplicated.append(result)
    return deduplicated


def _wikipedia_languages(query: str) -> tuple[str, str]:
    if re.search(r"[\u4e00-\u9fff]", query):
        return ("zh", "en")
    return ("en", "zh")
