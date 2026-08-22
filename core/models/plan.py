"""The research plan: a question decomposed into executable tasks.

The plan is the contract between thinking and doing. Everything after this point
executes tasks; nothing re-decides what the tasks should be.

Most of this module is validation, and that is deliberate. A plan is produced by
a language model, and three of its possible mistakes are silent and expensive:

*A dependency on a task that does not exist* leaves a task permanently unable to
start. The scheduler would either wait forever or skip it without explanation.

*A dependency cycle* deadlocks execution outright. Two tasks each waiting for the
other is not recoverable at runtime, and the failure surfaces far from its cause.

*Duplicate tasks* double the cost of a research run while adding no coverage,
and the duplication is easy to miss because both tasks look reasonable.

All three are caught here, at construction, where the error names the problem.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from core.models.text import content_words, similarity

_TASK_ID = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
DUPLICATE_SIMILARITY_THRESHOLD = 0.85
"""Jaccard similarity above which two task questions count as duplicates.

Token overlap rather than embeddings, because this check must be deterministic
and free -- it runs on every plan, and a validation rule that costs an API call
and can vary between runs is the wrong shape. Semantic near-duplicate detection
arrives with the evidence layer, where embeddings already exist for other reasons.
"""


class TaskPriority(StrEnum):
    """How much the overall answer depends on this task.

    Used to decide what to drop when a depth budget is exhausted: low-priority
    tasks are shed before high-priority ones, so a truncated run still covers
    what matters.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SourceRequirement(StrEnum):
    """The kind of source a task needs, in the project's preference order.

    Recorded per task so the researcher can prefer appropriate sources and the
    fact checker can judge whether evidence came from a suitable place. A claim
    about a protocol's behaviour backed by a forum post is weaker than the same
    claim backed by the specification.
    """

    OFFICIAL_DOCS = "official_docs"
    ACADEMIC_PAPERS = "academic_papers"
    STANDARDS = "standards"
    ENGINEERING_BLOGS = "engineering_blogs"
    TECHNICAL_PUBLICATIONS = "technical_publications"
    COMMUNITY = "community"
    ANY = "any"


class ResearchTask(BaseModel):
    """One atomic unit of research.

    Atomic means answerable from its own searches without needing another task's
    findings, unless that need is declared in ``dependencies``.
    """

    model_config = {"extra": "forbid"}

    id: str = Field(
        min_length=2,
        max_length=60,
        description="Stable slug, referenced by other tasks' dependencies.",
    )
    question: str = Field(
        min_length=10,
        max_length=300,
        description="The specific question this task answers.",
    )
    priority: TaskPriority = TaskPriority.MEDIUM
    dependencies: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Task ids whose findings this task needs before it can start.",
    )
    parallelizable: bool = Field(
        default=True,
        description="Whether this task may run concurrently with its peers.",
    )
    source_requirements: list[SourceRequirement] = Field(
        default_factory=lambda: [SourceRequirement.ANY],
        max_length=5,
    )

    @model_validator(mode="after")
    def _check_id_and_self_reference(self) -> ResearchTask:
        if not _TASK_ID.match(self.id):
            raise ValueError(
                f"Task id {self.id!r} must be a lowercase slug such as "
                f"'delivery_semantics'. Ids appear in dependencies, traces, and "
                f"URLs, so they must be stable and predictable."
            )
        if self.id in self.dependencies:
            raise ValueError(f"Task {self.id!r} depends on itself.")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError(f"Task {self.id!r} lists the same dependency twice.")
        return self

    def normalized_question(self) -> frozenset[str]:
        """Content words of the question, stemmed, for duplicate comparison."""
        return content_words(self.question)


class ResearchPlan(BaseModel):
    """An executable research plan.

    Guaranteed on construction: task ids are unique, every dependency resolves,
    the dependency graph is acyclic, and no two tasks ask the same question.
    """

    model_config = {"extra": "forbid"}

    objective: str = Field(
        min_length=10,
        max_length=500,
        description="What the research as a whole is trying to establish.",
    )
    tasks: list[ResearchTask] = Field(min_length=1, max_length=20)
    completion_criteria: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Checkable conditions under which the research is done.",
    )

    @model_validator(mode="after")
    def _validate_graph(self) -> ResearchPlan:
        self._require_unique_ids()
        self._require_resolvable_dependencies()
        self._require_acyclic()
        self._require_distinct_questions()
        return self

    def _require_unique_ids(self) -> None:
        seen: set[str] = set()
        for task in self.tasks:
            if task.id in seen:
                raise ValueError(
                    f"Duplicate task id {task.id!r}. Ids identify tasks in "
                    f"dependencies and in the trace, so they must be unique."
                )
            seen.add(task.id)

    def _require_resolvable_dependencies(self) -> None:
        """A dependency on a task that does not exist can never be satisfied."""
        known = {task.id for task in self.tasks}
        for task in self.tasks:
            missing = [dep for dep in task.dependencies if dep not in known]
            if missing:
                raise ValueError(
                    f"Task {task.id!r} depends on unknown task(s): "
                    f"{', '.join(sorted(missing))}. Known tasks: "
                    f"{', '.join(sorted(known))}."
                )

    def _require_acyclic(self) -> None:
        """Reject cycles, which would deadlock execution.

        Uses Kahn's algorithm, which is the same traversal
        :meth:`execution_waves` needs, so the cycle check and the schedule come
        from one piece of logic rather than two that could disagree.
        """
        remaining = {task.id: set(task.dependencies) for task in self.tasks}

        while remaining:
            ready = {task_id for task_id, deps in remaining.items() if not deps}
            if not ready:
                blocked = ", ".join(sorted(remaining))
                raise ValueError(
                    f"Task dependencies form a cycle among: {blocked}. "
                    f"A cyclic plan can never start, since every task in the "
                    f"cycle waits for another."
                )
            for task_id in ready:
                del remaining[task_id]
            for deps in remaining.values():
                deps -= ready

    def _require_distinct_questions(self) -> None:
        """Reject near-duplicate tasks, which double cost without adding coverage."""
        normalized = [(task, task.normalized_question()) for task in self.tasks]
        for index, (task, tokens) in enumerate(normalized):
            for other, other_tokens in normalized[index + 1 :]:
                score = similarity(tokens, other_tokens)
                if score >= DUPLICATE_SIMILARITY_THRESHOLD:
                    raise ValueError(
                        f"Tasks {task.id!r} and {other.id!r} ask effectively the "
                        f"same question (similarity {score:.2f}). Duplicate tasks "
                        f"double research cost without adding coverage."
                    )

    # -- scheduling --------------------------------------------------------

    def execution_waves(self) -> list[list[ResearchTask]]:
        """Group tasks into waves that can run concurrently.

        Wave *n* contains every task whose dependencies are all satisfied by
        earlier waves. This is what the parallel executor consumes: run a wave,
        wait, run the next.

        Tasks marked ``parallelizable=False`` are placed in a wave of their own
        so nothing runs alongside them.
        """
        by_id = {task.id: task for task in self.tasks}
        remaining = {task.id: set(task.dependencies) for task in self.tasks}
        waves: list[list[ResearchTask]] = []

        while remaining:
            ready = sorted(task_id for task_id, deps in remaining.items() if not deps)
            # _require_acyclic guarantees `ready` is non-empty here.
            serial = [task_id for task_id in ready if not by_id[task_id].parallelizable]
            concurrent = [task_id for task_id in ready if by_id[task_id].parallelizable]

            batch = [concurrent] if concurrent else []
            batch.extend([task_id] for task_id in serial)

            for group in batch:
                waves.append([by_id[task_id] for task_id in group])

            for task_id in ready:
                del remaining[task_id]
            for deps in remaining.values():
                deps -= set(ready)

        return waves

    @property
    def independent_tasks(self) -> list[ResearchTask]:
        """Tasks with no dependencies. These start immediately."""
        return [task for task in self.tasks if not task.dependencies]

    @property
    def max_parallelism(self) -> int:
        """Largest number of tasks runnable at once, before concurrency limits."""
        return max((len(wave) for wave in self.execution_waves()), default=0)

    def task(self, task_id: str) -> ResearchTask:
        for candidate in self.tasks:
            if candidate.id == task_id:
                return candidate
        raise KeyError(f"No task {task_id!r} in plan.")

    def summary(self) -> str:
        waves = self.execution_waves()
        return (
            f"{len(self.tasks)} tasks | {len(waves)} waves | max parallelism {self.max_parallelism}"
        )
