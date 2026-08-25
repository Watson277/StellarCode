"""Safe web-search/fetch providers and content extraction for Agent tools."""

from stellarcode.web.fetch import WebFetcher, WebFetchError
from stellarcode.web.model import SearchResult, format_search_results
from stellarcode.web.network import (
    NetworkPolicy,
    NetworkPolicyError,
    SlidingWindowRateLimiter,
)
from stellarcode.web.search import (
    SearchError,
    SearchProvider,
    SearchProviderFactory,
    SearxngSearchProvider,
    SerpApiSearchProvider,
    SmartSearchProvider,
    WikipediaSearchProvider,
    ZhipuSearchProvider,
)
from stellarcode.web.quality import SearchAssessment, assess_search_results

__all__ = [
    "NetworkPolicy",
    "NetworkPolicyError",
    "SearchError",
    "SearchProvider",
    "SearchProviderFactory",
    "SearchResult",
    "SearxngSearchProvider",
    "SerpApiSearchProvider",
    "SmartSearchProvider",
    "SlidingWindowRateLimiter",
    "SearchAssessment",
    "WebFetchError",
    "WebFetcher",
    "WikipediaSearchProvider",
    "ZhipuSearchProvider",
    "assess_search_results",
    "format_search_results",
]
