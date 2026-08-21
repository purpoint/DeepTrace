"""URL fetching and page extraction.

Three properties matter more than the parsing:

*Every hop is validated.* Redirects are followed manually rather than by the
HTTP client, because validating only the first URL is the standard way SSRF
protection is defeated -- a public URL answers ``302 Location: http://127.0.0.1``
and an auto-following client walks straight into the internal network.

*The response is capped while streaming.* Checking ``Content-Length`` is not
enough; a hostile or broken server can omit it or lie. Bytes are counted as they
arrive and the connection is dropped the moment the limit is passed, so an
endless response cannot exhaust the worker.

*Extraction is deterministic.* No model is involved in turning HTML into text.
The tool layer must not reason, so extraction strips chrome by structure and
leaves interpretation to the evidence agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from core.logging import get_logger
from core.observability.recorder import RunRecorder
from core.tools.base import (
    SourceFetchError,
    ToolRun,
    ToolTimeoutError,
    ToolUnavailableError,
)
from core.tools.url_guard import (
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    URLValidationError,
    validate_url,
)

log = get_logger(__name__)

USER_AGENT = "DeepTrace/0.1 (research agent; +https://github.com/purpoint/DeepTrace)"

# Content types worth extracting text from. Anything else is fetched only to be
# rejected, so it is refused up front instead.
_TEXTUAL_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "application/xml")

# Structural chrome that is never evidence. Removed before text extraction so a
# navigation menu cannot be quoted as a source passage.
_CHROME_SELECTORS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "button",
    "iframe",
    "figure figcaption",
    "[aria-hidden='true']",
    "[role='navigation']",
)

_WHITESPACE = re.compile(r"[ \t\xa0]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """A retrieved page, with provenance.

    ``final_url`` is recorded separately from the requested URL because
    redirects mean the two can differ, and a citation must point at what was
    actually read.
    """

    url: str
    final_url: str
    status_code: int
    content_type: str
    html: str
    fetched_at: datetime
    bytes_received: int
    redirect_chain: tuple[str, ...] = ()

    @property
    def was_redirected(self) -> bool:
        return bool(self.redirect_chain)


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """Readable text extracted from a page."""

    url: str
    final_url: str
    title: str
    text: str
    fetched_at: datetime
    word_count: int
    truncated: bool = False

    @property
    def is_substantive(self) -> bool:
        """Whether there is enough text to be worth extracting evidence from.

        Short pages are usually error pages, paywalls, or consent interstitials.
        Treating them as sources produces citations that support nothing.
        """
        return self.word_count >= 50


def _clean(text: str) -> str:
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def extract_text(html: str) -> tuple[str, str]:
    """Turn HTML into a title and readable text.

    Deterministic and model-free. Prefers a semantic content container when the
    page provides one, because that removes sidebars and related-article lists
    that would otherwise be extracted as though they were the article.
    """
    tree = HTMLParser(html)

    title = ""
    if tree.head is not None:
        node = tree.head.css_first("title")
        if node is not None:
            title = _clean(node.text(deep=True))
    if not title:
        heading = tree.css_first("h1")
        title = _clean(heading.text(deep=True)) if heading else ""

    for selector in _CHROME_SELECTORS:
        for node in tree.css(selector):
            node.decompose()

    container = None
    for selector in ("article", "main", "[role='main']", "#content", ".content"):
        container = tree.css_first(selector)
        if container is not None:
            break
    body = container or tree.body
    text = _clean(body.text(separator="\n", deep=True)) if body is not None else ""

    return title, text


async def fetch_url(
    url: str,
    *,
    timeout_seconds: float = 20.0,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    allow_private: bool = False,
    recorder: RunRecorder | None = None,
    research_id: str | None = None,
    task_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> FetchedPage:
    """Fetch a URL, validating the destination at every redirect hop.

    Raises:
        SourceFetchError: The URL was blocked, returned an error status, or
            served content that is not text.
        ToolTimeoutError / ToolUnavailableError: Transient, worth retrying.
    """
    with ToolRun(
        "fetch_url",
        recorder,
        research_id=research_id,
        task_id=task_id,
        arguments={"url": url},
    ) as run:
        owns_client = client is None
        http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,  # every hop is validated by hand
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"},
        )

        try:
            current = url
            chain: list[str] = []

            for _ in range(max_redirects + 1):
                try:
                    validate_url(current, allow_private=allow_private)
                except URLValidationError as exc:
                    raise SourceFetchError(current, exc.reason) from exc

                try:
                    request = http.build_request("GET", current)
                    response = await http.send(request, stream=True)
                except httpx.TimeoutException as exc:
                    raise ToolTimeoutError(
                        f"Timed out after {timeout_seconds}s fetching {current}",
                        tool="fetch_url",
                    ) from exc
                except httpx.HTTPError as exc:
                    raise ToolUnavailableError(
                        f"Network error fetching {current}: {exc}", tool="fetch_url"
                    ) from exc

                try:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise SourceFetchError(current, "redirect without a Location header")
                        chain.append(current)
                        current = urljoin(current, location)
                        continue

                    if response.status_code >= 400:
                        raise SourceFetchError(
                            current,
                            f"HTTP {response.status_code}",
                            status_code=response.status_code,
                        )

                    content_type = response.headers.get("content-type", "").split(";")[0].strip()
                    if content_type and not content_type.startswith(_TEXTUAL_TYPES):
                        raise SourceFetchError(
                            current, f"content type {content_type!r} is not text"
                        )

                    # Count bytes as they arrive. Content-Length may be absent or
                    # untrue, so the cap is enforced against what is received.
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > max_bytes:
                            raise SourceFetchError(
                                current,
                                f"response exceeded {max_bytes} bytes",
                            )
                        chunks.append(chunk)

                    body = b"".join(chunks)
                    run.result_bytes = received

                    return FetchedPage(
                        url=url,
                        final_url=current,
                        status_code=response.status_code,
                        content_type=content_type or "text/html",
                        html=body.decode(response.encoding or "utf-8", errors="replace"),
                        fetched_at=datetime.now(UTC),
                        bytes_received=received,
                        redirect_chain=tuple(chain),
                    )
                finally:
                    await response.aclose()

            raise SourceFetchError(url, f"exceeded {max_redirects} redirects")
        finally:
            if owns_client:
                await http.aclose()


async def extract_page(
    url: str,
    *,
    max_words: int = 8000,
    **kwargs: object,
) -> ExtractedPage:
    """Fetch a URL and extract its readable text.

    ``max_words`` bounds what reaches a model. A long specification can exceed a
    context window on its own, and truncation here is visible in the result
    rather than happening silently inside a prompt.
    """
    page = await fetch_url(url, **kwargs)  # type: ignore[arg-type]
    title, text = extract_text(page.html)

    words = text.split()
    truncated = len(words) > max_words
    if truncated:
        text = " ".join(words[:max_words])

    return ExtractedPage(
        url=page.url,
        final_url=page.final_url,
        title=title,
        text=text,
        fetched_at=page.fetched_at,
        word_count=min(len(words), max_words),
        truncated=truncated,
    )
