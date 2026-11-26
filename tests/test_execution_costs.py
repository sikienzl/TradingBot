"""Unit tests for the pre-trade execution-cost estimates (audit point 5).

Everything here is pure: no exchange, no ``BotConfig`` and no bot object, which
is exactly why the money maths lives in :mod:`src.execution_costs`.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.execution_costs import (
    DEFAULT_SLIPPAGE_RATE,
    DEFAULT_TAKER_FEE_RATE,
    ESTIMATE_JOURNAL_FIELDS,
    FILL_JOURNAL_FIELDS,
    build_cost_estimate,
    estimate_execution_costs,
    execution_cost_block_reason,
    execution_cost_journal_fields,
    fee_amount_in_quote,
    fee_rate_from_market_info,
    format_execution_cost_estimate,
    normalize_requested_amount_coin,
    partial_fill_journal_fields,
    quote_from_market_data,
    quote_from_record,
    resolve_execution_cost_rates,
)


@dataclass
class _Config:
    max_entry_cost_bps: float = 0.0
    live_taker_fee_rate: float | None = None
    live_slippage_rate: float | None = None


# ---------------------------------------------------------------------------
# Fee tier resolution
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("market_info", [
    None,
    {},
    {"takerFee": "garbage"},
    {"takerFee": -1.0},
    {"takerFee": 100.0},
    "not-a-mapping",
])
def test_missing_or_invalid_market_fee_falls_back_to_default_tier(market_info):
    assert fee_rate_from_market_info(market_info) == DEFAULT_TAKER_FEE_RATE


def test_market_fee_percentage_shape_is_accepted():
    assert fee_rate_from_market_info({"takerFee": 0.26}) == pytest.approx(0.0026)
    assert fee_rate_from_market_info({"makerFee": 0.16}) == pytest.approx(0.0016)


def test_market_fee_fraction_shape_is_accepted():
    # The parsed Kraken market exposes maker/taker as decimal fractions.
    assert fee_rate_from_market_info({"taker": 0.004}) == pytest.approx(0.004)
    assert fee_rate_from_market_info({"maker": 0.002}) == pytest.approx(0.002)


def test_taker_wins_over_maker():
    assert fee_rate_from_market_info(
        {"takerFee": 0.26, "makerFee": 0.16}) == pytest.approx(0.0026)


def test_operator_settings_win_over_the_exchange():
    config = _Config(live_taker_fee_rate=0.001, live_slippage_rate=0.002)
    assert resolve_execution_cost_rates(config, {"taker": 0.004}) == (0.001, 0.002)


def test_out_of_range_operator_settings_are_ignored():
    config = _Config(live_taker_fee_rate=1.5, live_slippage_rate=-0.1)
    fee, slippage = resolve_execution_cost_rates(config, {"taker": 0.004})
    assert fee == pytest.approx(0.004)
    assert slippage == pytest.approx(DEFAULT_SLIPPAGE_RATE)


# ---------------------------------------------------------------------------
# Quote extraction
# ---------------------------------------------------------------------------
def test_quote_from_market_data_reads_the_per_iteration_record():
    quote = quote_from_market_data(
        {"BTC": {"bid": 99.0, "ask": 101.0, "price": 100.0}}, "BTC", 100.0)
    assert quote is not None
    assert quote["bid"] == 99.0 and quote["ask"] == 101.0
    assert quote["spread_bps"] == pytest.approx(200.0)


def test_quote_is_absent_without_any_quote_fields():
    assert quote_from_market_data({"BTC": {"price": 100.0}}, "BTC", 100.0) is None
    assert quote_from_market_data(None, "BTC", 100.0) is None
    assert quote_from_record({"bid": 0, "ask": 0, "spread_bps": -1}, 100.0) is None


def test_negative_spread_is_dropped_but_bid_ask_kept():
    quote = quote_from_record({"bid": 99.0, "ask": 101.0, "spread_bps": -5.0}, 100.0)
    assert quote is not None
    assert quote["spread_bps"] == pytest.approx(200.0)


def test_stale_quote_is_not_charged_to_the_order():
    quote = quote_from_record({"bid": 99.0, "ask": 101.0, "spread_bps": 202.0}, 130.0)
    assert quote is not None
    assert quote["spread_bps"] is None
    assert quote["bid"] == 99.0


# ---------------------------------------------------------------------------
# Cost estimate
# ---------------------------------------------------------------------------
def test_estimate_adds_fee_slippage_and_observed_spread():
    estimate = estimate_execution_costs(
        "BTC", 100.0, 100.0, 0.0026, 0.001,
        {"bid": 99.9, "ask": 100.1, "spread_bps": 20.0})
    assert estimate is not None
    assert estimate["fee_base"] == pytest.approx(0.26)
    assert estimate["slippage_base"] == pytest.approx(0.10)
    assert estimate["spread_base"] == pytest.approx(0.20)
    assert estimate["total_cost_base"] == pytest.approx(0.56)
    assert estimate["cost_pct"] == pytest.approx(0.56)


def test_estimate_without_quote_keeps_spread_unknown():
    estimate = estimate_execution_costs("BTC", 100.0, 50.0, 0.0026, 0.001, None)
    assert estimate is not None
    assert estimate["spread_base"] is None
    assert estimate["spread_bps"] is None
    assert estimate["total_cost_base"] == pytest.approx(0.18)


def test_normalized_quote_without_spread_is_not_recomputed():
    """A dropped spread stays dropped instead of being re-derived from bid/ask."""
    quote = quote_from_record({"bid": 80.0, "ask": 82.0, "spread_bps": 250.0}, 100.0)
    assert quote is not None and quote["spread_bps"] is None
    estimate = estimate_execution_costs("BTC", 100.0, 100.0, 0.0026, 0.001, quote)
    assert estimate is not None
    assert estimate["spread_bps"] is None
    assert estimate["spread_base"] is None
    assert estimate["total_cost_base"] == pytest.approx(0.36)
    # A raw record without the key still gets the observed spread.
    raw = estimate_execution_costs(
        "BTC", 100.0, 100.0, 0.0026, 0.001, {"bid": 99.0, "ask": 101.0})
    assert raw is not None
    assert raw["spread_bps"] == pytest.approx(200.0)


@pytest.mark.parametrize("price, amount, fee, slippage", [
    (0.0, 100.0, 0.0026, 0.001),
    (-1.0, 100.0, 0.0026, 0.001),
    (float("nan"), 100.0, 0.0026, 0.001),
    (100.0, 0.0, 0.0026, 0.001),
    (100.0, 100.0, -0.01, 0.001),
    (100.0, 100.0, 0.0026, float("nan")),
])
def test_unusable_order_request_has_no_estimate(price, amount, fee, slippage):
    assert estimate_execution_costs(
        "BTC", price, amount, fee, slippage, None) is None


def test_build_cost_estimate_uses_market_fee_and_quote():
    estimate = build_cost_estimate(
        _Config(), "BTC", 100.0, 100.0,
        market_info={"taker": 0.004},
        quote={"bid": 99.9, "ask": 100.1, "spread_bps": 10.0})
    assert estimate is not None
    assert estimate["taker_fee_rate"] == pytest.approx(0.004)
    assert estimate["total_cost_base"] == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# Cost budget gate
# ---------------------------------------------------------------------------
def test_cost_budget_is_disabled_below_one_bps():
    estimate = estimate_execution_costs("BTC", 100.0, 100.0, 0.0026, 0.001, None)
    for budget in (0.0, 0.5, -10.0, float("nan")):
        assert execution_cost_block_reason(
            _Config(max_entry_cost_bps=budget), estimate) is None
    assert execution_cost_block_reason(_Config(), None) is None


def test_cost_budget_refuses_an_expensive_entry():
    estimate = estimate_execution_costs(
        "BTC", 100.0, 100.0, 0.0026, 0.001, {"spread_bps": 32.4})
    reason = execution_cost_block_reason(_Config(max_entry_cost_bps=12.0), estimate)
    assert reason is not None
    assert reason.startswith("estimated_cost_above_limit (")
    assert reason.endswith("bps > 12.0bps)")


def test_cost_budget_accepts_a_cheap_entry():
    estimate = estimate_execution_costs(
        "BTC", 100.0, 100.0, 0.0026, 0.001, {"spread_bps": 1.0})
    assert execution_cost_block_reason(
        _Config(max_entry_cost_bps=120.0), estimate) is None


def test_estimate_without_spread_is_journaled_as_empty():
    estimate = estimate_execution_costs("BTC", 100.0, 100.0, 0.0026, 0.001, None)
    fields = execution_cost_journal_fields(estimate)
    assert set(fields) == set(ESTIMATE_JOURNAL_FIELDS)
    assert fields["est_spread_base"] == ""
    assert fields["est_spread_bps"] == ""
    assert fields["est_total_cost_base"] == pytest.approx(0.36)
    assert execution_cost_journal_fields(None) == {}


def test_format_estimate_is_single_line():
    estimate = estimate_execution_costs("BTC", 100.0, 100.0, 0.0026, 0.001, None)
    text = format_execution_cost_estimate(estimate)
    assert "\n" not in text
    assert "spread=n/a" in text
    assert format_execution_cost_estimate(None) == "cost estimate unavailable"


# ---------------------------------------------------------------------------
# Partial fill bookkeeping
# ---------------------------------------------------------------------------
def test_partial_fill_is_recorded():
    fields = partial_fill_journal_fields(1.0, 0.4)
    assert fields == {"requested_amount_coin": 1.0, "partial_fill": 1}
    assert set(FILL_JOURNAL_FIELDS) == set(fields)


def test_full_fill_is_not_a_partial_fill():
    assert partial_fill_journal_fields(1.0, 1.0)["partial_fill"] == 0
    assert partial_fill_journal_fields(1.0, 1.0 + 1e-12)["partial_fill"] == 0


def test_unknown_requested_quantity_stays_empty():
    assert partial_fill_journal_fields(None, 0.4) == {
        "requested_amount_coin": "", "partial_fill": ""}
    assert partial_fill_journal_fields(float("nan"), 0.4) == {
        "requested_amount_coin": "", "partial_fill": ""}


def test_requested_amount_falls_back_to_base_over_price():
    assert normalize_requested_amount_coin(0.5, 50.0, 100.0) == 0.5
    assert normalize_requested_amount_coin(None, 50.0, 100.0) == 0.5
    assert normalize_requested_amount_coin(None, 50.0, 0.0) == 0.0
    assert normalize_requested_amount_coin(None, None, None) == 0.0


# ---------------------------------------------------------------------------
# Exchange reported fees
# ---------------------------------------------------------------------------
def test_quote_currency_fees_are_summed():
    total, raw = fee_amount_in_quote(
        {"fees": [{"currency": "EUR", "cost": 0.26}, {"currency": "eur", "cost": 0.14}]},
        "EUR", "BTC", 100.0)
    assert total == pytest.approx(0.40)
    assert "EUR" in raw


def test_base_asset_fees_are_converted_with_the_fill_price():
    total, _ = fee_amount_in_quote(
        {"fee": {"currency": "BTC", "cost": 0.001}}, "EUR", "BTC", 100.0)
    assert total == pytest.approx(0.1)


def test_unusable_fee_records_are_reported_not_guessed():
    total, raw = fee_amount_in_quote({"fees": [{"currency": "XBT", "cost": 1}]},
                                     "EUR", "BTC", 100.0)
    assert total is None
    assert "XBT" in raw
    assert fee_amount_in_quote({}, "EUR", "BTC", 100.0) == (None, "")
    assert fee_amount_in_quote({"fees": [{"cost": 1}]}, "EUR", "BTC", 100.0)[0] is None
