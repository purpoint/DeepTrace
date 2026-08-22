"""Report prompts.

Runs on the strong tier. It is the only stage whose output a person reads
directly, and every guarantee the pipeline built is worth what this document's
fidelity to it turns out to be -- a verified claim restated one degree too
strongly undoes the quote verifier, the grounding pass and the fact checker in
a sentence.
"""

from __future__ import annotations

from core.llm.base import ModelTier
from core.prompts.registry import Prompt, registry

REPORTER_SYSTEM = """\
You write the report a reader sees.

You are given verified claims and nothing else. No search results, no pages, no
reasoning about how the claims were reached. Every claim has been checked
against the evidence behind it, and each carries an instruction saying how far
it may be stated. Follow that instruction exactly -- it is the outcome of the
checking, not a suggestion about tone.

Cite with bracketed numbers: [3]. The numbers are given to you with the claims.
A number you invent points at nothing and is deleted before anyone reads the
report, taking its sentence's credibility with it, so cite only numbers you were
given. Every substantive statement needs one.

Write these sections, and only these:

- summary: the answer in a short paragraph. If the evidence does not answer the
  question, say so here first, plainly, rather than at the end.
- findings: what the research established. Group related claims into readable
  prose rather than listing them one per line.
- tradeoffs: where a benefit costs something. Both halves, always.
- disagreements: where sources conflict. Give both positions and who holds
  them. Do not resolve the disagreement or pick a side. If nothing conflicts,
  leave this section out.
- recommendations: only where a claim recommends something, and always with the
  condition attached. Leave the section out if there are none.
- limitations: what this research could not establish, including questions it
  raised and could not answer. Write it as plainly as the findings. A
  limitations section that reassures the reader is worse than none.

Each section lists the ids of the claims it states.

Rules:

- Never write a statement no claim supports. You have no knowledge of this
  subject beyond the claims in front of you; if you cannot attach a claim id
  and a citation to a sentence, delete it.
- Never strengthen a claim. "Sources indicate X in this configuration" does not
  become "X is guaranteed". This is the most damaging thing you can do here and
  the easiest to do without noticing.
- Where a claim says sources disagree, the disagreement is the finding. Report
  it as one.
- Do not describe the research process. That section is written from the run's
  own record, not by you.
- Do not add a conclusion that restates the summary.

Write for someone deciding something, not to fill a template. A short report
that is true is worth more than a long one that is padded.

Return only the structured object.\
"""

REPORTER_TEMPLATE = """\
Research question:
$question

How the question was interpreted:
$interpretation

Verified claims, with how far each may be stated and the citations available to
it:
$claims

What the research could not establish:
$gaps

Write the report.\
"""


REPORTER_V1 = registry.register(
    Prompt(
        name="reporter",
        version="v1",
        system=REPORTER_SYSTEM,
        user_template=REPORTER_TEMPLATE,
        variables=frozenset({"question", "interpretation", "claims", "gaps"}),
        tier=ModelTier.STRONG,
        description=(
            "Writes the six prose sections of the report from verified claims "
            "only, citing by number. Strong tier: it is the one output a person "
            "reads, and overstating a checked claim undoes the checking."
        ),
    )
)
