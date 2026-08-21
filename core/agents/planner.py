"""Research planner: turns a specification into executable tasks.

Runs on the strong model tier. That is a deliberate cost decision: planning
happens once per research run, and a bad decomposition cannot be repaired by
later stages. Every subsequent search, every piece of evidence, and every claim
inherits the plan's blind spots, so this is the wrong place to economise.

The agent enforces one thing the model cannot be trusted with: the task ceiling
from the depth budget. A model asked for "at most six tasks" will sometimes
return seven, and an over-budget plan quietly spends more than the user
authorised.
"""

from __future__ import annotations

from core.config import DEPTH_BUDGETS, ResearchDepth
from core.llm.client import LLMClient
from core.logging import get_logger
from core.models.plan import ResearchPlan, TaskPriority
from core.models.query import QuerySpec
from core.prompts.planner import PLANNER_V1
from core.prompts.registry import Prompt

log = get_logger(__name__)

AGENT_NAME = "planner"

_PRIORITY_ORDER = {
    TaskPriority.HIGH: 0,
    TaskPriority.MEDIUM: 1,
    TaskPriority.LOW: 2,
}


def _bullets(items: list[str], *, empty: str = "(none stated)") -> str:
    """Render a list for prompt interpolation.

    An explicit "(none stated)" rather than a blank line, because an empty
    section reads as a missing section, and models fill perceived gaps.
    """
    return "\n".join(f"- {item}" for item in items) if items else empty


class PlanTooLargeError(ValueError):
    """A plan exceeded the depth budget and could not be trimmed safely."""


class ResearchPlanner:
    """Decomposes a research specification into an executable plan."""

    def __init__(self, client: LLMClient, *, prompt: Prompt = PLANNER_V1) -> None:
        self.client = client
        self.prompt = prompt

    async def plan(
        self,
        spec: QuerySpec,
        *,
        depth: ResearchDepth = ResearchDepth.STANDARD,
        research_id: str | None = None,
    ) -> ResearchPlan:
        """Produce a validated plan for a research specification.

        The returned plan is guaranteed to have unique task ids, resolvable
        dependencies, an acyclic dependency graph, no duplicate questions, and
        no more tasks than the depth budget allows.
        """
        budget = DEPTH_BUDGETS[depth]

        assumptions = [f"{item.aspect}: {item.assumption}" for item in spec.ambiguities]

        plan = await self.client.complete_structured(
            self.prompt,
            ResearchPlan,
            {
                "question": spec.normalized_question,
                "research_type": spec.research_type.value,
                "scope": _bullets(spec.scope),
                "out_of_scope": _bullets(spec.out_of_scope),
                "constraints": _bullets(spec.constraints),
                "assumptions": _bullets(assumptions, empty="(none)"),
                "depth": depth.value,
                "max_tasks": budget.max_tasks,
            },
            agent=AGENT_NAME,
            research_id=research_id,
        )

        plan = self._enforce_task_budget(plan, budget.max_tasks)

        waves = plan.execution_waves()
        log.info(
            "research.planned",
            research_id=research_id,
            tasks=len(plan.tasks),
            waves=len(waves),
            max_parallelism=plan.max_parallelism,
            independent_tasks=len(plan.independent_tasks),
            depth=depth.value,
            prompt_version=self.prompt.version,
        )
        return plan

    def _enforce_task_budget(self, plan: ResearchPlan, max_tasks: int) -> ResearchPlan:
        """Trim an over-budget plan, shedding the least important tasks.

        The budget is a spending limit the user authorised, so it is enforced
        here rather than trusted to the prompt. Trimming keeps high-priority
        tasks and drops low-priority ones, so a truncated plan still covers what
        matters most.

        A task is only dropped if nothing depends on it. Removing a task that
        others depend on would leave dangling references, and silently rewriting
        their dependencies would change what those tasks actually research.
        """
        if len(plan.tasks) <= max_tasks:
            return plan

        depended_on = {dep for task in plan.tasks for dep in task.dependencies}
        ranked = sorted(
            plan.tasks,
            key=lambda task: (
                _PRIORITY_ORDER[task.priority],
                task.id not in depended_on,
            ),
        )

        keep: list[str] = []
        for task in ranked:
            if len(keep) < max_tasks or task.id in depended_on:
                keep.append(task.id)

        kept = [task for task in plan.tasks if task.id in keep]
        dropped = [task.id for task in plan.tasks if task.id not in keep]

        if len(kept) > max_tasks:
            raise PlanTooLargeError(
                f"Plan has {len(plan.tasks)} tasks against a budget of "
                f"{max_tasks}, and {len(kept)} of them are depended upon by "
                f"others, so it cannot be trimmed without breaking dependencies."
            )

        log.warning(
            "research.plan_trimmed",
            requested=len(plan.tasks),
            budget=max_tasks,
            dropped=dropped,
        )
        return ResearchPlan(
            objective=plan.objective,
            tasks=kept,
            completion_criteria=plan.completion_criteria,
        )
