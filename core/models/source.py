"""Sources: what DeepTrace read, with the metadata needed to judge it.

A source is raw material, not evidence. Keeping the two separate is the point of
the whole design: a source is a document that was retrieved, while evidence is a
specific passage that supports a specific claim. Conflating them is how systems
end up citing a page that never actually said the thing.

Source quality is scored **deterministically from the domain**, not by asking a
model. Three reasons:

*It runs on every result.* A scorer that costs an API call would dominate the
budget of a research run that looks at fifty sources.

*It must be stable.* Reproducing a run means the same source scoring the same
way, and a model asked twice will not always agree with itself.

*It is defensible.* "The Kafka documentation outranks a forum post because
official documentation is a primary source" is a rule you can state and argue
with. "The model gave it 0.82" is not.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class SourceType(StrEnum):
    """What kind of document this is, in the project's preference order.

    The ordering is from the architecture document: official documentation and
    primary research outrank secondary commentary, and community sources are
    usable but must be labelled as such.
    """

    OFFICIAL_DOCS = "official_docs"
    ACADEMIC_PAPER = "academic_paper"
    STANDARD = "standard"
    ENGINEERING_BLOG = "engineering_blog"
    TECHNICAL_PUBLICATION = "technical_publication"
    COMMUNITY = "community"
    UNKNOWN = "unknown"


# Ordered most specific first: the first match wins, so a rule that would also
# match something more general has to come earlier.
_TYPE_RULES: list[tuple[re.Pattern[str], SourceType]] = [
    # Standards bodies. Checked first because ietf.org would otherwise look
    # like an ordinary organisation domain.
    (
        re.compile(r"(^|\.)(ietf|rfc-editor|w3|iso|iec|ecma-international|oasis-open)\.org$"),
        SourceType.STANDARD,
    ),
    (re.compile(r"(^|\.)(unicode|khronos)\.org$"), SourceType.STANDARD),
    # Academic and research.
    (re.compile(r"(^|\.)(arxiv|biorxiv|medrxiv)\.org$"), SourceType.ACADEMIC_PAPER),
    (
        re.compile(
            r"(^|\.)(doi|dl\.acm|ieeexplore\.ieee|link\.springer|sciencedirect|jstor|pubmed\.ncbi\.nlm\.nih)\.(org|com|gov)$"
        ),
        SourceType.ACADEMIC_PAPER,
    ),
    (re.compile(r"\.edu$|\.ac\.[a-z]{2}$"), SourceType.ACADEMIC_PAPER),
    # Community and user-generated. Before the blog rules, since many of these
    # sit on ordinary .com domains.
    (
        re.compile(r"(^|\.)(stackoverflow|stackexchange|superuser|serverfault|askubuntu)\.com$"),
        SourceType.COMMUNITY,
    ),
    (
        re.compile(r"(^|\.)(reddit|quora|medium|dev|hashnode|substack)\.(com|to|dev)$"),
        SourceType.COMMUNITY,
    ),
    (re.compile(r"(^|\.)(news\.ycombinator|discourse|discuss)\..+$"), SourceType.COMMUNITY),
    (re.compile(r"(^|\.)(github|gitlab)\.(com|io)$"), SourceType.COMMUNITY),
    # Official documentation.
    (re.compile(r"^docs?\..+$|(^|\.)docs\..+$"), SourceType.OFFICIAL_DOCS),
    (re.compile(r"(^|\.)readthedocs\.io$"), SourceType.OFFICIAL_DOCS),
    (
        re.compile(r"(^|\.)(apache|python|postgresql|kernel|mozilla|gnu)\.org$"),
        SourceType.OFFICIAL_DOCS,
    ),
    # Vendor engineering blogs.
    (
        re.compile(r"^(engineering|eng|tech|blog|developer|developers)\..+$"),
        SourceType.ENGINEERING_BLOG,
    ),
    # Established technical publications.
    (
        re.compile(r"(^|\.)(infoq|thenewstack|acm|usenix|lwn|arstechnica)\.(com|org|net)$"),
        SourceType.TECHNICAL_PUBLICATION,
    ),
]

# Path fragments that mark documentation regardless of the domain.
_DOC_PATHS = ("/docs/", "/documentation/", "/reference/", "/api/", "/guide/", "/manual/", "/spec/")

_BASE_SCORES: dict[SourceType, float] = {
    SourceType.OFFICIAL_DOCS: 0.95,
    SourceType.STANDARD: 0.95,
    SourceType.ACADEMIC_PAPER: 0.90,
    SourceType.ENGINEERING_BLOG: 0.75,
    SourceType.TECHNICAL_PUBLICATION: 0.70,
    SourceType.COMMUNITY: 0.45,
    SourceType.UNKNOWN: 0.50,
}


def classify_domain(url: str) -> SourceType:
    """Classify a URL by its domain, then by its path.

    Domain first because it is the stronger signal: a ``/docs/`` path on a
    marketing site is still marketing, while the Apache documentation is
    authoritative wherever it lives on the site.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return SourceType.UNKNOWN

    for pattern, source_type in _TYPE_RULES:
        if pattern.search(host):
            return source_type

    path = parts.path.lower()
    if any(fragment in path for fragment in _DOC_PATHS):
        return SourceType.OFFICIAL_DOCS

    return SourceType.UNKNOWN


def score_source(
    url: str,
    *,
    source_type: SourceType | None = None,
    published_at: datetime | None = None,
    freshness_matters: bool = False,
    now: datetime | None = None,
) -> float:
    """Score a source between 0 and 1.

    Starts from the document type and applies two adjustments.

    HTTPS is a small positive signal -- not about transport security, but
    because a site serving documentation over plain HTTP in the current decade
    is usually unmaintained.

    Age is penalised **only when the question is time-sensitive**. A ten-year-old
    explanation of how TCP works is fine; a ten-year-old page about a library's
    recommended API is actively misleading. The query analyzer decides which
    case applies, so that judgement is made once and reused rather than guessed
    at here.
    """
    resolved = source_type or classify_domain(url)
    score = _BASE_SCORES[resolved]

    if urlsplit(url).scheme == "https":
        score += 0.02

    if freshness_matters and published_at is not None:
        reference = now or datetime.now(UTC)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        years = (reference - published_at).days / 365.25
        if years > 1:
            # Up to 0.30 off, reached at roughly six years old. Linear because a
            # sharper curve would imply a precision this heuristic does not have.
            score -= min(0.30, (years - 1) * 0.06)

    return round(max(0.0, min(1.0, score)), 3)


class Source(BaseModel):
    """A document DeepTrace retrieved.

    ``retrieved_at`` is recorded because a source is a snapshot: pages change,
    and a citation is a claim about what a page said at a particular moment.
    """

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=3, description="Stable id referenced by evidence and citations.")
    url: str
    title: str = ""
    domain: str = ""
    source_type: SourceType = SourceType.UNKNOWN
    quality_score: float = Field(default=0.5, ge=0.0, le=1.0)
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    task_id: str | None = Field(default=None, description="The task that discovered it.")
    search_query: str | None = Field(default=None, description="The query that surfaced it.")
    content: str = Field(default="", description="Extracted text, if retrieved.")
    word_count: int = 0
    fetch_failed: bool = False
    fetch_error: str | None = None

    @field_validator("domain")
    @classmethod
    def _normalise_domain(cls, value: str) -> str:
        return value.lower().removeprefix("www.")

    @property
    def is_primary(self) -> bool:
        """Whether this is a primary source.

        The fact checker weighs a claim differently when its only support comes
        from commentary about a system rather than from the system's own
        documentation or specification.
        """
        return self.source_type in (
            SourceType.OFFICIAL_DOCS,
            SourceType.STANDARD,
            SourceType.ACADEMIC_PAPER,
        )

    @property
    def has_content(self) -> bool:
        """Whether there is enough text to extract evidence from.

        Below this, a page is usually an error page, a paywall, or a consent
        screen. Treating those as sources produces citations that support
        nothing.
        """
        return self.word_count >= 50 and not self.fetch_failed

    def summary(self) -> str:
        return f"{self.domain} [{self.source_type.value} {self.quality_score:.2f}]"
