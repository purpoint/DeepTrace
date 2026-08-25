"""What the API sends and receives.

Separate from the domain models on purpose, and the purpose is not tidiness.

*The wire is a contract.* A domain model is free to gain a field, rename one, or
split in two the moment the research engine needs it to. If those models were
the response bodies, every such change would break clients, and the engine would
end up frozen by consumers it never knew about.

*Not everything belongs on the wire.* A source carries the full text of a
retrieved page -- tens of kilobytes -- and a list of twenty sources would be a
megabyte of HTML nobody asked for. What a client needs from a source is where it
came from and how good it is.

*Untrusted content has to be labelled.* Titles, quotations and report prose all
originate on pages this system does not control, and they reach a browser's DOM
at the other end. The API cannot sanitize on the client's behalf -- what is safe
depends on where it is rendered -- but it can be explicit about which fields
came from outside, and these docstrings are that.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from core.config import ResearchDepth


class SubmitRequest(BaseModel):
    """A question to research."""

    model_config = {"extra": "forbid"}

    question: str = Field(
        min_length=10,
        max_length=2000,
        description="The research question.",
    )
    depth: ResearchDepth = ResearchDepth.STANDARD
    max_tasks: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="Research only the first N tasks. For a cheap smoke test.",
    )


class SubmitResponse(BaseModel):
    """Where to look for the answer.

    Returned before any research happens. Both ids are given because they
    identify different things: the job is the attempt, and can be retried or
    cancelled; the research is the result, and outlives every attempt.
    """

    job_id: str
    research_id: str
    status: str
    poll: str = Field(description="Path to poll for progress.")


class ResearchSummary(BaseModel):
    """One run, as it appears in a list."""

    research_id: str
    question: str
    """Written by the user."""
    depth: ResearchDepth
    status: str
    created_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class ResearchDetail(ResearchSummary):
    """One run, in full, without its bodies.

    Counts rather than contents. A client that wants the evidence asks for the
    evidence; a client rendering a progress screen wants to know there are
    twenty-four pieces of it, and sending them all to answer that would make the
    cheap question expensive.
    """

    normalized_question: str | None = None
    """Written by a model, from the user's question."""

    research_type: str | None = Field(
        default=None,
        description=(
            "What the analyzer decided this question was: comparison, "
            "explanation, investigation, recommendation, or review. Absent "
            "until the run has been analysed. It drives planning, so it is "
            "worth showing -- a comparison is researched differently from an "
            "explanation."
        ),
    )

    sources: int = 0
    evidence: int = 0
    claims: int = 0
    has_report: bool = False
    job: JobView | None = None


class JobView(BaseModel):
    """The attempt behind a run, while it is still an attempt.

    Present until the run is archived, which is what lets a client show
    "queued", "running", or "retrying" before there is any research to show.
    """

    job_id: str
    status: str
    attempts: int
    worker: str | None = None
    error: str | None = None


class SourceView(BaseModel):
    """A retrieved page, without the page.

    ``title`` and ``domain`` come from the retrieved page and are untrusted.
    """

    id: str
    url: str
    title: str
    domain: str
    source_type: str
    quality_score: float
    word_count: int
    fetch_failed: bool
    retrieved_at: datetime


class EvidenceView(BaseModel):
    """A passage, with the result of checking it against its page.

    ``claim`` and ``supporting_text`` are model output over untrusted page
    content. ``quote_status`` is what the deterministic check concluded, and is
    the field a client should render distinctly: a paraphrase is weaker support
    than a quotation, and showing them identically overstates the weaker one.
    """

    id: str
    source_id: str
    task_id: str | None
    claim: str
    supporting_text: str
    location: str
    quote_status: str
    quote_similarity: float
    weight: float


class ClaimView(BaseModel):
    """An assertion, with its verdict and what it rests on."""

    id: str
    text: str
    kind: str
    status: str
    confidence: str
    strength: float
    condition: str | None = None
    disposition: str | None = None
    reasoning: str | None = None
    overgeneralization: str | None = None
    suggested_revision: str | None = None
    follow_up_question: str | None = None
    conflicts_with: list[str] = Field(default_factory=list)
    contradicted_by: list[str] = Field(default_factory=list)


class ReportView(BaseModel):
    """The report, both ways.

    ``markdown`` is what a reader wants; ``structured`` is what a renderer
    wants, so a client can resolve a citation to its passage and source without
    parsing prose. Both are the same document.

    The prose is model output over untrusted page content and reaches the DOM
    at the other end. It must be sanitized where it is rendered.
    """

    research_id: str
    title: str
    markdown: str
    structured: dict[str, Any]
    citations: int
    fully_cited: bool
    """Whether every citation in the prose resolved. False means citations were
    removed during assembly, which is worth surfacing rather than hiding."""


class TraceEntry(BaseModel):
    """One model call or tool call, as it happened.

    What "shows its work" means at the API: a client can replay the run's
    sequence without access to the logs.
    """

    kind: str
    name: str
    started_at: datetime
    latency_ms: float
    status: str
    detail: dict[str, Any] = Field(default_factory=dict)


class TraceView(BaseModel):
    research_id: str
    entries: list[TraceEntry]
    total_tokens: int
    cost_usd: float | None = None
    """Null when any call had unknown pricing.

    Not zero. A run whose cost was never measured and a run that cost nothing
    are different facts, and reporting the first as the second makes an
    unmeasured run look free."""


class CancelResponse(BaseModel):
    research_id: str
    cancelled: bool
    message: str


class HealthResponse(BaseModel):
    """Whether the service can do its job, dependency by dependency.

    Each is reported separately because they fail independently and mean
    different things: without the database the API cannot answer, and without
    Redis it cannot accept new work but can still serve finished research.
    """

    status: str
    database: bool
    queue: bool
    version: str


ResearchDetail.model_rebuild()


# ---------------------------------------------------------------------------
# Accounts and sessions
#
# The request models here are the only ones in this file that carry a secret.
# They are also the only ones with no matching response model that echoes them
# back, which is deliberate: a password has exactly one legitimate direction of
# travel, and a response model that could hold one is a response model that
# eventually does.
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """A new account."""

    model_config = {"extra": "forbid"}

    email: EmailStr = Field(description="Validated for shape, not for existence.")
    password: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "Checked against the configured policy after validation. The bounds "
            "here are only wide enough to keep an unbounded body from reaching "
            "the hasher -- the real minimum is applied where the policy lives."
        ),
    )
    display_name: str | None = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    """Credentials for an existing account."""

    model_config = {"extra": "forbid"}

    email: EmailStr
    password: str = Field(min_length=1, max_length=4096)


class RefreshRequest(BaseModel):
    """A refresh token, exchanged for a new pair."""

    model_config = {"extra": "forbid"}

    refresh_token: str = Field(min_length=1, max_length=4096)


class TokenPair(BaseModel):
    """What a successful sign-in returns.

    ``expires_in`` is seconds rather than an absolute time. An absolute expiry
    requires the client's clock to agree with the server's, and a browser whose
    clock is ten minutes fast would refresh constantly or not at all.
    """

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"  # noqa: S105 -- the scheme name, per RFC 6750
    expires_in: int = Field(description="Seconds until the access token expires.")


class UserView(BaseModel):
    """An account, as its owner sees it.

    There is no field here for a password hash, and that is the point of having
    a response model at all: the ORM row has one, and this cannot carry it even
    if someone passes the row in by mistake.
    """

    id: str
    email: str
    display_name: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None


class TicketResponse(BaseModel):
    """A single-use credential for opening a progress stream."""

    ticket: str
    expires_in: int


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


class AgentSpend(BaseModel):
    """What one agent spent on one run."""

    agent: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float | None = Field(
        default=None,
        description=(
            "Null when any call in this group had no recorded price. A number "
            "here is a measured cost, not an estimate."
        ),
    )
    unpriced: int = Field(description="Calls in this group with no recorded price.")
    failed: int


class ToolSpend(BaseModel):
    """What one tool cost in time.

    Tools have no token cost, and on a rate-limited provider the wall clock is
    dominated by waiting rather than spending -- so a view showing only model
    spend explains the invoice and not the elapsed time.
    """

    tool: str
    calls: int
    latency_ms: float
    failed: int


class CostView(BaseModel):
    """A run's bill, broken down, with its own trustworthiness attached.

    ``complete`` is the field that matters. A total computed over calls whose
    prices are unknown looks authoritative and understates, so the response says
    outright whether every call was priced -- and a client that shows a figure
    without reading it is showing a number the API did not stand behind.
    """

    research_id: str
    total_cost_usd: float | None = None
    complete: bool = Field(description="Whether every model call in this run had a recorded price.")
    unpriced_calls: int
    total_input_tokens: int
    total_output_tokens: int
    model_latency_ms: float
    tool_latency_ms: float
    by_agent: list[AgentSpend]
    by_tool: list[ToolSpend]
    stale_prices: list[str] = Field(
        default_factory=list,
        description=(
            "Models whose recorded price is past its published end date. The "
            "figures still compute; they are just no longer safe to quote."
        ),
    )
