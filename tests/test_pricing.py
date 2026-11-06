"""ModelPrice unit tests."""

from __future__ import annotations

from decimal import Decimal

from llm_gateway import ModelPrice


def test_sub_cent_list_prices_are_exact():
    price = ModelPrice("0.10", "0.50")
    assert price.cost_cents(input_tokens=1, output_tokens=1) == Decimal("0.00006")
    assert price.cost_cents(input_tokens=1_000_000, output_tokens=0) == Decimal("10")


def test_float_prices_are_read_as_written():
    assert ModelPrice(0.1, 2.5) == ModelPrice("0.1", "2.5")
