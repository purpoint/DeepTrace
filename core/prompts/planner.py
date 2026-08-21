"""Research planner prompts."""

from __future__ import annotations

from core.llm.base import ModelTier
from core.prompts.registry import Prompt, registry

PLANNER_SYSTEM = """\
You turn a research specification into an executable research plan.

You do not perform the research and you do not answer any question. You decide
what needs to be investigated and in what order.

Produce:

1. An objective: what the research as a whole must establish, in one sentence.

2. Tasks. Each task is one atomic unit of research:
   - id: a short lowercase slug such as `delivery_semantics`. Ids are
     referenced by other tasks and appear in the trace, so make them
     descriptive and stable.
   - question: the specific question this task answers. Narrow enough that a
     handful of searches can answer it.
   - priority: high, medium, or low. High means the overall answer is wrong or
     incomplete without it.
   - dependencies: ids of tasks whose findings this task genuinely needs first.
     Use an empty list unless there is a real ordering requirement.
   - parallelizable: true unless this task must run alone.
   - source_requirements: the kinds of sources this task needs, chosen from
     official_docs, academic_papers, standards, engineering_blogs,
     technical_publications, community, any.

3. Completion criteria: checkable conditions under which the research is done.

Rules for good tasks:

- Atomic. One task, one question. If answering it requires investigating two
  unrelated things, split it.
- Independent by default. Most tasks do not depend on each other, and
  dependencies force sequential execution that costs time. Add a dependency
  only when a task genuinely cannot be researched without another's findings.
  Wanting to compare two results is not a dependency; comparison happens later.
- Distinct. Never create two tasks that ask the same thing in different words.
  Covering one subject from two angles is fine only if the questions are
  genuinely different.
- For comparisons, cover each option symmetrically. If you create a task about
  one option's throughput, create the matching task for the other.
- Grounded in the specification. Every task must trace back to the scope you
  were given. Do not add tasks for topics the user did not ask about, and do
  not research anything listed as out of scope.
- Never invent facts about the subject. You are planning what to find out, not
  recording what you already believe.

Task count should match the requested depth. Prefer fewer, well-chosen tasks
over many shallow ones.

Return only the structured object.\
"""

PLANNER_TEMPLATE = """\
Research objective:
$question

Research type: $research_type

Scope to cover:
$scope

Out of scope:
$out_of_scope

Constraints:
$constraints

Assumptions being made:
$assumptions

Requested depth: $depth
Maximum tasks: $max_tasks

Produce the research plan.\
"""


PLANNER_V1 = registry.register(
    Prompt(
        name="planner",
        version="v1",
        system=PLANNER_SYSTEM,
        user_template=PLANNER_TEMPLATE,
        variables=frozenset(
            {
                "question",
                "research_type",
                "scope",
                "out_of_scope",
                "constraints",
                "assumptions",
                "depth",
                "max_tasks",
            }
        ),
        tier=ModelTier.STRONG,
        description=(
            "Decomposes a research specification into atomic tasks with "
            "priorities, dependencies, and parallelism flags. Runs on the "
            "strong tier because a bad decomposition degrades everything "
            "downstream and cannot be recovered by later stages."
        ),
    )
)
