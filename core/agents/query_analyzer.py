"""Query analyzer: the first agent in the research pipeline.

Takes a raw human question and produces a :class:`QuerySpec` -- a precise,
executable research specification. Everything downstream reads from that spec
rather than re-interpreting the original sentence, so the interpretation happens
once, is recorded, and is inspectable in the trace.

The agent is thin on purpose. It renders a versioned prompt, asks the client for
a validated structured response, and logs what it produced. It does not retry,
price, or record runs; the client does all of that for every agent identically.
"""

from __future__ import annotations

from core.config import ResearchDepth
from core.llm.client import LLMClient
from core.logging import get_logger
from core.models.query import QuerySpec
from core.prompts.analyzer import QUERY_ANALYZER_V1
from core.prompts.registry import Prompt

log = get_logger(__name__)

AGENT_NAME = "query_analyzer"


class QueryAnalyzer:
    """Converts a research question into a structured specification."""

    def __init__(self, client: LLMClient, *, prompt: Prompt = QUERY_ANALYZER_V1) -> None:
        """Args:
        client: Vendor-neutral LLM client.
        prompt: Injected so a regression test can pin an older version and
            compare, which is the point of versioning prompts at all.
        """
        self.client = client
        self.prompt = prompt

    async def analyze(
        self,
        question: str,
        *,
        depth: ResearchDepth = ResearchDepth.STANDARD,
        research_id: str | None = None,
    ) -> QuerySpec:
        """Analyse a question.

        Raises:
            ValueError: The question is empty or whitespace.
            StructuredOutputError: The model could not produce a valid spec
                after the client's bounded repair attempts.
        """
        cleaned = question.strip()
        if not cleaned:
            raise ValueError("Research question must not be empty.")

        spec = await self.client.complete_structured(
            self.prompt,
            QuerySpec,
            {"question": cleaned, "depth": depth.value},
            agent=AGENT_NAME,
            research_id=research_id,
        )

        log.info(
            "research.analyzed",
            research_id=research_id,
            research_type=spec.research_type.value,
            scope_items=len(spec.scope),
            ambiguities=len(spec.ambiguities),
            time_sensitivity=spec.time_sensitivity.value,
            requires_current_information=spec.requires_current_information,
            prompt_version=self.prompt.version,
        )
        return spec
