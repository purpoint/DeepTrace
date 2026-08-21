"""Tests for the search tool.

A search provider is an untrusted source of URLs. A poisoned index or a
compromised result can point at an internal address, so the guard runs at this
boundary rather than at whichever caller happens to fetch first.
"""

from __future__ import annotations

import httpx
import pytest

from core.observability.recorder import InMemoryRunRecorder
from core.tools.base import (
    ToolConfigurationError,
    ToolRateLimitError,
    ToolTimeoutError,
    ToolUnavailableError,
)
from core.tools.search import (
    SearchProvider,
    SearchResult,
    TavilySearchProvider,
    canonical_url,
    web_search,
)

pytestmark = pytest.mark.unit


class StubProvider:
    """A search provider implemented without any vendor SDK."""

    def __init__(self, results: list[SearchResult] | None = None, error: Exception | None = None):
        self._results = results or []
        self._error = error
        self.queries: list[str] = []

    @property
    def name(self) -> str:
        return "stub"

    async def search(
        self, query: str, *, max_results: int = 8, timeout_seconds: float = 30.0
    ) -> list[SearchResult]:
        self.queries.append(query)
        if self._error:
            raise self._error
        return self._results[:max_results]


def result(url: str, title: str = "A page", content: str = "") -> SearchResult:
    return SearchResult(url=url, title=title, content=content, provider="stub")


class TestProviderInterface:
    def test_stub_satisfies_the_protocol(self) -> None:
        assert isinstance(StubProvider(), SearchProvider)

    def test_tavily_satisfies_the_protocol(self) -> None:
        assert isinstance(TavilySearchProvider(api_key="test"), SearchProvider)

    def test_missing_key_fails_at_construction(self) -> None:
        """Better than discovering it on the first search of a research run."""
        with pytest.raises(ToolConfigurationError, match="TAVILY_API_KEY"):
            TavilySearchProvider(api_key="")


class TestResultFiltering:
    async def test_internal_urls_from_search_are_discarded(self) -> None:
        """A poisoned or compromised result must not become a fetch target."""
        provider = StubProvider(
            [
                result("https://kafka.apache.org/documentation/"),
                result("http://169.254.169.254/latest/meta-data/"),
                result("http://127.0.0.1:8080/admin"),
            ]
        )
        response = await web_search("kafka ordering", provider)

        assert len(response.results) == 1
        assert response.results[0].domain == "kafka.apache.org"

    async def test_blocked_urls_are_reported_not_silently_dropped(self) -> None:
        """A gap in the evidence must be explainable in the trace."""
        provider = StubProvider(
            [
                result("https://kafka.apache.org/documentation/"),
                result("http://169.254.169.254/"),
            ]
        )
        response = await web_search("kafka", provider)

        assert len(response.blocked) == 1
        blocked_url, reason = response.blocked[0]
        assert blocked_url == "http://169.254.169.254/"
        assert "metadata" in reason

    async def test_results_without_a_url_are_skipped(self) -> None:
        provider = StubProvider([result(""), result("https://kafka.apache.org/docs")])
        assert len(await web_search("q", provider)) == 1


class TestDeduplication:
    async def test_tracking_parameter_variants_collapse(self) -> None:
        """Researching one page twice costs a fetch and an extraction while
        adding no coverage."""
        provider = StubProvider(
            [
                result("https://kafka.apache.org/docs?utm_source=google"),
                result("https://kafka.apache.org/docs"),
                result("https://www.kafka.apache.org/docs/"),
            ]
        )
        assert len(await web_search("kafka", provider)) == 1

    async def test_different_pages_are_kept(self) -> None:
        provider = StubProvider(
            [
                result("https://kafka.apache.org/docs/ordering"),
                result("https://kafka.apache.org/docs/delivery"),
            ]
        )
        assert len(await web_search("kafka", provider)) == 2

    async def test_the_first_occurrence_wins(self) -> None:
        provider = StubProvider(
            [
                result("https://kafka.apache.org/docs", title="First"),
                result("https://kafka.apache.org/docs/", title="Second"),
            ]
        )
        response = await web_search("kafka", provider)
        assert response.results[0].title == "First"


class TestCanonicalisation:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("https://a.com/p/", "https://a.com/p"),
            ("https://www.a.com/p", "https://a.com/p"),
            ("https://A.com/p", "https://a.com/p"),
            ("https://a.com/p#section", "https://a.com/p"),
            ("https://a.com/p?utm_source=x&id=7", "https://a.com/p?id=7"),
            ("https://a.com/p?b=2&a=1", "https://a.com/p?a=1&b=2"),
        ],
    )
    def test_equivalent_spellings_match(self, left: str, right: str) -> None:
        assert canonical_url(left) == canonical_url(right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("https://a.com/one", "https://a.com/two"),
            ("https://a.com/p?id=1", "https://a.com/p?id=2"),
            ("https://a.com/p", "https://b.com/p"),
        ],
    )
    def test_different_pages_stay_different(self, left: str, right: str) -> None:
        assert canonical_url(left) != canonical_url(right)

    def test_meaningful_query_parameters_survive(self) -> None:
        """Stripping a real parameter would merge genuinely different pages."""
        assert "id=7" in canonical_url("https://a.com/p?id=7&utm_campaign=spring")


class TestErrorMapping:
    """Classification decides whether the retry policy will try again."""

    @pytest.mark.parametrize(
        ("status", "expected", "retryable"),
        [
            (429, ToolRateLimitError, True),
            (500, ToolUnavailableError, True),
            (503, ToolUnavailableError, True),
            (401, ToolConfigurationError, False),
            (403, ToolConfigurationError, False),
        ],
    )
    async def test_http_status_mapping(
        self, status: int, expected: type[Exception], retryable: bool
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TavilySearchProvider(api_key="test", client=client)
            with pytest.raises(expected) as exc:
                await provider.search("kafka")

        assert exc.value.retryable is retryable  # type: ignore[attr-defined]

    async def test_timeout_is_retryable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TavilySearchProvider(api_key="test", client=client)
            with pytest.raises(ToolTimeoutError):
                await provider.search("kafka")


class TestTavilyTranslation:
    async def test_response_becomes_search_results(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://kafka.apache.org/docs",
                            "title": "Kafka Docs",
                            "content": "Kafka preserves order within a partition.",
                            "score": 0.94,
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            results = await TavilySearchProvider(api_key="test", client=client).search("kafka")

        assert results[0].url == "https://kafka.apache.org/docs"
        assert results[0].provider == "tavily"
        assert results[0].provider_score == 0.94

    async def test_missing_fields_do_not_crash(self) -> None:
        """A provider adapter must tolerate a response shape it did not expect
        rather than failing the research run."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": [{"url": "https://a.com/p"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            results = await TavilySearchProvider(api_key="test", client=client).search("q")

        assert results[0].title == ""


class TestSearchResultProperties:
    def test_domain_is_extracted(self) -> None:
        assert result("https://kafka.apache.org/docs").domain == "kafka.apache.org"

    def test_substantial_content_avoids_a_second_fetch(self) -> None:
        assert result("https://a.com/p", content="word " * 60).has_content is True

    def test_a_snippet_is_not_enough_content(self) -> None:
        assert result("https://a.com/p", content="Short snippet.").has_content is False


class TestToolCallRecording:
    async def test_search_is_recorded(self) -> None:
        recorder = InMemoryRunRecorder()
        provider = StubProvider([result("https://kafka.apache.org/docs")])

        await web_search("kafka ordering", provider, recorder=recorder, research_id="res_1")

        call = recorder.tool_calls[0]
        assert call.tool == "web_search"
        assert call.research_id == "res_1"
        assert call.arguments["query"] == "kafka ordering"
        assert call.result_count == 1

    async def test_a_failed_search_is_recorded(self) -> None:
        recorder = InMemoryRunRecorder()
        provider = StubProvider(error=ToolRateLimitError("429", tool="web_search"))

        with pytest.raises(ToolRateLimitError):
            await web_search("kafka", provider, recorder=recorder)

        assert recorder.tool_calls[0].status == "error"
        assert recorder.tool_calls[0].error_type == "ToolRateLimitError"

    async def test_result_count_reflects_what_survived_filtering(self) -> None:
        """The recorded count must be what research can actually use, not what
        the provider returned."""
        recorder = InMemoryRunRecorder()
        provider = StubProvider(
            [result("https://kafka.apache.org/docs"), result("http://127.0.0.1/admin")]
        )

        await web_search("kafka", provider, recorder=recorder)

        assert recorder.tool_calls[0].result_count == 1
