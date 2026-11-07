"""Per-model prices. The gateway's price table is also its model allowlist.

Prices are ``Decimal`` US dollars per million tokens, so list prices such as
$0.10 or $2.50 per million are exact, and so is the cost of a single token.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal

    def __post_init__(self) -> None:
        # str() first so a float like 0.1 becomes Decimal("0.1"), not its binary expansion.
        object.__setattr__(self, "input_usd_per_mtok", Decimal(str(self.input_usd_per_mtok)))
        object.__setattr__(self, "output_usd_per_mtok", Decimal(str(self.output_usd_per_mtok)))

    def cost_cents(self, input_tokens: int, output_tokens: int) -> Decimal:
        # tokens x dollars-per-million-tokens = millionths of a dollar
        micro_dollars = (
            input_tokens * self.input_usd_per_mtok + output_tokens * self.output_usd_per_mtok
        )
        return micro_dollars / 10_000


# Anthropic list prices as of November 2026. Check them against
# https://platform.claude.com/docs/en/about-claude/pricing before billing with them.
ANTHROPIC_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5-20251001": ModelPrice("1", "5"),
    "claude-sonnet-5-5": ModelPrice("2", "10"),
    "claude-opus-5-5": ModelPrice("4", "20"),
}
