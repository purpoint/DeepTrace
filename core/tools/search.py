"""Web search, behind a provider interface.

The interface exists for the same reason the LLM one does: the reliability
requirement says a search failure should fall back to another provider rather
than fail the run. That is only implementable if nothing above this layer knows
which search vendor is answering.

Tavily is the first implementation. It returns extracted page content alongside
links, which means many searches need no follow-up fetch at all -- fewer requests
and fewer chances to be blocked by a site.

Results are filtered through the SSRF guard before they are returned. A search
provider is an untrusted source of URLs: a poisoned or compromised result could
point at an internal address, and the guard belongs at the boundary rather than
at whichever caller happens to fetch first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from core.config import Settings, get_settings
from core.logging import get_logger
from core.observability.recorder import RunRecorder
from core.tools.base import (
    ToolConfigurationError,
    ToolRateLimitError,
    ToolRun,
    ToolTimeoutError,
    ToolUnavailableError,
)
from core.tools.url_guard import URLValidationError, validate_url

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One search hit.

    ``content`` is whatever the provider extracted. It is untrusted text and
    must be wrapped before any model sees it.
    """

    url: str
    title: str
    content: str = ""
    published_at: datetime | None = None
    provider_score: float | None = None
    provider: str = ""

    @property
    def domain(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def has_content(self) -> bool:
        """Whether the provider returned enough text to skip a separate fetch."""
        return len(self.content.split()) >= 50


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """The results for one query, plus what was discarded and why."""

    query: str
    results: tuple[SearchResult, ...]
    provider: str
    searched_at: datetime
    blocked: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """URLs the guard refused, as (url, reason). Kept rather than dropped so a
    gap in the evidence is explainable in the trace."""

    def __len__(self) -> int:
        return len(self.results)


@runtime_checkable
class SearchProvider(Protocol):
    """What a search vendor adapter must implement.

    Like the LLM provider interface, an adapter only translates. It does not
    retry, cache, deduplicate, or filter -- those are written once above it.
    """

    @property
    def name(self) -> str: ...

    async def search(
        self, query: str, *, max_results: int = 8, timeout_seconds: float = 30.0
    ) -> list[SearchResult]: ...


class TavilySearchProvider:
    """Tavily adapter."""

    API_URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ToolConfigurationError(
                "Tavily is not configured. Add TAVILY_API_KEY to your .env file "
                "(see .env.example).",
                tool="web_search",
            )
        self._api_key = api_key
        self._client = client

    @property
    def name(self) -> str:
        return "tavily"

    async def search(
        self, query: str, *, max_results: int = 8, timeout_seconds: float = 30.0
    ) -> list[SearchResult]:
        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced",
            "include_answer": False,  # DeepTrace synthesises its own answers
            "include_raw_content": False,
        }

        owns_client = self._client is None
        http = self._client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))
        try:
            response = await http.post(self.API_URL, json=payload)
        except httpx.TimeoutException as exc:
            raise ToolTimeoutError(
                f"Tavily timed out after {timeout_seconds}s", tool="web_search"
            ) from exc
        except httpx.HTTPError as exc:
            raise ToolUnavailableError(f"Could not reach Tavily: {exc}", tool="web_search") from exc
        finally:
            if owns_client:
                await http.aclose()

        if response.status_code == 429:
            raise ToolRateLimitError("Tavily rate limit reached", tool="web_search")
        if response.status_code in (401, 403):
            raise ToolConfigurationError("Tavily rejected the API key", tool="web_search")
        if response.status_code >= 500:
            raise ToolUnavailableError(
                f"Tavily server error {response.status_code}", tool="web_search"
            )
        if response.status_code >= 400:
            raise ToolUnavailableError(
                f"Tavily rejected the request ({response.status_code})", tool="web_search"
            )

        return [self._to_result(item) for item in response.json().get("results", [])]

    def _to_result(self, item: dict[str, Any]) -> SearchResult:
        return SearchResult(
            url=str(item.get("url", "")),
            title=str(item.get("title", "")),
            content=str(item.get("content", "")),
            provider_score=item.get("score"),
            provider=self.name,
        )


async def web_search(
    query: str,
    provider: SearchProvider,
    *,
    max_results: int = 8,
    timeout_seconds: float = 30.0,
    recorder: RunRecorder | None = None,
    research_id: str | None = None,
    task_id: str | None = None,
) -> SearchResponse:
    """Search the web, discarding results the URL guard refuses.

    Deduplicates by canonical URL. Providers return the same page under
    tracking-parameter variants, and researching one page twice costs a fetch
    and an extraction while adding no coverage.
    """
    with ToolRun(
        "web_search",
        recorder,
        research_id=research_id,
        task_id=task_id,
        arguments={"query": query, "max_results": max_results},
    ) as run:
        raw = await provider.search(query, max_results=max_results, timeout_seconds=timeout_seconds)

        kept: list[SearchResult] = []
        blocked: list[tuple[str, str]] = []
        seen: set[str] = set()

        for result in raw:
            if not result.url:
                continue
            try:
                validate_url(result.url)
            except URLValidationError as exc:
                blocked.append((result.url, exc.reason))
                continue

            key = canonical_url(result.url)
            if key in seen:
                continue
            seen.add(key)
            kept.append(result)

        run.result_count = len(kept)
        if blocked:
            log.warning(
                "search.results_blocked",
                research_id=research_id,
                query=query,
                blocked_count=len(blocked),
                reasons=[reason for _, reason in blocked],
            )

        return SearchResponse(
            query=query,
            results=tuple(kept),
            provider=provider.name,
            searched_at=datetime.now(UTC),
            blocked=tuple(blocked),
        )


_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "msclkid",
        "ref",
        "ref_src",
        "source",
        "mc_cid",
        "mc_eid",
    }
)


def canonical_url(url: str) -> str:
    """Normalise a URL so two spellings of the same page compare equal.

    Lowercases the host, drops the fragment, removes tracking parameters, and
    strips a trailing slash. Query parameters that survive are sorted, since
    ordering is not meaningful but does change a string comparison.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    query = "&".join(
        sorted(
            pair
            for pair in parts.query.split("&")
            if pair and pair.split("=")[0].lower() not in _TRACKING_PARAMS
        )
    )
    path = parts.path.rstrip("/") or "/"
    suffix = f"?{query}" if query else ""
    return f"{parts.scheme}://{host}{path}{suffix}"


def build_search_provider(settings: Settings | None = None) -> SearchProvider:
    """Construct the configured search provider.

    Mirrors ``build_provider`` in the LLM layer: adding a vendor means adding a
    branch here and a module implementing the protocol, and nothing above this
    boundary changes.

    It lives beside the providers rather than in whichever module happens to
    compose a run, so every entry point -- CLI, worker, API -- resolves search
    the same way instead of each growing its own construction.
    """
    settings = settings or get_settings()
    if not settings.tavily_api_key:
        raise ToolConfigurationError(
            "No search provider is configured. Add TAVILY_API_KEY to your .env "
            "file (see .env.example). A free key is available at tavily.com.",
            tool="web_search",
        )
    return TavilySearchProvider(api_key=settings.tavily_api_key)
