"""Tests for URL fetching and page extraction.

All HTTP is served by a mock transport, so nothing leaves the machine. URLs use
public IP literals rather than hostnames: an IP literal resolves without a DNS
query, which keeps the suite fast and working on a machine with no network.
"""

from __future__ import annotations

import httpx
import pytest

from core.observability.recorder import InMemoryRunRecorder
from core.tools.base import SourceFetchError, ToolTimeoutError, ToolUnavailableError
from core.tools.fetch import extract_page, extract_text, fetch_url

pytestmark = pytest.mark.unit

# A public address that needs no DNS lookup. No connection is ever made; the
# mock transport answers every request.
PUBLIC = "http://1.1.1.1:8080/docs"

PAGE = """
<html>
  <head><title>Kafka Documentation</title></head>
  <body>
    <nav>Home About Contact</nav>
    <script>trackEverything();</script>
    <article>
      <h1>Message Ordering</h1>
      <p>Kafka preserves order within a partition.</p>
    </article>
    <footer>Copyright notice</footer>
  </body>
</html>
"""


def transport(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        follow_redirects=False,
    )


def respond(html: str = PAGE, **kwargs: object) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=html, **kwargs)  # type: ignore[arg-type]

    return handler


class TestRedirectValidation:
    """The most commonly defeated SSRF defence: validating only the first URL.

    A public page answers 302 with an internal Location, and a client that
    follows redirects automatically walks into the private network.
    """

    async def test_redirect_to_loopback_is_blocked(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "1.1.1.1":
                return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
            return httpx.Response(200, html="<html><body>internal secrets</body></html>")

        async with transport(handler) as client:
            with pytest.raises(SourceFetchError) as exc:
                await fetch_url(PUBLIC, client=client)

        assert "loopback" in exc.value.reason
        assert "127.0.0.1" in exc.value.url

    async def test_redirect_to_cloud_metadata_is_blocked(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "1.1.1.1":
                return httpx.Response(
                    302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
                )
            return httpx.Response(200, html="<html><body>credentials</body></html>")

        async with transport(handler) as client:
            with pytest.raises(SourceFetchError, match="metadata"):
                await fetch_url(PUBLIC, client=client)

    async def test_relative_redirect_is_resolved_then_validated(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path == "/docs":
                return httpx.Response(302, headers={"location": "/docs/ordering"})
            return httpx.Response(200, html=PAGE)

        async with transport(handler) as client:
            page = await fetch_url(PUBLIC, client=client)

        assert page.final_url.endswith("/docs/ordering")
        assert page.was_redirected

    async def test_redirect_chain_is_recorded(self) -> None:
        """A citation must point at what was actually read, and the path taken
        to get there is part of the trace."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/docs":
                return httpx.Response(302, headers={"location": "http://1.1.1.1:8080/a"})
            if request.url.path == "/a":
                return httpx.Response(302, headers={"location": "http://1.1.1.1:8080/b"})
            return httpx.Response(200, html=PAGE)

        async with transport(handler) as client:
            page = await fetch_url(PUBLIC, client=client)

        assert len(page.redirect_chain) == 2
        assert page.final_url.endswith("/b")
        assert page.url == PUBLIC  # the requested URL is preserved

    async def test_redirect_loop_is_bounded(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": PUBLIC})

        async with transport(handler) as client:
            with pytest.raises(SourceFetchError, match="redirect"):
                await fetch_url(PUBLIC, client=client, max_redirects=3)

    async def test_redirect_without_location_is_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302)

        async with transport(handler) as client:
            with pytest.raises(SourceFetchError, match="Location"):
                await fetch_url(PUBLIC, client=client)


class TestResponseLimits:
    async def test_oversized_response_is_aborted(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"x" * 200_000, headers={"content-type": "text/html"}
            )

        async with transport(handler) as client:
            with pytest.raises(SourceFetchError, match="exceeded"):
                await fetch_url(PUBLIC, client=client, max_bytes=10_000)

    async def test_the_limit_is_enforced_on_bytes_received_not_content_length(self) -> None:
        """A hostile or broken server can omit Content-Length or lie about it,
        so the cap is applied to what actually arrives."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"y" * 100_000,
                headers={"content-type": "text/html", "content-length": "10"},
            )

        async with transport(handler) as client:
            with pytest.raises(SourceFetchError, match="exceeded"):
                await fetch_url(PUBLIC, client=client, max_bytes=1_000)

    async def test_a_normal_page_is_within_limits(self) -> None:
        async with transport(respond()) as client:
            page = await fetch_url(PUBLIC, client=client)

        assert page.bytes_received > 0
        assert page.status_code == 200


class TestContentTypeFiltering:
    @pytest.mark.parametrize(
        "content_type",
        ["application/pdf", "image/png", "application/zip", "video/mp4"],
    )
    async def test_non_text_content_is_refused(self, content_type: str) -> None:
        """Fetching a binary only to reject it wastes bandwidth and time."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"...", headers={"content-type": content_type})

        async with transport(handler) as client:
            with pytest.raises(SourceFetchError, match="not text"):
                await fetch_url(PUBLIC, client=client)

    @pytest.mark.parametrize(
        "content_type", ["text/html", "text/html; charset=utf-8", "text/plain"]
    )
    async def test_textual_content_is_accepted(self, content_type: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=PAGE.encode(), headers={"content-type": content_type}
            )

        async with transport(handler) as client:
            assert (await fetch_url(PUBLIC, client=client)).status_code == 200


class TestErrorMapping:
    @pytest.mark.parametrize("status", [400, 403, 404, 500, 503])
    async def test_error_statuses_become_source_fetch_errors(self, status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status)

        async with transport(handler) as client:
            with pytest.raises(SourceFetchError) as exc:
                await fetch_url(PUBLIC, client=client)

        assert exc.value.status_code == status
        assert exc.value.retryable is False

    async def test_timeout_is_retryable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        async with transport(handler) as client:
            with pytest.raises(ToolTimeoutError) as exc:
                await fetch_url(PUBLIC, client=client)

        assert exc.value.retryable is True

    async def test_network_failure_is_retryable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        async with transport(handler) as client:
            with pytest.raises(ToolUnavailableError) as exc:
                await fetch_url(PUBLIC, client=client)

        assert exc.value.retryable is True

    async def test_a_blocked_url_never_reaches_the_network(self) -> None:
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(200, html=PAGE)

        async with transport(handler) as client:
            with pytest.raises(SourceFetchError):
                await fetch_url("http://169.254.169.254/latest/meta-data/", client=client)

        assert requests == []


class TestToolCallRecording:
    async def test_successful_fetch_is_recorded(self) -> None:
        recorder = InMemoryRunRecorder()
        async with transport(respond()) as client:
            await fetch_url(PUBLIC, client=client, recorder=recorder, research_id="res_1")

        call = recorder.tool_calls[0]
        assert call.tool == "fetch_url"
        assert call.research_id == "res_1"
        assert call.status == "success"
        assert call.result_bytes and call.result_bytes > 0

    async def test_failed_fetch_is_also_recorded(self) -> None:
        """A source that could not be fetched explains a gap in the evidence.
        Dropping the record leaves the gap unaccounted for."""
        recorder = InMemoryRunRecorder()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        async with transport(handler) as client:
            with pytest.raises(SourceFetchError):
                await fetch_url(PUBLIC, client=client, recorder=recorder)

        call = recorder.tool_calls[0]
        assert call.status == "error"
        assert call.error_type == "SourceFetchError"
        assert call.succeeded is False


class TestExtraction:
    def test_title_comes_from_the_title_tag(self) -> None:
        title, _ = extract_text(PAGE)
        assert title == "Kafka Documentation"

    def test_title_falls_back_to_h1(self) -> None:
        title, _ = extract_text("<html><body><h1>Fallback Heading</h1></body></html>")
        assert title == "Fallback Heading"

    def test_chrome_is_removed(self) -> None:
        """A navigation menu must never be quotable as a source passage."""
        _, text = extract_text(PAGE)

        assert "Kafka preserves order within a partition." in text
        assert "trackEverything" not in text
        assert "Home About Contact" not in text
        assert "Copyright notice" not in text

    def test_article_container_is_preferred(self) -> None:
        html = """
        <html><body>
          <aside>Related: buy our product</aside>
          <article><p>The actual documented behaviour.</p></article>
        </body></html>
        """
        _, text = extract_text(html)

        assert "actual documented behaviour" in text
        assert "buy our product" not in text

    def test_extraction_is_model_free(self) -> None:
        """Deterministic: the same HTML always produces the same text. The tool
        layer must not reason."""
        assert extract_text(PAGE) == extract_text(PAGE)

    async def test_extract_page_reports_word_count(self) -> None:
        async with transport(respond()) as client:
            page = await extract_page(PUBLIC, client=client)

        assert page.title == "Kafka Documentation"
        assert page.word_count > 0
        assert page.truncated is False

    async def test_long_pages_are_truncated_visibly(self) -> None:
        """Truncation is reported in the result rather than happening silently
        inside a prompt."""
        long_html = f"<html><body><article>{'word ' * 5000}</article></body></html>"
        async with transport(respond(long_html)) as client:
            page = await extract_page(PUBLIC, client=client, max_words=100)

        assert page.truncated is True
        assert page.word_count == 100

    async def test_thin_pages_are_flagged_as_unsubstantive(self) -> None:
        """Short pages are usually error pages, paywalls, or consent screens.
        Treating them as sources produces citations that support nothing."""
        async with transport(respond("<html><body><p>Access denied.</p></body></html>")) as client:
            page = await extract_page(PUBLIC, client=client)

        assert page.is_substantive is False
