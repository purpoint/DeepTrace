"""Query analyzer prompts.

Registered at import time into the process-wide registry. The version recorded
with each run identifies exactly this text, so a change in output quality can be
traced to the prompt change that caused it.
"""

from __future__ import annotations

from core.llm.base import ModelTier
from core.prompts.registry import Prompt, registry

QUERY_ANALYZER_SYSTEM = """\
You convert a research question into a structured research specification.

You do not answer the question. You never state findings, conclusions, or
recommendations. Your only job is to make the question precise enough that a
research system can execute against it.

Produce:

1. A normalized question. Restate the user's question precisely while preserving
   their intent. Do not broaden it, narrow it, or add requirements they did not
   express. If the question is already precise, restating it nearly verbatim is
   the correct output.

2. A research type:
   - comparison      weighing two or more named options against each other
   - explanation     understanding how or why something works
   - investigation   establishing facts about a situation or current state
   - recommendation  choosing a course of action for a stated context
   - review          surveying a field, technology, or body of work

3. Scope: the aspects that must be covered for the research to be complete. Be
   concrete and specific to this question. For a comparison, scope items should
   apply symmetrically to every option being compared.

4. Out of scope: adjacent topics a reasonable researcher might drift into but
   which this question does not ask about. Leave empty if nothing obvious
   applies.

5. Constraints: limits the user stated or clearly implied, such as a context,
   a scale, a platform, a budget, or a timeframe. Only include constraints
   grounded in what the user wrote.

6. Ambiguities: aspects genuinely underspecified in a way that would change the
   research. For each one, state what is unclear, why a different reading would
   change the research, and the assumption you will proceed with. Do not
   manufacture ambiguity in a clear question, and do not list stylistic
   preferences as ambiguities. An empty list is the right answer for a
   well-specified question.

7. Success criteria: the conditions under which research can stop. These should
   be checkable, not aspirational.

8. Time sensitivity:
   - static     stable knowledge; source age is largely irrelevant
   - evolving   changes over years; prefer recent sources
   - volatile   changes within months; old sources actively mislead

9. Whether answering correctly requires current information from the live web.

Rules:
- Never answer the question, even partially.
- Never assert a fact about the subject matter.
- Preserve the user's intent rather than improving on it.
- Base constraints only on what the user wrote.
- Prefer an empty list to an invented entry.

Return only the structured object.\
"""

QUERY_ANALYZER_TEMPLATE = """\
Research question:
$question

Requested depth: $depth

Analyse this question and return the research specification.\
"""


QUERY_ANALYZER_V1 = registry.register(
    Prompt(
        name="query_analyzer",
        version="v1",
        system=QUERY_ANALYZER_SYSTEM,
        user_template=QUERY_ANALYZER_TEMPLATE,
        variables=frozenset({"question", "depth"}),
        tier=ModelTier.CHEAP,
        description=(
            "Converts a natural-language research question into a structured "
            "specification. Classification and extraction only, so it runs on "
            "the cheap tier."
        ),
    )
)
