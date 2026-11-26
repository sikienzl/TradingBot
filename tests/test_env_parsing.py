"""Unit tests for the reusable environment parsers in ``src.env_parsing``.

These helpers were extracted verbatim from :mod:`src.bot_config`; the tests pin
the parsing rules so the behaviour stays identical after the move.
"""

from __future__ import annotations

import pytest

from src.env_parsing import (
    env_bool,
    env_optional_float,
    env_str,
    symbol_float_map,
    symbol_int_map,
    threshold_float_pairs,
    threshold_int_pairs,
)

# ---------------------------------------------------------------------------
# env_bool / env_str
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("  YES ", True),
        ("y", True),
        ("On", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("nope", False),
    ],
)
def test_env_bool_accepts_documented_truthy_values(monkeypatch, raw, expected):
    monkeypatch.setenv("FLAG", raw)
    assert env_bool("FLAG", False) is expected


def test_env_bool_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("FLAG", raising=False)
    assert env_bool("FLAG", True) is True


def test_env_str_strips_quotes_and_whitespace(monkeypatch):
    monkeypatch.setenv("TEXT", '  "EUR/USD"  ')
    assert env_str("TEXT", "fallback") == "EUR/USD"


def test_env_str_uses_default_only_when_unset(monkeypatch):
    monkeypatch.delenv("TEXT", raising=False)
    assert env_str("TEXT", "fallback") == "fallback"
    monkeypatch.setenv("TEXT", "")
    assert env_str("TEXT", "fallback") == ""


# ---------------------------------------------------------------------------
# env_optional_float
# ---------------------------------------------------------------------------


def test_env_optional_float_empty_means_not_configured(monkeypatch):
    monkeypatch.setenv("MAYBE", "   ")
    assert env_optional_float("MAYBE") is None
    monkeypatch.delenv("MAYBE", raising=False)
    assert env_optional_float("MAYBE") is None


def test_env_optional_float_parses_values(monkeypatch):
    monkeypatch.setenv("MAYBE", "0.0026")
    assert env_optional_float("MAYBE") == pytest.approx(0.0026)


@pytest.mark.parametrize("raw", ["abc", "nan", "inf", "-inf"])
def test_env_optional_float_rejects_non_finite_values(monkeypatch, raw):
    monkeypatch.setenv("MAYBE", raw)
    with pytest.raises(ValueError, match="MAYBE must be a finite number"):
        env_optional_float("MAYBE")


# ---------------------------------------------------------------------------
# symbol maps
# ---------------------------------------------------------------------------


def test_symbol_int_map_skips_malformed_entries(monkeypatch):
    monkeypatch.setenv(
        "MAP", "btc:12, ETH:007 , :5, XAU:, junk, ADA:4.5, SOL: 9 ")
    assert symbol_int_map("MAP") == {"BTC": 12, "ETH": 7, "SOL": 9}


def test_symbol_int_map_empty_when_unset(monkeypatch):
    monkeypatch.delenv("MAP", raising=False)
    assert symbol_int_map("MAP") == {}
    assert symbol_int_map("MAP", "xrp:3") == {"XRP": 3}


def test_symbol_float_map_uses_default_and_uppercases(monkeypatch):
    monkeypatch.delenv("MAP", raising=False)
    assert symbol_float_map("MAP", "doge:0.5") == {"DOGE": 0.5}


def test_symbol_float_map_skips_non_numeric_values(monkeypatch):
    monkeypatch.setenv("MAP", "BTC:1.5,EUR:oops")
    assert symbol_float_map("MAP") == {"BTC": 1.5}


# ---------------------------------------------------------------------------
# threshold pairs
# ---------------------------------------------------------------------------


def test_threshold_float_pairs_sorted_by_threshold(monkeypatch):
    monkeypatch.setenv("TIERS", "200:1.5, 50:1.1 ,bad:1,100:")
    assert threshold_float_pairs("TIERS") == [(50.0, 1.1), (200.0, 1.5)]


def test_threshold_float_pairs_empty_when_unset(monkeypatch):
    monkeypatch.delenv("TIERS", raising=False)
    assert threshold_float_pairs("TIERS") == []


def test_threshold_int_pairs_rejects_fractional_values(monkeypatch):
    monkeypatch.setenv("TIERS", "100:3,200:4.5,300:5")
    assert threshold_int_pairs("TIERS") == [(100.0, 3), (300.0, 5)]


def test_threshold_int_pairs_uses_default(monkeypatch):
    monkeypatch.delenv("TIERS", raising=False)
    assert threshold_int_pairs("TIERS", "100:3,200:4") == [
        (100.0, 3),
        (200.0, 4),
    ]
