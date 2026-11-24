import math


def rule_recommendation(
    rsi: float,
    sma_short: float,
    sma_long: float,
    current_price: float,
) -> tuple[str, int]:
    """Return the rule-only recommendation shared by live analysis and replay."""
    if not all(math.isfinite(value) for value in (rsi, sma_short, sma_long, current_price)):
        return "HOLD", 50

    if rsi < 30 and sma_short > sma_long and current_price > sma_short:
        return "STRONG BUY", 90
    if rsi < 40 and sma_short > sma_long:
        return "BUY", 75
    if rsi > 70 and sma_short < sma_long and current_price < sma_short:
        return "SELL", 10
    if rsi > 60 and sma_short < sma_long:
        return "WEAK SELL", 25
    if sma_short > sma_long:
        return "HOLD (Up-Trend)", 60
    if sma_short < sma_long:
        return "HOLD (Down-Trend)", 40
    return "HOLD", 50
