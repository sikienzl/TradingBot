#!/usr/bin/env python3
from trading_bot import BotConfig, CryptoTradingBot, logger as bot_logger
import argparse
import logging
import os
import sys
from collections import Counter
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_COINS = ["BTC", "ETH", "SOL", "XRP",
                 "ADA", "DOGE", "TRX", "CHZ", "VVV"]
DEFAULT_REGIMES = ["uptrend", "sideways",
                   "downtrend", "crash", "recovery", "mixed"]


def _parse_csv(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _format_rsi(value) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "n/a"


def _has_valid_number(value) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return numeric == numeric


def _build_bot(regime: str, seed: int, top_n: int, coins: list[str]) -> CryptoTradingBot:
    os.environ["SIMULATE_DATA"] = "true"
    os.environ["DRY_RUN"] = "true"
    os.environ["USE_TABULAR_MODEL"] = "false"
    os.environ["USE_ML_MODEL"] = "false"
    os.environ["PERFORMANCE_LOG_ENABLED"] = "false"
    os.environ["SIMULATION_SEED"] = str(seed)
    os.environ["SIMULATION_REGIME"] = regime
    os.environ["TOP_N_FOR_ANALYSIS"] = str(max(1, top_n))

    bot_logger.setLevel(logging.WARNING)
    bot = CryptoTradingBot(BotConfig())
    bot.all_coins = coins
    bot.all_symbols = [f"{coin}/{bot.config.base_currency}" for coin in coins]
    return bot


def _summarize_regime(regime: str, seed: int, top_n: int, coins: list[str]) -> dict:
    bot = _build_bot(regime=regime, seed=seed, top_n=top_n, coins=coins)
    market_data = bot._get_market_data()
    analysis = bot._analyze_markets(market_data)

    occupied_positions = set(bot.portfolio.open_trades.keys()) | set(
        bot.portfolio.holdings.keys())
    available_trade_slots = max(
        0, bot.config.max_open_trades - len(bot.portfolio.open_trades))
    downtrend_filter_results = {
        coin: bot._passes_downtrend_reversal_filter(data)
        for coin, data in analysis.items()
    }
    uptrend_filter_results = {
        coin: bot._passes_uptrend_entry_filter(data)
        for coin, data in analysis.items()
    }
    entry_mode = bot._entry_market_mode(analysis)

    recommendation_mix = Counter(
        data.get("recommendation", "UNKNOWN") for data in analysis.values()
    )
    strict_buy_candidates = [
        coin for coin, data in analysis.items()
        if coin not in occupied_positions
        and data.get("recommendation") in ["STRONG BUY", "BUY"]
        and data.get("score", 0) >= bot.config.min_entry_score
    ]
    top_buy_recommendations = strict_buy_candidates[:min(
        bot.config.top_n_for_analysis, available_trade_slots)]

    excluded_signals_fallback = ["SELL", "WEAK SELL"]
    if bot.config.exit_on_downtrend:
        excluded_signals_fallback.append("HOLD (Down-Trend)")

    fallback_allowed = (
        bot.config.enable_fallback_entry
        and available_trade_slots > len(top_buy_recommendations)
        and (
            not bot.config.defensive_entry_mode_enabled
            or entry_mode != "defensive"
        )
    )
    fallback_candidates = [
        coin for coin, data in analysis.items()
        if coin not in occupied_positions
        and coin not in top_buy_recommendations
        and data.get("score", 0) >= bot.config.fallback_min_score
        and data.get("recommendation") not in excluded_signals_fallback
        and downtrend_filter_results.get(coin, (False, "not_evaluated"))[0]
        and uptrend_filter_results.get(coin, (False, "not_evaluated"))[0]
        and _has_valid_number(data.get("rsi"))
        and float(data.get("rsi")) <= bot.config.fallback_max_rsi
    ]

    force_fill_allowed = (
        bot.config.force_fill_slots
        and available_trade_slots > len(top_buy_recommendations)
        and (
            not bot.config.defensive_entry_mode_enabled
            or entry_mode == "normal"
        )
    )
    force_fill_candidates = [
        coin for coin, data in analysis.items()
        if coin not in occupied_positions
        and coin not in top_buy_recommendations
        and data.get("score", 0) >= bot.config.force_fill_min_score
        and data.get("recommendation") not in excluded_signals_fallback
        and downtrend_filter_results.get(coin, (False, "not_evaluated"))[0]
        and uptrend_filter_results.get(coin, (False, "not_evaluated"))[0]
    ]

    downtrend_reversal_eligible = sum(
        1
        for coin, data in analysis.items()
        if data.get("recommendation") == "HOLD (Down-Trend)"
        and downtrend_filter_results.get(coin, (False, "not_evaluated"))[0]
    )

    top_entries = []
    for coin, data in list(analysis.items())[:top_n]:
        top_entries.append({
            "coin": coin,
            "recommendation": data.get("recommendation", "HOLD"),
            "score": int(data.get("score", 0)),
            "rsi": data.get("rsi"),
            "price": float(data.get("price", 0.0)),
        })

    return {
        "regime": regime,
        "mix": dict(recommendation_mix),
        "entry_mode": entry_mode,
        "strict_buy_candidates": strict_buy_candidates,
        "fallback_allowed": fallback_allowed,
        "fallback_candidates": fallback_candidates,
        "force_fill_allowed": force_fill_allowed,
        "force_fill_candidates": force_fill_candidates,
        "downtrend_reversal_eligible": downtrend_reversal_eligible,
        "top_entries": top_entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare trading bot recommendations across multiple simulated market regimes."
    )
    parser.add_argument(
        "--coins",
        default=",".join(DEFAULT_COINS),
        help="Comma-separated coin universe to analyze in simulation mode.",
    )
    parser.add_argument(
        "--regimes",
        default=",".join(DEFAULT_REGIMES),
        help="Comma-separated simulation regimes to compare.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="How many top recommendations to print per regime.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Simulation seed used for deterministic output.",
    )
    args = parser.parse_args()

    coins = _parse_csv(args.coins)
    regimes = [item.strip().lower()
               for item in args.regimes.split(",") if item.strip()]

    if not coins:
        raise SystemExit("No coins provided.")
    if not regimes:
        raise SystemExit("No regimes provided.")

    print(
        f"Simulation comparison: coins={','.join(coins)} seed={args.seed} top={args.top}")
    for regime in regimes:
        summary = _summarize_regime(
            regime=regime, seed=args.seed, top_n=args.top, coins=coins)
        print(f"\n[{summary['regime']}]")
        print(f"recommendation_mix={summary['mix']}")
        print(
            "entry_controls="
            f"mode={summary['entry_mode']} "
            f"strict={len(summary['strict_buy_candidates'])} "
            f"fallback_allowed={summary['fallback_allowed']} "
            f"fallback_candidates={len(summary['fallback_candidates'])} "
            f"force_fill_allowed={summary['force_fill_allowed']} "
            f"force_fill_candidates={len(summary['force_fill_candidates'])} "
            f"downtrend_reversal_eligible={summary['downtrend_reversal_eligible']}"
        )
        if not summary["top_entries"]:
            print("no analyzed entries")
            continue
        for idx, entry in enumerate(summary["top_entries"], start=1):
            print(
                f"{idx}. {entry['coin']}: {entry['recommendation']} | "
                f"score={entry['score']} | rsi={_format_rsi(entry['rsi'])} | price={entry['price']:.4f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
