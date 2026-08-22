"""Finding the evidence relevant to a claim, whichever task collected it.

Verification needs more than the evidence a claim cites. A claim citing three
passages that support it is not verified by reading those three -- that only
confirms the analyst read them. What matters is whether anything *else* in the
run bears on it, and in particular whether something contradicts it.

That evidence is usually somewhere else. Research is decomposed into tasks, each
searching separately, so the passage that qualifies a claim about producers is
often the one a task about consumers happened to retrieve. Nothing in the
pipeline brings them together until here.

The interface exists for the same reason the LLM and search ones do: the
retrieval strategy is a swappable implementation detail, and nothing above this
boundary should know which one is answering.

Two implementations are intended:

*Lexical* -- shipped, deterministic, free. Word overlap against the claim, using
the same stemming and stopword rules the planner uses for duplicate detection.
It finds passages that share vocabulary with a claim, which is most of what
matters when both were written about the same subject.

*Vector* -- not yet built. It needs pgvector, which is not installed on the
development database, and an embedding per passage. The gap it closes is real:
lexical retrieval misses a passage that contradicts a claim in different words,
and different words are exactly what a contradiction often uses. Recorded as a
limitation rather than papered over -- the fact checker reports what it
compared, so a reader can see how wide the net was.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.models.evidence import Evidence
from core.models.text import retrieval_words, similarity

RELEVANCE_THRESHOLD = 0.12
"""Minimum word overlap for a passage to be worth comparing against a claim.

Low on purpose, and asymmetrically so. A passage included that turns out to be
irrelevant costs part of a prompt; a passage excluded that would have
contradicted the claim costs the thing this stage exists to catch.
"""


@runtime_checkable
class EvidenceRetriever(Protocol):
    """Finds evidence relevant to a statement."""

    @property
    def name(self) -> str: ...

    def related(self, statement: str, pool: list[Evidence], *, limit: int) -> list[Evidence]: ...


class LexicalRetriever:
    """Word-overlap retrieval. Deterministic, free, and no extension required.

    Folds words harder than duplicate detection does, because the errors cost
    different amounts: a missed duplicate costs a repeated line in a report, and
    a missed passage costs the contradiction this stage exists to surface. A
    claim about "order" has to reach a passage about "ordering".
    """

    name = "lexical"

    def __init__(self, threshold: float = RELEVANCE_THRESHOLD) -> None:
        self.threshold = threshold

    def related(self, statement: str, pool: list[Evidence], *, limit: int) -> list[Evidence]:
        """The most relevant passages, strongest match first.

        Ties break on evidence weight, so when two passages match a claim
        equally well the better-supported one is compared first -- and, when the
        limit bites, it is the one that survives.
        """
        target = retrieval_words(statement)
        scored = [
            (similarity(target, retrieval_words(f"{item.claim} {item.supporting_text}")), item)
            for item in pool
        ]
        relevant = [(score, item) for score, item in scored if score >= self.threshold]
        relevant.sort(key=lambda pair: (-pair[0], -pair[1].weight, pair[1].id))
        return [item for _, item in relevant[:limit]]
