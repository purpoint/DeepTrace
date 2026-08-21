"""Model pricing and cost estimation.

Cost is reported per research run, and those figures are meant to be quotable.
That imposes two rules on this module.

**Never guess.** An unrecognised model returns ``None``, not an approximation.
A fabricated cost is worse than an absent one, because an absent one is visibly
missing while a fabricated one silently corrupts every total it feeds.

**Never use floats for money.** ``Decimal`` throughout. Accumulating float
rounding error across thousands of calls produces totals that drift from what
the provider actually bills.

Prices below are *recorded* values with the date they were checked, not
guarantees. Providers change pricing, so :data:`PRICING` is data to be updated
rather than a constant to be trusted indefinitely. Run ``deeptrace pricing`` to
see what is configured and when it was last verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from core.llm.base import TokenUsage

PRICING_LAST_VERIFIED = date(2025, 6, 1)
"""When the table below was last checked against provider pricing pages.

Cost figures should be re-verified before being published anywhere, including
in a report, a dashboard, or a resume.
"""

# Prices are per one million tokens, in USD.
_M = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Per-million-token prices for one model.

    ``cached_input`` is separate because providers discount cached input
    substantially; treating it as full price overstates cost on prompt-heavy
    workloads, which research pipelines are.
    """

    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None
    verified_on: date = PRICING_LAST_VERIFIED

    def cost_for(self, usage: TokenUsage) -> Decimal:
        """Compute the cost of one call from its token usage."""
        if self.cached_input_per_million is not None and usage.cached_tokens:
            input_cost = (
                Decimal(usage.billable_input_tokens) * self.input_per_million
                + Decimal(usage.cached_tokens) * self.cached_input_per_million
            ) / _M
        else:
            input_cost = Decimal(usage.input_tokens) * self.input_per_million / _M

        output_cost = Decimal(usage.output_tokens) * self.output_per_million / _M
        return input_cost + output_cost


def _usd(amount: str) -> Decimal:
    """Build a price from a string so no float ever touches a money value."""
    return Decimal(amount)


PRICING: dict[str, ModelPricing] = {
    # -- Google Gemini -----------------------------------------------------
    #
    # Rates are the paid tier. A run on a provider's free tier bills zero, and
    # these figures are then the paid-tier equivalent -- useful for reporting
    # what a run would cost at scale. State which of the two a number
    # represents; they are not the same claim.
    #
    # The models currently routed to (see LLM_MODEL_* in .env) are deliberately
    # absent until their prices are read off the provider's pricing page and
    # entered here. Until then estimate_cost returns None and the run log shows
    # "unknown", which is the correct report for a cost that has not been
    # verified. Adding a guessed price would silently corrupt every total that
    # depends on it, including any figure that ends up on a resume.
    #
    #   To populate: https://ai.google.dev/gemini-api/docs/pricing
    #   Then update PRICING_LAST_VERIFIED above.
    #
    # -- OpenAI ------------------------------------------------------------
    "gpt-4o": ModelPricing(
        input_per_million=_usd("2.50"),
        output_per_million=_usd("10.00"),
        cached_input_per_million=_usd("1.25"),
    ),
    "gpt-4o-mini": ModelPricing(
        input_per_million=_usd("0.15"),
        output_per_million=_usd("0.60"),
        cached_input_per_million=_usd("0.075"),
    ),
    "text-embedding-3-small": ModelPricing(
        input_per_million=_usd("0.02"),
        output_per_million=_usd("0"),
    ),
    "text-embedding-3-large": ModelPricing(
        input_per_million=_usd("0.13"),
        output_per_million=_usd("0"),
    ),
}
"""Recorded prices per million tokens, USD.

Adding a provider means adding its models here. A model absent from this table
still runs -- it simply reports unknown cost rather than a fabricated one.
"""


def normalise_model(model: str) -> str:
    """Strip a dated snapshot suffix so ``gpt-4o-2024-11-20`` prices as ``gpt-4o``.

    Providers publish dated snapshots that share the base model's price. Matching
    the longest known prefix avoids an entry per snapshot while still returning
    ``None`` for genuinely unknown models.
    """
    if model in PRICING:
        return model
    candidates = [known for known in PRICING if model.startswith(known)]
    return max(candidates, key=len) if candidates else model


def get_pricing(model: str) -> ModelPricing | None:
    """Return pricing for a model, or ``None`` when it is not recorded."""
    return PRICING.get(normalise_model(model))


def estimate_cost(model: str, usage: TokenUsage) -> Decimal | None:
    """Estimate the cost of one call.

    Returns ``None`` when the model has no recorded price. Callers must treat
    that as "unknown" and propagate it, never coerce it to zero -- a run whose
    cost silently reads ``$0.00`` looks free rather than unmeasured.
    """
    pricing = get_pricing(model)
    return pricing.cost_for(usage) if pricing is not None else None


def format_cost(cost: Decimal | None) -> str:
    """Render a cost for display, distinguishing unknown from free.

    Sub-cent amounts keep four decimal places, because individual calls in a
    research run routinely cost a fraction of a cent and rounding them to
    ``$0.00`` makes per-call cost tracking useless.
    """
    if cost is None:
        return "unknown"
    if cost == 0:
        return "$0.0000"
    if cost < Decimal("0.01"):
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def known_models() -> tuple[str, ...]:
    """Models with recorded pricing, sorted for stable display."""
    return tuple(sorted(PRICING))
