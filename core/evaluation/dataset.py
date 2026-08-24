"""The benchmark: questions chosen to be checkable, not to be easy.

**There are no expected answers here, and that is deliberate.** The obvious way
to build a benchmark is to write the right answer next to each question and
score similarity to it. That measures agreement with whoever wrote the key, and
for questions like these there is no single right answer -- "is Kafka or
RabbitMQ better for high-scale microservices" has a defensible answer in both
directions depending on what you weight. A benchmark scored against one person's
opinion rewards a system for reproducing that opinion.

What can be checked without an opinion is whether the *work* was done properly:
whether every quotation appears on the page it cites, whether every published
claim traces to evidence, whether the scope the system said it would cover was
covered, and whether the sources are worth citing. Those are facts about a run,
and they are what :mod:`core.evaluation.metrics` measures.

**What each question carries instead** is a structural expectation: the kind of
research it calls for, how much its answer depends on recency, and the concepts
a competent answer has to engage with. The last one is not a keyword check on
the prose -- it is used to ask whether the *scope the analyzer produced* covered
the ground, which catches a run that quietly narrowed a hard question into an
easy one.

**Chosen for checkability.** Each question is answerable from public technical
documentation, because a benchmark whose sources are paywalled or absent
measures retrieval luck. Several are deliberately contested, where good sources
disagree, because a system that reports a contradiction is doing better than one
that averages it away -- and the second failure is invisible unless the
benchmark contains a question that provokes it.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.models.query import ResearchType, TimeSensitivity


@dataclass(frozen=True, slots=True)
class BenchmarkQuestion:
    """One benchmark item, and what can be checked about a run of it."""

    id: str
    question: str
    research_type: ResearchType
    """The classification a competent analyzer should reach.

    Checked because misclassifying a comparison as an explanation changes the
    plan: a comparison needs symmetric coverage of each option, and a run that
    never realised it was comparing produces a report that is thorough about one
    side.
    """

    time_sensitivity: TimeSensitivity
    concepts: tuple[str, ...] = ()
    """Ground a competent answer has to cover.

    Compared against the specification's scope rather than against the report's
    prose. A keyword check on prose rewards a system for saying a word; checking
    the scope asks whether the run set out to cover the ground at all, which is
    where the failure actually happens.
    """

    contested: bool = False
    """Whether good sources are expected to disagree.

    Marked so the benchmark can report contradiction-surfacing separately. A
    system that averages away a real disagreement scores well on every other
    metric while doing the single most misleading thing it can do.
    """

    notes: str = ""


def _q(
    id: str,
    question: str,
    research_type: ResearchType,
    time_sensitivity: TimeSensitivity,
    *concepts: str,
    contested: bool = False,
    notes: str = "",
) -> BenchmarkQuestion:
    return BenchmarkQuestion(
        id=id,
        question=question,
        research_type=research_type,
        time_sensitivity=time_sensitivity,
        concepts=concepts,
        contested=contested,
        notes=notes,
    )


BENCHMARK: tuple[BenchmarkQuestion, ...] = (
    # -- comparison --------------------------------------------------------
    _q(
        "cmp-01",
        "Compare Kafka and RabbitMQ for high-scale microservice messaging.",
        ResearchType.COMPARISON,
        TimeSensitivity.EVOLVING,
        "throughput",
        "ordering guarantees",
        "delivery semantics",
        "operational complexity",
        contested=True,
        notes="The canonical case. Vendor documentation on both sides overstates.",
    ),
    _q(
        "cmp-02",
        "Compare PostgreSQL and MySQL for handling JSON document workloads.",
        ResearchType.COMPARISON,
        TimeSensitivity.EVOLVING,
        "indexing",
        "query syntax",
        "storage format",
    ),
    _q(
        "cmp-03",
        "Compare gRPC and REST for internal service-to-service communication.",
        ResearchType.COMPARISON,
        TimeSensitivity.EVOLVING,
        "serialisation",
        "streaming",
        "tooling",
        "browser support",
        contested=True,
    ),
    _q(
        "cmp-04",
        "Compare optimistic and pessimistic locking for high-contention database rows.",
        ResearchType.COMPARISON,
        TimeSensitivity.STATIC,
        "contention",
        "retry",
        "deadlock",
    ),
    _q(
        "cmp-05",
        "Compare server-side rendering and client-side rendering for content-heavy sites.",
        ResearchType.COMPARISON,
        TimeSensitivity.EVOLVING,
        "time to first byte",
        "search indexing",
        "caching",
        contested=True,
    ),
    # -- explanation -------------------------------------------------------
    _q(
        "exp-01",
        "How does Kafka guarantee message ordering within a partition?",
        ResearchType.EXPLANATION,
        TimeSensitivity.STATIC,
        "partition",
        "producer",
        "offset",
        notes="Has a precise documented answer, and a well-known caveat about "
        "in-flight requests under retries that a shallow run misses.",
    ),
    _q(
        "exp-02",
        "How does the TCP three-way handshake establish a connection?",
        ResearchType.EXPLANATION,
        TimeSensitivity.STATIC,
        "SYN",
        "sequence number",
        "state machine",
        notes="Old, stable, abundantly documented. A run that fails here has a "
        "retrieval problem rather than a hard question.",
    ),
    _q(
        "exp-03",
        "How does PostgreSQL's MVCC implementation handle concurrent writes?",
        ResearchType.EXPLANATION,
        TimeSensitivity.STATIC,
        "tuple visibility",
        "transaction id",
        "vacuum",
    ),
    _q(
        "exp-04",
        "How does HTTP/2 multiplexing differ from HTTP/1.1 pipelining?",
        ResearchType.EXPLANATION,
        TimeSensitivity.STATIC,
        "head-of-line blocking",
        "streams",
        "framing",
    ),
    _q(
        "exp-05",
        "How does Raft achieve consensus when a leader fails?",
        ResearchType.EXPLANATION,
        TimeSensitivity.STATIC,
        "election",
        "term",
        "log replication",
    ),
    _q(
        "exp-06",
        "How does Rust's borrow checker prevent data races at compile time?",
        ResearchType.EXPLANATION,
        TimeSensitivity.STATIC,
        "ownership",
        "lifetimes",
        "aliasing",
    ),
    # -- investigation -----------------------------------------------------
    _q(
        "inv-01",
        "What are the known failure modes of using Redis as a primary datastore?",
        ResearchType.INVESTIGATION,
        TimeSensitivity.EVOLVING,
        "persistence",
        "durability",
        "failover",
        contested=True,
    ),
    _q(
        "inv-02",
        "What causes tail latency in JVM applications with large heaps?",
        ResearchType.INVESTIGATION,
        TimeSensitivity.EVOLVING,
        "garbage collection",
        "pause time",
        "allocation rate",
    ),
    _q(
        "inv-03",
        "What are the documented security risks of running containers as root?",
        ResearchType.INVESTIGATION,
        TimeSensitivity.EVOLVING,
        "privilege escalation",
        "namespace",
        "capabilities",
    ),
    _q(
        "inv-04",
        "Why do distributed systems suffer from clock skew, and what breaks because of it?",
        ResearchType.INVESTIGATION,
        TimeSensitivity.STATIC,
        "NTP",
        "ordering",
        "leases",
    ),
    # -- recommendation ----------------------------------------------------
    _q(
        "rec-01",
        "Should a small team adopt Kubernetes for a three-service application?",
        ResearchType.RECOMMENDATION,
        TimeSensitivity.EVOLVING,
        "operational overhead",
        "team size",
        "alternatives",
        contested=True,
        notes="A question where the honest answer is conditional. A run that "
        "picks a side without stating conditions is overreaching.",
    ),
    _q(
        "rec-02",
        "What database isolation level should a payment system use, and why?",
        ResearchType.RECOMMENDATION,
        TimeSensitivity.STATIC,
        "serializable",
        "write skew",
        "throughput",
    ),
    _q(
        "rec-03",
        "How should secrets be managed in a containerised deployment?",
        ResearchType.RECOMMENDATION,
        TimeSensitivity.EVOLVING,
        "rotation",
        "at rest",
        "injection",
    ),
    _q(
        "rec-04",
        "What is a sound retry strategy for calls to a rate-limited third-party API?",
        ResearchType.RECOMMENDATION,
        TimeSensitivity.STATIC,
        "exponential backoff",
        "jitter",
        "idempotency",
    ),
    # -- review ------------------------------------------------------------
    _q(
        "rev-01",
        "What approaches exist for reducing hallucination in retrieval-augmented generation?",
        ResearchType.REVIEW,
        TimeSensitivity.VOLATILE,
        "grounding",
        "citation",
        "verification",
        contested=True,
        notes="Volatile and close to this project's own subject, so a run that "
        "cites only marketing pages is visible immediately.",
    ),
    _q(
        "rev-02",
        "What techniques are used for zero-downtime database schema migration?",
        ResearchType.REVIEW,
        TimeSensitivity.EVOLVING,
        "backwards compatible",
        "dual write",
        "backfill",
    ),
    _q(
        "rev-03",
        "What are the current approaches to WebAssembly outside the browser?",
        ResearchType.REVIEW,
        TimeSensitivity.VOLATILE,
        "WASI",
        "runtime",
        "sandboxing",
    ),
    _q(
        "rev-04",
        "What methods exist for detecting prompt injection in LLM applications?",
        ResearchType.REVIEW,
        TimeSensitivity.VOLATILE,
        "instruction hierarchy",
        "input isolation",
        "detection",
        contested=True,
    ),
    _q(
        "rev-05",
        "What are the established techniques for rate limiting a distributed API?",
        ResearchType.REVIEW,
        TimeSensitivity.STATIC,
        "token bucket",
        "sliding window",
        "distributed counter",
    ),
)
"""Twenty-four questions, spanning every research type the system classifies.

Sized for the tension between coverage and cost. The roadmap asks for 20-50;
twenty-four is the low end on purpose, because a benchmark that costs a day's
quota to run is a benchmark that gets run once and quoted forever. A suite that
can be run again after a change is worth more than a larger one that cannot.

Note on categories: the roadmap says "all six categories in the source doc", and
:class:`ResearchType` defines five. The five are what the system actually
classifies into and therefore what a benchmark can check, so these are organised
by those. Recency is covered as a second axis rather than a sixth category,
because it is orthogonal -- a comparison can be volatile or static.
"""


def by_type(research_type: ResearchType) -> tuple[BenchmarkQuestion, ...]:
    return tuple(item for item in BENCHMARK if item.research_type is research_type)


def contested() -> tuple[BenchmarkQuestion, ...]:
    """Questions where good sources are expected to disagree."""
    return tuple(item for item in BENCHMARK if item.contested)


def coverage_summary() -> dict[str, int]:
    """How many questions sit in each research type. Used by the dataset test
    that keeps one category from quietly dominating the benchmark."""
    counts: dict[str, int] = {}
    for item in BENCHMARK:
        counts[item.research_type.value] = counts.get(item.research_type.value, 0) + 1
    return counts


__all__ = ["BENCHMARK", "BenchmarkQuestion", "by_type", "contested", "coverage_summary"]
