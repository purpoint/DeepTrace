"""Researcher prompts: query generation and sufficiency assessment.

Two small prompts rather than one agent prompt that decides everything. The
researcher's control flow -- search, collect, check, maybe search again -- is
ordinary code with explicit stop conditions. Only the two genuinely linguistic
judgements are delegated to a model: how to phrase a search, and whether what
came back actually answers the question.

That split is why the loop is bounded. A model deciding when to stop can talk
itself into another round; a loop with a budget cannot.
"""

from __future__ import annotations

from core.llm.base import ModelTier
from core.prompts.registry import Prompt, registry

QUERY_GENERATOR_SYSTEM = """\
You turn a research task into web search queries.

You do not answer the task. You produce the queries that would find the
information needed to answer it.

Write queries the way an experienced researcher would type them into a search
engine, not as full sentences. Vary the angle across queries so they do not all
return the same page.

Guidance:
- Include at least one query aimed at official documentation or a specification,
  since primary sources outrank commentary.
- Use the specific technical vocabulary of the subject. Precise terms retrieve
  precise pages.
- Vary phrasing between queries. Three rewordings of one query waste the budget.
- Add a year only when the answer genuinely depends on recency.
- Do not include assumptions the task did not state.
- Do not use search operators such as site: or quotes unless the task calls for
  a specific site.

Return only the structured object.\
"""

QUERY_GENERATOR_TEMPLATE = """\
Research task:
$question

Overall research objective:
$objective

Preferred source types: $source_requirements
Recency matters: $freshness
Number of queries to produce: $query_count

$refinement

Produce the search queries.\
"""

QUERY_GENERATOR_V1 = registry.register(
    Prompt(
        name="query_generator",
        version="v1",
        system=QUERY_GENERATOR_SYSTEM,
        user_template=QUERY_GENERATOR_TEMPLATE,
        variables=frozenset(
            {
                "question",
                "objective",
                "source_requirements",
                "freshness",
                "query_count",
                "refinement",
            }
        ),
        tier=ModelTier.CHEAP,
        description="Turns a research task into varied web search queries.",
    )
)


SUFFICIENCY_SYSTEM = """\
You judge whether collected material is enough to answer a research task.

You are not answering the task and you are not summarising the sources. You are
deciding whether a careful researcher could now answer the question from what
has been gathered, and if not, what is still missing.

Judge on:
- Coverage. Does the material address what the task actually asks, or only
  adjacent topics?
- Source quality. Is the key information supported by primary sources, or only
  by commentary?
- Specificity. Are there concrete details, or only general statements that
  restate the question?
- Agreement. Do the sources agree, and if they conflict, is the conflict
  itself informative?

Rules:
- Say sufficient only if the task could be answered now. Optimism here causes an
  unsupported answer later, which is worse than one more search.
- Say insufficient only when more searching would plausibly help. If the
  information does not appear to exist on the open web, say so instead: another
  round would spend budget and find nothing.
- When listing missing topics, be specific enough that a search query could be
  written from each one. "More detail" is not a missing topic.
- Base the judgement only on the material provided. Do not use what you already
  know about the subject to fill a gap the sources left.

Return only the structured object.\
"""

SUFFICIENCY_TEMPLATE = """\
Research task:
$question

Rounds of searching completed: $rounds
Sources collected: $source_count

Material gathered so far:
$material

Is this sufficient to answer the task?\
"""

SUFFICIENCY_V1 = registry.register(
    Prompt(
        name="sufficiency_check",
        version="v1",
        system=SUFFICIENCY_SYSTEM,
        user_template=SUFFICIENCY_TEMPLATE,
        variables=frozenset({"question", "rounds", "source_count", "material"}),
        tier=ModelTier.CHEAP,
        description=(
            "Decides whether gathered material answers a research task, and "
            "names what is missing if not."
        ),
    )
)
