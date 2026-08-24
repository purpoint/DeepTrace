"""Tests for the benchmark dataset itself.

A benchmark is data, and data rots quietly. These are the checks that keep it
from drifting into something that measures less than it claims: one category
quietly dominating, a duplicated question inflating a score, or a question with
no declared concepts that therefore cannot be scored for coverage at all.
"""

from __future__ import annotations

import pytest

from core.evaluation.dataset import BENCHMARK, by_type, contested, coverage_summary
from core.models.query import ResearchType


class TestShape:
    def test_the_suite_is_within_the_size_the_roadmap_asks_for(self) -> None:
        assert 20 <= len(BENCHMARK) <= 50

    def test_ids_are_unique(self) -> None:
        """A duplicated id would overwrite a result in the JSONL file and make
        a resumed benchmark silently skip a question."""
        ids = [question.id for question in BENCHMARK]

        assert len(ids) == len(set(ids))

    def test_questions_are_unique(self) -> None:
        texts = [question.question.lower() for question in BENCHMARK]

        assert len(texts) == len(set(texts))

    @pytest.mark.parametrize("research_type", list(ResearchType))
    def test_every_research_type_is_represented(self, research_type: ResearchType) -> None:
        """The system classifies into five types and plans differently for each.
        A benchmark missing one cannot detect a planner that is bad at it."""
        assert len(by_type(research_type)) >= 3

    def test_no_single_type_dominates(self) -> None:
        """A suite that is half comparisons reports a comparison score and calls
        it an overall one."""
        counts = coverage_summary()

        assert max(counts.values()) <= len(BENCHMARK) // 2

    def test_some_questions_are_contested(self) -> None:
        """A system that averages away a real disagreement scores well on every
        other metric while doing the most misleading thing available to it.
        Without contested questions, nothing here would notice."""
        assert len(contested()) >= 4


class TestScoreability:
    def test_every_question_declares_concepts(self) -> None:
        """Coverage is scored against declared concepts. A question without them
        is a question that silently contributes nothing to that metric."""
        missing = [question.id for question in BENCHMARK if not question.concepts]

        assert missing == []

    def test_questions_are_specific_enough_to_research(self) -> None:
        """A topic is not a question.

        Length rather than punctuation. An imperative -- "Compare Kafka and
        RabbitMQ for high-scale microservice messaging." -- is a perfectly good
        research prompt, and is in fact the example the application shows users
        in its own placeholder text. Requiring a question mark would have
        rejected every comparison in the suite for being phrased the way the
        product asks people to phrase them.
        """
        for question in BENCHMARK:
            assert len(question.question.split()) >= 6, question.id
            assert question.question.strip()[-1] in ".?", question.id

    def test_no_question_carries_an_expected_answer(self) -> None:
        """Deliberate. Scoring similarity to a written-down answer measures
        agreement with whoever wrote the key, and for questions like these there
        is no single right answer -- so the benchmark scores whether the work was
        done properly instead."""
        for question in BENCHMARK:
            assert not hasattr(question, "expected_answer")
