from __future__ import annotations

from typing import Any

import httpx
import pytest

from stellarcode.tools import ToolExecutionError, build_default_registry
from stellarcode.web import (
    NetworkPolicy,
    SearchProvider,
    SearchProviderFactory,
    SearchResult,
    SearxngSearchProvider,
    SerpApiSearchProvider,
    SmartSearchProvider,
    SlidingWindowRateLimiter,
    WebFetchError,
    WebFetcher,
    WikipediaSearchProvider,
    ZhipuSearchProvider,
    assess_search_results,
)
from stellarcode.web.quality import core_query_terms


def test_zhipu_provider_sends_current_api_shape_and_parses_results():
    captured: dict[str, Any] = {}

    def request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(method=method, url=url, **kwargs)
        return {
            "search_result": [
                {
                    "title": "StellarCode news",
                    "link": "https://example.com/news",
                    "content": "A concise result.",
                    "media": "Example",
                    "publish_date": "2026-07-30",
                }
            ]
        }

    provider = ZhipuSearchProvider(
        api_key="test-key",
        search_engine="search_std",
        request_json=request_json,
    )

    results = provider.search("StellarCode latest", top_k=3)

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/paas/v4/web_search")
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["search_query"] == "StellarCode latest"
    assert captured["json"]["count"] == 3
    assert results == [
        SearchResult(
            title="StellarCode news",
            url="https://example.com/news",
            snippet="A concise result.",
            source="Example",
            published_at="2026-07-30",
        )
    ]


def test_serpapi_provider_parses_organic_results():
    provider = SerpApiSearchProvider(
        api_key="test-key",
        request_json=lambda *_args, **_kwargs: {
            "organic_results": [
                {
                    "title": "Python",
                    "link": "https://python.org",
                    "snippet": "Official site",
                }
            ]
        },
    )

    result = provider.search("Python", 5)[0]

    assert result.title == "Python"
    assert result.url == "https://python.org"
    assert result.source == "Google via SerpAPI"


def test_searxng_provider_uses_json_endpoint_and_limits_results():
    captured: dict[str, Any] = {}

    def request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(method=method, url=url, **kwargs)
        return {
            "results": [
                {"title": "One", "url": "https://one.example", "content": "first"},
                {"title": "Two", "url": "https://two.example", "content": "second"},
            ]
        }

    provider = SearxngSearchProvider(
        base_url="http://localhost:8888/",
        request_json=request_json,
    )

    results = provider.search("test", top_k=1)

    assert captured["url"] == "http://localhost:8888/search"
    assert captured["params"]["format"] == "json"
    assert [result.title for result in results] == ["One"]


def test_search_provider_factory_auto_selects_configured_provider(monkeypatch):
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("SERPAPI_KEY", "configured")
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")

    provider = SearchProviderFactory.create()

    assert provider.name == "serpapi"


def test_search_quality_rejects_results_that_only_match_birthday_intent():
    results = [
        SearchResult(
            title="Birthday wishes and quotes",
            url="https://example.com/birthday",
            snippet="Birthday messages for friends and family.",
        ),
        SearchResult(
            title="ILLIT official profile",
            url="https://example.com/illit",
            snippet="General information about ILLIT.",
        ),
    ]

    assessment = assess_search_results("ILLIT Wonhee birthday", results)

    assert assessment.quality == "low"
    assert assessment.relevant_count == 0
    assert assessment.core_terms == ("illit", "wonhee")


def test_query_terms_keep_chinese_entity_and_remove_question_intent():
    assert core_query_terms("\u91d1\u591a\u5a1f\u662f\u8c01") == [
        "\u91d1\u591a\u5a1f"
    ]


def test_search_quality_returns_at_most_three_fetch_candidates():
    results = [
        SearchResult(
            title=f"Wonhee from ILLIT profile {index}",
            url=f"https://example.com/wonhee-{index}",
            snippet="Wonhee is a member of ILLIT.",
        )
        for index in range(5)
    ]

    assessment = assess_search_results("ILLIT Wonhee birthday", results)

    assert assessment.quality == "high"
    assert assessment.relevant_count == 5
    assert len(assessment.fetch_candidates) == 3


def test_search_quality_is_medium_when_most_results_are_off_topic():
    relevant = [
        SearchResult(
            title=f"Hearts2Hearts SM Entertainment result {index}",
            url=f"https://example.com/relevant-{index}",
        )
        for index in range(4)
    ]
    irrelevant = [
        SearchResult(
            title=f"Unrelated result {index}",
            url=f"https://example.com/unrelated-{index}",
        )
        for index in range(6)
    ]

    assessment = assess_search_results(
        "Hearts2Hearts members SM Entertainment",
        relevant + irrelevant,
    )

    assert assessment.quality == "medium"
    assert assessment.relevant_count == 4


def test_search_ranking_prefers_exact_entity_title_over_context_page():
    results = [
        SearchResult(
            title="List of SM Entertainment artists",
            url="https://en.wikipedia.org/wiki/List_of_SM_Entertainment_artists",
            snippet="SM Entertainment launched Hearts2Hearts in 2025.",
        ),
        SearchResult(
            title="Hearts2Hearts",
            url="https://en.wikipedia.org/wiki/Hearts2Hearts",
            snippet="Hearts2Hearts is an eight-member group formed by SM Entertainment.",
        ),
    ]

    assessment = assess_search_results(
        "Hearts2Hearts members SM Entertainment",
        results,
    )

    assert assessment.fetch_candidates[0].title == "Hearts2Hearts"


def test_wikipedia_provider_uses_entity_terms_and_parses_html_snippet():
    captured: dict[str, Any] = {}

    def request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(method=method, url=url, **kwargs)
        return {
            "pages": [
                {
                    "key": "Wonhee_(singer)",
                    "title": "Wonhee (singer)",
                    "description": "South Korean singer",
                    "excerpt": "<span class='searchmatch'>Wonhee</span> is in ILLIT.",
                }
            ]
        }

    result = WikipediaSearchProvider(request_json=request_json).search(
        "ILLIT Wonhee birthday",
        3,
    )[0]

    assert captured["params"]["q"] == "illit wonhee"
    assert captured["params"]["limit"] == 3
    assert captured["headers"]["User-Agent"].startswith("StellarCode-Python/")
    assert result.title == "Wonhee (singer)"
    assert result.snippet == "South Korean singer Wonhee is in ILLIT."
    assert result.url.startswith("https://en.wikipedia.org/wiki/Wonhee_")
    assert result.fetch_url.startswith(
        "https://api.wikimedia.org/core/v1/wikipedia/en/page/Wonhee_"
    )


def test_wikipedia_provider_prefers_chinese_for_chinese_entity_query():
    captured_urls: list[str] = []

    def request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        captured_urls.append(url)
        return {
            "pages": [
                {
                    "key": "\u91d1\u591a\u5a1f",
                    "title": "\u91d1\u591a\u5a1f",
                    "description": "\u97e9\u56fd\u5973\u6b4c\u624b",
                    "excerpt": "\u91d1\u591a\u5a1f\u662f\u97e9\u56fd\u5973\u5b50\u7ec4\u5408\u6210\u5458\u3002",
                }
            ]
        }

    result = WikipediaSearchProvider(request_json=request_json).search(
        "\u91d1\u591a\u5a1f\u662f\u8c01",
        3,
    )[0]

    assert "/wikipedia/zh/search/page" in captured_urls[0]
    assert len(captured_urls) == 1
    assert result.url.startswith("https://zh.wikipedia.org/wiki/")
    assert "/wikipedia/zh/page/" in result.fetch_url


class _FixedSearchProvider(SearchProvider):
    def __init__(self, name: str, results: list[SearchResult]) -> None:
        self._name = name
        self.results = results
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    def is_ready(self) -> bool:
        return True

    def unavailable_hint(self) -> str:
        return ""

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        self.calls += 1
        return self.results[:top_k]


def test_smart_search_falls_back_when_primary_results_are_off_topic():
    primary = _FixedSearchProvider(
        "primary",
        [
            SearchResult(
                title="Birthday calculator",
                url="https://example.com/calculator",
                snippet="Calculate any birthday.",
            )
        ],
    )
    fallback = _FixedSearchProvider(
        "fallback",
        [
            SearchResult(
                title="Wonhee (singer)",
                url="https://en.wikipedia.org/wiki/Wonhee_(singer)",
                snippet="Wonhee is a South Korean singer and member of ILLIT.",
            )
        ],
    )
    provider = SmartSearchProvider([primary, fallback])

    results = provider.search("ILLIT Wonhee birthday", 5)

    assert primary.calls == 1
    assert fallback.calls == 1
    assert provider.name == "smart/fallback"
    assert results[0].title == "Wonhee (singer)"


def test_smart_search_continues_after_medium_quality_primary_results():
    primary_results = [
        SearchResult(
            title=f"Hearts2Hearts SM Entertainment event {index}",
            url=f"https://example.com/event-{index}",
        )
        for index in range(4)
    ] + [
        SearchResult(
            title=f"Unrelated page {index}",
            url=f"https://example.com/unrelated-{index}",
        )
        for index in range(6)
    ]
    primary = _FixedSearchProvider("primary", primary_results)
    fallback = _FixedSearchProvider(
        "fallback",
        [
            SearchResult(
                title="Hearts2Hearts members",
                url="https://example.com/members",
                snippet=(
                    "Hearts2Hearts is formed by SM Entertainment and consists of "
                    "Carmen, Jiwoo, Yuha, Stella, Juun, A-na, Ian, and Ye-on."
                ),
            )
        ],
    )
    provider = SmartSearchProvider([primary, fallback])

    results = provider.search("Hearts2Hearts members SM Entertainment", 10)

    assert primary.calls == 1
    assert fallback.calls == 1
    assert provider.name == "smart/fallback"
    assert results[0].url == "https://example.com/members"


def test_smart_search_stops_after_relevant_primary_results():
    primary = _FixedSearchProvider(
        "primary",
        [
            SearchResult(
                title="Wonhee from ILLIT",
                url="https://example.com/wonhee",
                snippet="Wonhee is a member of ILLIT.",
            )
        ],
    )
    fallback = _FixedSearchProvider("fallback", [])
    provider = SmartSearchProvider([primary, fallback])

    provider.search("ILLIT Wonhee birthday", 5)

    assert primary.calls == 1
    assert fallback.calls == 0


class _StubSearchProvider(SearchProvider):
    @property
    def name(self) -> str:
        return "stub"

    def is_ready(self) -> bool:
        return True

    def unavailable_hint(self) -> str:
        return ""

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                title="Current result",
                url="https://example.com/current",
                snippet=f"Result for {query}",
                published_at="2026-07-30",
            )
        ][:top_k]


class _UnavailableSearchProvider(_StubSearchProvider):
    def is_ready(self) -> bool:
        return False

    def unavailable_hint(self) -> str:
        return "Configure a test provider key."


def test_web_search_tool_formats_provider_results(tmp_path):
    registry = build_default_registry(
        tmp_path,
        search_provider=_StubSearchProvider(),
    )

    output = registry.execute("web_search", {"query": "latest release", "top_k": 1})

    assert "Search provider: stub" in output
    assert "Current result" in output
    assert "https://example.com/current" in output
    assert "Published: 2026-07-30" in output
    assert "Search quality:" in output
    assert "Fetch guidance:" in output


def test_web_search_tool_reports_unavailable_provider_without_request(tmp_path):
    registry = build_default_registry(
        tmp_path,
        search_provider=_UnavailableSearchProvider(),
    )

    with pytest.raises(ToolExecutionError, match="Configure a test provider key"):
        registry.execute("web_search", {"query": "latest release"})


def _public_policy() -> NetworkPolicy:
    return NetworkPolicy(resolver=lambda _host, _port: ["93.184.216.34"])


def test_web_fetch_streams_plain_text_and_reports_truncation(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="abcdef",
            headers={"content-type": "text/plain; charset=utf-8"},
            request=request,
        )

    fetcher = WebFetcher(
        network_policy=_public_policy(),
        transport=httpx.MockTransport(handler),
    )
    registry = build_default_registry(tmp_path, web_fetcher=fetcher)

    output = registry.execute(
        "web_fetch",
        {"url": "https://example.test/start", "max_chars": 4},
    )

    assert "URL: https://example.test/start" in output
    assert "Status: 200" in output
    assert "abcd" in output
    assert "content truncated at 4 chars" in output


def test_web_fetch_extracts_article_and_removes_navigation():
    html = """
    <html><body>
      <nav>Account Pricing Login</nav>
      <article>
        <h1>Useful title</h1>
        <p>This is the useful article body with enough text to be selected and returned.</p>
        <a href="https://example.com/source">Source link</a>
      </article>
      <footer>Copyright noise</footer>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=html,
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    fetcher = WebFetcher(
        network_policy=_public_policy(),
        transport=httpx.MockTransport(handler),
    )

    output = fetcher.fetch("https://example.test/article")

    assert "# Useful title" in output
    assert "useful article body" in output
    assert "[Source link](https://example.com/source)" in output
    assert "Account Pricing Login" not in output
    assert "Copyright noise" not in output


def test_web_fetch_keeps_wikimedia_lead_before_longer_sections():
    html = """
    <html><body>
      <section data-mw-section-id="0">
        <p>Wonhee was born on June 26, 2007, and is a member of Illit.</p>
      </section>
      <section data-mw-section-id="1">
        <h2>Career</h2>
        <p>This much longer career section contains enough repeated details to win a
        generic longest-block heuristic, but it must not hide the lead biography.</p>
      </section>
    </body></html>
    """

    fetcher = WebFetcher(
        network_policy=_public_policy(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=html,
                headers={"content-type": "text/html; charset=utf-8"},
                request=request,
            )
        ),
    )

    output = fetcher.fetch("https://api.wikimedia.test/page/Wonhee/html")

    assert "Wonhee was born on June 26, 2007" in output
    assert output.index("Wonhee was born") < output.index("## Career")


def test_web_fetch_rewrites_public_wikipedia_url_to_wikimedia_api():
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
                text=(
                    "<html><body><section data-mw-section-id='0'>"
                    "<p>Hearts2Hearts has eight members and debuted under "
                    "SM Entertainment in 2025.</p>"
                    "</section></body></html>"
                ),
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    fetcher = WebFetcher(
        network_policy=_public_policy(),
        transport=httpx.MockTransport(handler),
    )

    output = fetcher.fetch("https://zh.wikipedia.org/wiki/Hearts2Hearts")

    assert requested_urls == [
        "https://api.wikimedia.org/core/v1/wikipedia/zh/page/Hearts2Hearts/html"
    ]
    assert "Hearts2Hearts has eight members" in output


def test_web_fetch_limits_streamed_body_bytes():
    fetcher = WebFetcher(
        network_policy=_public_policy(),
        max_body_bytes=4,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"abcdefgh",
                headers={"content-type": "text/plain; charset=utf-8"},
                request=request,
            )
        ),
    )

    output = fetcher.fetch("https://example.test/large")

    assert "\n\nabcd" in output
    assert "response body exceeded 4 bytes" in output
    assert "efgh" not in output


def test_web_fetch_blocks_private_address_before_request():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="should not run", request=request)

    fetcher = WebFetcher(
        network_policy=NetworkPolicy(
            resolver=lambda _host, _port: ["127.0.0.1"]
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(WebFetchError, match="non-public address"):
        fetcher.fetch("https://internal.example/secret")

    assert not called


def test_web_fetch_revalidates_redirect_target():
    requests: list[str] = []

    def resolver(host: str, _port: int) -> list[str]:
        if host == "public.example":
            return ["93.184.216.34"]
        return ["10.0.0.8"]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://private.example/admin"},
            request=request,
        )

    fetcher = WebFetcher(
        network_policy=NetworkPolicy(resolver=resolver),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(WebFetchError, match="non-public address"):
        fetcher.fetch("https://public.example/start")

    assert requests == ["https://public.example/start"]


def test_web_fetch_rate_limiter_is_shared_across_calls():
    clock_values = iter([0.0, 1.0])
    limiter = SlidingWindowRateLimiter(
        max_requests=1,
        window_seconds=60,
        clock=lambda: next(clock_values),
    )
    fetcher = WebFetcher(
        network_policy=_public_policy(),
        rate_limiter=limiter,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="ok", request=request)
        ),
    )

    fetcher.fetch("https://example.test/one")
    with pytest.raises(WebFetchError, match="rate limit"):
        fetcher.fetch("https://example.test/two")


def test_web_fetch_tool_rejects_non_http_urls(tmp_path):
    registry = build_default_registry(tmp_path)

    with pytest.raises(ToolExecutionError, match="http:// or https://"):
        registry.execute("web_fetch", {"url": "file:///etc/passwd"})
