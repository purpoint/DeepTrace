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

PRICING_LAST_VERIFIED = date(2026, 8, 24)
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

    superseded_on: date | None = None
    """When this price is known to stop being correct.

    Providers run promotions with published end dates, and a promotional rate
    recorded without its expiry is a number that silently becomes wrong on a
    date somebody already told us about. Costs computed after this date are
    still reported -- refusing would be worse -- but ``is_stale`` marks them, so
    a figure being quoted can be checked before it is quoted.
    """

    def is_stale(self, on: date | None = None) -> bool:
        """Whether this price is past its published end date."""
        if self.superseded_on is None:
            return False
        return (on or date.today()) >= self.superseded_on

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
    # Read off https://ai.google.dev/gemini-api/docs/pricing on 2026-08-24.
    # Before this they were deliberately absent, so every run reported an
    # unknown cost -- correct, but it made the cost view an empty column.
    #
    # gemini-3.7-flash is on promotional pricing that DOUBLES on 2027-01-01:
    # input 0.75 -> 1.50 and output 3.75 -> 7.50. Recorded here as the rate in
    # force today, with ``superseded_on`` naming the date it stops being true,
    # so a total computed next January is visibly stale rather than quietly
    # wrong. See PROMOTIONAL below.
    #
    "gemini-3.7-flash": ModelPricing(
        input_per_million=_usd("0.75"),
        output_per_million=_usd("3.75"),
        cached_input_per_million=_usd("0.075"),
        superseded_on=date(2027, 1, 1),
    ),
    "gemini-3.5-flash": ModelPricing(
        input_per_million=_usd("1.50"),
        output_per_million=_usd("9.00"),
        cached_input_per_million=_usd("0.15"),
    ),
    "gemini-3.5-flash-lite": ModelPricing(
        input_per_million=_usd("0.30"),
        output_per_million=_usd("2.50"),
        cached_input_per_million=_usd("0.03"),
    ),
    "gemini-embedding-001": ModelPricing(
        input_per_million=_usd("0.15"),
        output_per_million=_usd("0"),
    ),
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
