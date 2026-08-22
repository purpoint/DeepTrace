"""Checkpoint serialisation.

LangGraph serialises state with msgpack and, by default, warns when it
deserialises a type it was not told about -- and a future version will refuse
outright. DeepTrace's state holds Pydantic models and enums from ``core.models``,
so every checkpoint would eventually fail to load.

The failure would be the worst kind: checkpoints written today would restore fine
today and stop restoring after a library upgrade, with the loss showing up as
runs that cannot resume rather than as an error at write time.

Declaring the modules explicitly is also a security posture, not just
housekeeping. The allowlist is why a checkpoint cannot instantiate arbitrary
classes when it is read back, which matters because a checkpoint is data that
outlives the process that wrote it.
"""

from __future__ import annotations

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

CHECKPOINTED_TYPES: tuple[tuple[str, str], ...] = (
    # core.models.query
    ("core.models.query", "QuerySpec"),
    ("core.models.query", "Ambiguity"),
    ("core.models.query", "ResearchType"),
    ("core.models.query", "TimeSensitivity"),
    # core.models.plan
    ("core.models.plan", "ResearchPlan"),
    ("core.models.plan", "ResearchTask"),
    ("core.models.plan", "TaskPriority"),
    ("core.models.plan", "SourceRequirement"),
    # core.models.source
    ("core.models.source", "Source"),
    ("core.models.source", "SourceType"),
    # core.models.research
    ("core.models.research", "TaskResult"),
    ("core.models.research", "SufficiencyVerdict"),
    # core.models.evidence
    ("core.models.evidence", "Evidence"),
    ("core.models.evidence", "QuoteVerification"),
    ("core.models.evidence", "QuoteStatus"),
    ("core.models.evidence", "SupportStrength"),
    # core.models.analysis
    ("core.models.analysis", "AnalysisReport"),
    ("core.models.analysis", "Analysis"),
    ("core.models.analysis", "Finding"),
    ("core.models.analysis", "TradeOff"),
    ("core.models.analysis", "Contradiction"),
    ("core.models.analysis", "Recommendation"),
    ("core.models.analysis", "OpenQuestion"),
    ("core.models.analysis", "Confidence"),
    # core.models.claim
    ("core.models.claim", "ClaimSet"),
    ("core.models.claim", "Claim"),
    ("core.models.claim", "EvidenceLink"),
    ("core.models.claim", "ClaimStatus"),
    ("core.models.claim", "ClaimKind"),
    # core.models.verification
    ("core.models.verification", "VerificationReport"),
    ("core.models.verification", "ClaimVerification"),
    ("core.models.verification", "Disposition"),
)
"""Exactly the types permitted to appear in a checkpoint.

Listed individually rather than by module. An allowlist that admits a whole
module grows silently as that module grows, which is the opposite of what an
allowlist is for. A type added to the state and forgotten here fails loudly on
the first resume, which is the intended reminder.
"""


def build_serializer() -> JsonPlusSerializer:
    """Serializer that understands DeepTrace's domain models."""
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINTED_TYPES)
