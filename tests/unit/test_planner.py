"""Tests for the research plan model and planner agent.

Most of these defend against a specific failure the language model can produce.
A plan is the contract the rest of the system executes against, and a malformed
one fails far from its cause -- a cyclic plan deadlocks the scheduler, a dangling
dependency leaves a task unable to start, and duplicate tasks double the bill.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from core.agents.planner import PlanTooLargeError, ResearchPlanner
from core.config import ResearchDepth
from core.llm.client import LLMClient, ModelRouter
from core.llm.retry import RetryPolicy
from core.models.plan import (
    DUPLICATE_SIMILARITY_THRESHOLD,
    ResearchPlan,
    ResearchTask,
    SourceRequirement,
    TaskPriority,
)
from core.models.query import Ambiguity, QuerySpec
from core.models.text import similarity, stem
from core.observability.recorder import InMemoryRunRecorder
from core.prompts.planner import PLANNER_V1
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit


ROUTER = ModelRouter(
    provider_id="fake",
    cheap_model="gemini-3.5-flash-lite",
    strong_model="gemini-3.7-flash",
    embed_model="gemini-embedding-001",
)


def task(
    task_id: str,
    question: str,
    *,
    dependencies: list[str] | None = None,
    priority: str = "medium",
    parallelizable: bool = True,
) -> dict[str, object]:
    return {
        "id": task_id,
        "question": question,
        "priority": priority,
        "dependencies": dependencies or [],
        "parallelizable": parallelizable,
        "source_requirements": ["any"],
    }


def plan_json(tasks: list[dict[str, object]] | None = None, **overrides: object) -> str:
    payload: dict[str, object] = {
        "objective": "Compare Kafka and RabbitMQ for event-driven microservices",
        "tasks": tasks
        if tasks is not None
        else [
            task("kafka_arch", "How is Kafka's storage architecture designed?"),
            task("rabbit_arch", "How does RabbitMQ route and queue messages?"),
        ],
        "completion_criteria": ["Both systems are covered on every scope item"],
    }
    payload.update(overrides)
    return json.dumps(payload)


def build(tasks: list[dict[str, object]]) -> ResearchPlan:
    return ResearchPlan.model_validate_json(plan_json(tasks))


SPEC = QuerySpec(
    normalized_question="How do Kafka and RabbitMQ differ for event-driven microservices?",
    research_type="comparison",
    scope=["architecture", "delivery semantics"],
    out_of_scope=["managed service pricing"],
    constraints=["high scale"],
    ambiguities=[],
    success_criteria=["Both systems covered"],
    time_sensitivity="evolving",
    requires_current_information=True,
)


def make_planner(*responses: object) -> tuple[ResearchPlanner, InMemoryRunRecorder]:
    recorder = InMemoryRunRecorder()
    client = LLMClient(
        FakeProvider(responses),
        router=ROUTER,
        recorder=recorder,
        retry_policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0.001, jitter=0.0),
    )
    return ResearchPlanner(client), recorder


# ---------------------------------------------------------------------------
# Graph validation
# ---------------------------------------------------------------------------


class TestDependencyIntegrity:
    def test_dangling_dependency_is_rejected(self) -> None:
        """A dependency on a task that does not exist can never be satisfied,
        so the task would wait forever or be skipped without explanation."""
        with pytest.raises(ValidationError, match="depends on unknown task"):
            build(
                [
                    task("kafka_arch", "How is Kafka's storage architecture designed?"),
                    task(
                        "ops",
                        "What operational overhead does each system impose?",
                        dependencies=["does_not_exist"],
                    ),
                ]
            )

    def test_error_lists_the_known_tasks(self) -> None:
        """The fix is usually a typo in an id, so the message shows the options."""
        with pytest.raises(ValidationError, match="Known tasks"):
            build(
                [
                    task("kafka_arch", "How is Kafka's storage architecture designed?"),
                    task("ops", "What operational cost does each impose?", dependencies=["kafka"]),
                ]
            )

    def test_self_dependency_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="depends on itself"):
            ResearchTask(
                id="kafka_arch",
                question="How is Kafka's storage architecture designed?",
                dependencies=["kafka_arch"],
            )

    def test_repeated_dependency_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="same dependency twice"):
            ResearchTask(
                id="ops",
                question="What operational overhead does each system impose?",
                dependencies=["kafka_arch", "kafka_arch"],
            )

    def test_duplicate_ids_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate task id"):
            build(
                [
                    task("kafka_arch", "How is Kafka's storage architecture designed?"),
                    task("kafka_arch", "How does RabbitMQ route and queue messages?"),
                ]
            )

    @pytest.mark.parametrize("bad_id", ["Not A Slug", "UPPER", "has space", "trailing_", "-lead"])
    def test_ids_must_be_slugs(self, bad_id: str) -> None:
        """Ids appear in dependencies, traces, and URLs, so they must be stable."""
        with pytest.raises(ValidationError):
            ResearchTask(id=bad_id, question="How is this system actually designed?")


class TestCycleDetection:
    """A cyclic plan deadlocks execution and cannot recover at runtime."""

    def test_two_node_cycle_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cycle"):
            build(
                [
                    task(
                        "aa", "How is Kafka's storage architecture designed?", dependencies=["bb"]
                    ),
                    task("bb", "How does RabbitMQ route and queue messages?", dependencies=["aa"]),
                ]
            )

    def test_three_node_cycle_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cycle"):
            build(
                [
                    task(
                        "aa", "How is Kafka's storage architecture designed?", dependencies=["cc"]
                    ),
                    task("bb", "How does RabbitMQ route and queue messages?", dependencies=["aa"]),
                    task("cc", "What operational overhead does each impose?", dependencies=["bb"]),
                ]
            )

    def test_cycle_error_names_the_tasks_involved(self) -> None:
        with pytest.raises(ValidationError, match="aa, bb"):
            build(
                [
                    task(
                        "aa", "How is Kafka's storage architecture designed?", dependencies=["bb"]
                    ),
                    task("bb", "How does RabbitMQ route and queue messages?", dependencies=["aa"]),
                ]
            )

    def test_a_diamond_is_not_a_cycle(self) -> None:
        """Two tasks depending on a common ancestor and a common descendant is
        a legitimate shape and must not be mistaken for a cycle."""
        plan = build(
            [
                task("root", "How is Kafka's storage architecture designed?"),
                task("left", "What delivery guarantees does Kafka provide?", dependencies=["root"]),
                task("right", "How does RabbitMQ route and queue messages?", dependencies=["root"]),
                task(
                    "merge",
                    "Which trade-offs matter most at high scale?",
                    dependencies=["left", "right"],
                ),
            ]
        )
        assert len(plan.execution_waves()) == 3


class TestDuplicateDetection:
    def test_reworded_duplicate_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="effectively the same question"):
            build(
                [
                    task("aa", "How does Kafka guarantee message ordering?"),
                    task("bb", "How does Kafka guarantee ordering of messages?"),
                ]
            )

    def test_symmetric_comparison_tasks_are_allowed(self) -> None:
        """The critical distinction. A comparison needs the same aspect covered
        for each option, and those tasks differ by exactly one token -- the same
        shape as a plural-reworded duplicate. Stemming is what separates them."""
        plan = build(
            [
                task("kafka_ordering", "How does Kafka guarantee message ordering?"),
                task("rabbit_ordering", "How does RabbitMQ guarantee message ordering?"),
            ]
        )
        assert len(plan.tasks) == 2

    def test_stemming_is_what_makes_the_distinction_possible(self) -> None:
        """Without stemming both cases score identically, so no threshold could
        separate them. Regression test for that reasoning."""
        duplicate = similarity(
            ResearchTask(
                id="aa", question="How does Kafka guarantee message ordering?"
            ).normalized_question(),
            ResearchTask(
                id="bb", question="How does Kafka guarantee ordering of messages?"
            ).normalized_question(),
        )
        symmetric = similarity(
            ResearchTask(
                id="aa", question="How does Kafka guarantee message ordering?"
            ).normalized_question(),
            ResearchTask(
                id="cc", question="How does RabbitMQ guarantee message ordering?"
            ).normalized_question(),
        )

        assert duplicate >= DUPLICATE_SIMILARITY_THRESHOLD
        assert symmetric < DUPLICATE_SIMILARITY_THRESHOLD

    @pytest.mark.parametrize(
        ("word", "expected"),
        [
            ("messages", "message"),
            ("guarantees", "guarantee"),
            ("class", "class"),
            ("its", "its"),
            ("kafka", "kafka"),
        ],
    )
    def test_stemmer_is_conservative(self, word: str, expected: str) -> None:
        assert stem(word) == expected

    def test_unrelated_tasks_are_not_duplicates(self) -> None:
        plan = build(
            [
                task("kafka_arch", "How is Kafka's storage architecture designed?"),
                task("rabbit_ops", "What operational overhead does RabbitMQ impose?"),
            ]
        )
        assert len(plan.tasks) == 2


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class TestExecutionWaves:
    def test_independent_tasks_share_one_wave(self) -> None:
        """This is what parallel execution consumes: everything in a wave can
        run at once."""
        plan = build(
            [
                task("kafka_arch", "How is Kafka's storage architecture designed?"),
                task("rabbit_arch", "How does RabbitMQ route and queue messages?"),
                task("kafka_delivery", "What delivery guarantees does Kafka provide?"),
            ]
        )
        waves = plan.execution_waves()

        assert len(waves) == 1
        assert len(waves[0]) == 3
        assert plan.max_parallelism == 3

    def test_dependent_tasks_land_in_later_waves(self) -> None:
        plan = build(
            [
                task("kafka_arch", "How is Kafka's storage architecture designed?"),
                task("rabbit_arch", "How does RabbitMQ route and queue messages?"),
                task(
                    "ops",
                    "What operational overhead does each system impose?",
                    dependencies=["kafka_arch", "rabbit_arch"],
                ),
            ]
        )
        waves = plan.execution_waves()

        assert [t.id for t in waves[0]] == ["kafka_arch", "rabbit_arch"]
        assert [t.id for t in waves[1]] == ["ops"]

    def test_every_task_appears_exactly_once(self) -> None:
        plan = build(
            [
                task("aa", "How is Kafka's storage architecture designed?"),
                task("bb", "How does RabbitMQ route and queue messages?", dependencies=["aa"]),
                task("cc", "What delivery guarantees does Kafka provide?", dependencies=["bb"]),
            ]
        )
        scheduled = [t.id for wave in plan.execution_waves() for t in wave]

        assert sorted(scheduled) == ["aa", "bb", "cc"]
        assert len(scheduled) == len(set(scheduled))

    def test_a_dependency_never_precedes_its_dependent(self) -> None:
        plan = build(
            [
                task("root", "How is Kafka's storage architecture designed?"),
                task("mid", "What delivery guarantees does Kafka provide?", dependencies=["root"]),
                task("leaf", "Which trade-offs matter at high scale?", dependencies=["mid"]),
            ]
        )
        position = {t.id: index for index, wave in enumerate(plan.execution_waves()) for t in wave}

        for candidate in plan.tasks:
            for dependency in candidate.dependencies:
                assert position[dependency] < position[candidate.id]

    def test_non_parallelizable_task_runs_alone(self) -> None:
        plan = build(
            [
                task("kafka_arch", "How is Kafka's storage architecture designed?"),
                task("rabbit_arch", "How does RabbitMQ route and queue messages?"),
                task(
                    "solo",
                    "Which trade-offs matter most at very high scale?",
                    parallelizable=False,
                ),
            ]
        )
        waves = plan.execution_waves()

        solo_wave = next(w for w in waves if any(t.id == "solo" for t in w))
        assert len(solo_wave) == 1

    def test_independent_tasks_property(self) -> None:
        plan = build(
            [
                task("kafka_arch", "How is Kafka's storage architecture designed?"),
                task(
                    "ops",
                    "What operational overhead does each impose?",
                    dependencies=["kafka_arch"],
                ),
            ]
        )
        assert [t.id for t in plan.independent_tasks] == ["kafka_arch"]

    def test_summary_is_loggable(self) -> None:
        plan = build([task("kafka_arch", "How is Kafka's storage architecture designed?")])
        assert "1 tasks" in plan.summary()


class TestPlanShape:
    def test_a_plan_needs_at_least_one_task(self) -> None:
        with pytest.raises(ValidationError):
            ResearchPlan.model_validate_json(plan_json([]))

    def test_completion_criteria_are_required(self) -> None:
        """Without them nothing defines when research stops."""
        with pytest.raises(ValidationError):
            ResearchPlan.model_validate_json(plan_json(completion_criteria=[]))

    def test_task_defaults_are_sensible(self) -> None:
        """Independent and parallelizable by default, because that is the
        common case and the expensive mistake is a needless dependency."""
        created = ResearchTask(id="aa", question="How is Kafka's architecture designed?")

        assert created.dependencies == []
        assert created.parallelizable is True
        assert created.priority is TaskPriority.MEDIUM
        assert created.source_requirements == [SourceRequirement.ANY]

    def test_lookup_by_id(self) -> None:
        plan = build([task("kafka_arch", "How is Kafka's storage architecture designed?")])

        assert plan.task("kafka_arch").id == "kafka_arch"
        with pytest.raises(KeyError):
            plan.task("nope")


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class TestResearchPlanner:
    async def test_produces_a_validated_plan(self) -> None:
        planner, _ = make_planner(plan_json())
        plan = await planner.plan(SPEC)

        assert isinstance(plan, ResearchPlan)
        assert len(plan.tasks) == 2

    async def test_runs_on_the_strong_tier(self) -> None:
        """A bad decomposition cannot be repaired by later stages, so planning
        is the wrong place to economise."""
        planner, recorder = make_planner(plan_json())
        await planner.plan(SPEC)

        assert recorder.agent_runs[0].model == "gemini-3.7-flash"
        assert recorder.agent_runs[0].tier == "strong"

    async def test_specification_reaches_the_prompt(self) -> None:
        planner, _ = make_planner(plan_json())
        await planner.plan(SPEC)

        rendered = planner.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "architecture" in rendered
        assert "managed service pricing" in rendered
        assert "comparison" in rendered

    async def test_assumptions_are_passed_through(self) -> None:
        """The planner must research under the same assumptions the analyzer
        recorded, or the plan answers a different question."""
        spec = SPEC.model_copy(
            update={
                "ambiguities": [
                    Ambiguity(
                        aspect="scale",
                        why_it_matters="Throughput changes the recommendation",
                        assumption="100k events per second",
                    )
                ]
            }
        )
        planner, _ = make_planner(plan_json())
        await planner.plan(spec)

        rendered = planner.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "100k events per second" in rendered

    async def test_empty_sections_are_labelled_not_blank(self) -> None:
        """A blank section reads as a missing section, and models fill gaps."""
        spec = SPEC.model_copy(update={"constraints": [], "out_of_scope": []})
        planner, _ = make_planner(plan_json())
        await planner.plan(spec)

        rendered = planner.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "(none stated)" in rendered

    async def test_run_is_recorded_with_the_prompt_version(self) -> None:
        planner, recorder = make_planner(plan_json())
        await planner.plan(SPEC, research_id="res_1")

        run = recorder.agent_runs[0]
        assert run.agent == "planner"
        assert run.prompt_name == "planner"
        assert run.prompt_version == "v1"
        assert run.research_id == "res_1"

    async def test_invalid_plan_is_repaired(self) -> None:
        """A cyclic plan from the model is fed back for correction rather than
        reaching the scheduler."""
        cyclic = plan_json(
            [
                task("aa", "How is Kafka's storage architecture designed?", dependencies=["bb"]),
                task("bb", "How does RabbitMQ route and queue messages?", dependencies=["aa"]),
            ]
        )
        planner, recorder = make_planner(cyclic, plan_json())
        plan = await planner.plan(SPEC)

        assert len(plan.tasks) == 2
        assert [r.agent for r in recorder.agent_runs] == ["planner", "planner.repair"]


class TestTaskBudget:
    """The budget is a spending limit the user authorised, so it is enforced in
    code rather than trusted to the prompt."""

    async def test_over_budget_plan_is_trimmed(self) -> None:
        many = [
            task(f"topic_{n}", f"What is the effect of factor number {n} here?") for n in range(10)
        ]
        planner, _ = make_planner(plan_json(many))
        plan = await planner.plan(SPEC, depth=ResearchDepth.QUICK)

        assert len(plan.tasks) == 3  # quick budget

    async def test_trimming_keeps_high_priority_tasks(self) -> None:
        tasks = [
            task("low_one", "What is the effect of the first minor factor?", priority="low"),
            task("low_two", "What is the effect of the second minor factor?", priority="low"),
            task("critical", "How is Kafka's storage architecture designed?", priority="high"),
            task("also_key", "How does RabbitMQ route and queue messages?", priority="high"),
        ]
        planner, _ = make_planner(plan_json(tasks))
        plan = await planner.plan(SPEC, depth=ResearchDepth.QUICK)

        kept = {t.id for t in plan.tasks}
        assert "critical" in kept
        assert "also_key" in kept

    async def test_trimming_never_breaks_a_dependency(self) -> None:
        """Dropping a depended-upon task would leave a dangling reference, and
        rewriting the dependent's dependencies would change what it researches."""
        tasks = [
            task("base", "How is Kafka's storage architecture designed?", priority="low"),
            task("mid", "What delivery guarantees does Kafka provide?", dependencies=["base"]),
            task("extra_one", "What is the effect of the first minor factor?", priority="low"),
            task("extra_two", "What is the effect of the second minor factor?", priority="low"),
            task("extra_three", "What is the effect of the third minor factor?", priority="low"),
        ]
        planner, _ = make_planner(plan_json(tasks))
        plan = await planner.plan(SPEC, depth=ResearchDepth.QUICK)

        kept = {t.id for t in plan.tasks}
        if "mid" in kept:
            assert "base" in kept

    async def test_untrimmable_plan_raises(self) -> None:
        """A chain longer than the budget cannot be trimmed without breaking it,
        and silently returning an over-budget plan would exceed what the user
        authorised."""
        chain = [task("t0", "What is the effect of factor number 0 here?")]
        chain += [
            task(
                f"t{n}",
                f"What is the effect of factor number {n} here?",
                dependencies=[f"t{n - 1}"],
            )
            for n in range(1, 6)
        ]
        planner, _ = make_planner(plan_json(chain))

        with pytest.raises(PlanTooLargeError, match="cannot be trimmed"):
            await planner.plan(SPEC, depth=ResearchDepth.QUICK)

    async def test_within_budget_plan_is_untouched(self) -> None:
        planner, _ = make_planner(plan_json())
        plan = await planner.plan(SPEC, depth=ResearchDepth.STANDARD)

        assert len(plan.tasks) == 2


class TestPromptContract:
    def test_prompt_declares_its_variables(self) -> None:
        assert "max_tasks" in PLANNER_V1.variables
        assert "assumptions" in PLANNER_V1.variables

    def test_prompt_discourages_needless_dependencies(self) -> None:
        """Dependencies force sequential execution, which is the expensive
        default mistake."""
        assert "independent by default" in PLANNER_V1.system.lower()

    def test_prompt_requires_symmetric_comparison_coverage(self) -> None:
        assert "symmetrically" in PLANNER_V1.system.lower()

    def test_prompt_forbids_answering(self) -> None:
        assert "do not answer" in PLANNER_V1.system.lower()
