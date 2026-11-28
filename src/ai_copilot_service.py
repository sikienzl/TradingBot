"""AI co-pilot service for the trading bot.

Extracted from :class:`src.trading_bot.CryptoTradingBot` so the quota gate,
the snapshot builder, the API call, the JSON parsing, the clamping and the
variant orchestration can be tested without constructing the bot. The bodies
are copied verbatim from the bot methods; the only changes are the documented
renames (``self.config`` to ``config``, ``self.performance`` to
``performance``, ``self.consecutive_losses`` to ``consecutive_losses``,
``self.iteration`` to ``iteration``, ``self.portfolio`` to ``portfolio``,
``self._<method>`` to the module-level function and the module logger to an
injected ``logger`` parameter). The bot keeps thin delegators with the
original method names.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from src.state_store import (
    normalize_ai_state,
    read_ai_copilot_state,
    write_ai_copilot_state,
)


def estimate_ai_cost_usd(prompt_tokens: int, completion_tokens: int, input_cost_per_mtok: float, output_cost_per_mtok: float) -> float:
    input_cost = (max(0, prompt_tokens) / 1_000_000.0) * \
        input_cost_per_mtok
    output_cost = (max(0, completion_tokens) / 1_000_000.0) * \
        output_cost_per_mtok
    return float(input_cost + output_cost)


def can_run_ai_copilot(  # noqa: C901
    state: dict,
    *,
    enabled: bool,
    model: str,
    api_key: str,
    max_consecutive_errors: int,
    max_calls_per_day: int,
    max_calls_per_month: int,
    max_budget_usd_per_month: float,
    interval_minutes: int,
    logger: logging.Logger,
) -> tuple[bool, str]:
    if not enabled:
        return False, 'disabled'
    if not model:
        return False, 'missing_model'
    if not api_key:
        return False, 'missing_api_key'
    if (
        max_consecutive_errors > 0
        and state.get('consecutive_errors', 0) >= max_consecutive_errors
        and state.get('last_suspended_at')
    ):
        return False, 'suspended_after_errors'
    if max_calls_per_day > 0 and state.get('daily_calls', 0) >= max_calls_per_day:
        return False, 'daily_call_limit'
    if max_calls_per_month > 0 and state.get('monthly_calls', 0) >= max_calls_per_month:
        return False, 'monthly_call_limit'
    if max_budget_usd_per_month > 0 and state.get('monthly_spend_usd', 0.0) >= max_budget_usd_per_month:
        return False, 'monthly_budget_limit'

    last_run_raw = state.get('last_attempt_at') or state.get('last_run_at')
    if last_run_raw and interval_minutes > 0:
        try:
            last_run = datetime.fromisoformat(last_run_raw)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=UTC)
            elapsed_min = (datetime.now(UTC) -
                           last_run).total_seconds() / 60.0
            if elapsed_min < interval_minutes:
                return False, 'interval_not_reached'
        except (TypeError, ValueError) as exc:
            logger.warning(
                'Ignoring AI co-pilot state with invalid attempt timestamp: %s',
                exc,
            )
            return False, 'invalid_attempt_timestamp'
    return True, 'ok'


def ai_copilot_snapshot(
    config: Any,
    logger: logging.Logger,
    *,
    performance: dict[str, Any],
    consecutive_losses: int,
    iteration: int,
    portfolio: Any,
) -> dict[str, Any]:
    trades_recent = []
    recent_trade_summary: dict[str, Any] = {
        'window_rows': 0,
        'recent_buy_count': 0,
        'recent_sell_count': 0,
        'recent_break_even_count': 0,
        'recent_win_rate_pct': 0.0,
        'recent_avg_pnl_base': 0.0,
        'recent_avg_hold_seconds': 0.0,
        'recent_max_hold_exit_ratio': 0.0,
        'recent_exit_reasons': {},
        'coin_sell_summary': {},
        'top_winners': [],
        'top_losers': [],
    }
    if config.performance_log_enabled and os.path.exists(config.performance_log_file):
        try:
            df = pd.read_csv(config.performance_log_file)
            if not df.empty:
                cols = [
                    'timestamp', 'coin', 'action', 'pnl_base', 'pnl_pct',
                    'signal_source', 'signal_confidence', 'reason', 'hold_seconds'
                ]
                cols = [c for c in cols if c in df.columns]
                recent_window = df.tail(80).copy()
                trades_recent = recent_window.tail(40)[cols].where(
                    pd.notna(recent_window.tail(40)[cols]), None
                ).to_dict(orient='records')

                if 'action' in recent_window.columns:
                    actions = recent_window['action'].fillna(
                        '').astype(str).str.lower()
                    recent_trade_summary['window_rows'] = len(recent_window)
                    recent_trade_summary['recent_buy_count'] = int(
                        (actions == 'buy').sum())
                    sells = recent_window.loc[actions == 'sell'].copy()
                    recent_trade_summary['recent_sell_count'] = len(sells)

                    if not sells.empty:
                        pnl = pd.to_numeric(
                            sells.get('pnl_base'), errors='coerce').fillna(0.0)
                        recent_trade_summary['recent_break_even_count'] = int(
                            (pnl == 0).sum()
                        )
                        recent_trade_summary['recent_win_rate_pct'] = round(
                            float((pnl > 0).mean() * 100.0), 2
                        )
                        recent_trade_summary['recent_avg_pnl_base'] = round(
                            float(pnl.mean()), 6
                        )

                        hold_seconds = pd.to_numeric(
                            sells.get('hold_seconds'), errors='coerce'
                        ).fillna(0.0)
                        recent_trade_summary['recent_avg_hold_seconds'] = round(
                            float(hold_seconds.mean()), 2
                        )

                        reasons = sells.get('reason', pd.Series(
                            dtype=object)).fillna('').astype(str)
                        reason_counts = reasons[reasons !=
                                                ''].value_counts().head(6)
                        recent_trade_summary['recent_exit_reasons'] = {
                            str(reason): int(count) for reason, count in reason_counts.items()
                        }
                        max_hold_exits = int(
                            reasons.str.contains(
                                'MAX-HOLD-TIME', case=False, na=False).sum()
                        )
                        recent_trade_summary['recent_max_hold_exit_ratio'] = round(
                            max_hold_exits / max(len(sells), 1), 4
                        )

                        if 'coin' in sells.columns:
                            coin_summary: dict[str, Any] = {}
                            for coin, group in sells.groupby('coin', dropna=True):
                                coin_pnl = pd.to_numeric(
                                    group.get('pnl_base'), errors='coerce'
                                ).fillna(0.0)
                                coin_reasons = group.get('reason', pd.Series(
                                    dtype=object)).fillna('').astype(str)
                                coin_summary[str(coin)] = {
                                    'sell_count': len(group),
                                    'pnl_sum': round(float(coin_pnl.sum()), 6),
                                    'avg_pnl': round(float(coin_pnl.mean()), 6),
                                    'break_even_count': int((coin_pnl == 0).sum()),
                                    'win_rate_pct': round(float((coin_pnl > 0).mean() * 100.0), 2),
                                    'max_hold_exit_ratio': round(
                                        float(
                                            coin_reasons.str.contains(
                                                'MAX-HOLD-TIME', case=False, na=False
                                            ).mean()
                                        ),
                                        4,
                                    ),
                                }
                            recent_trade_summary['coin_sell_summary'] = coin_summary

                            ordered_coins = sorted(
                                coin_summary.items(),
                                key=lambda item: item[1]['pnl_sum'],
                            )
                            recent_trade_summary['top_losers'] = [
                                {'coin': coin, 'pnl_sum': data['pnl_sum']}
                                for coin, data in ordered_coins[:3]
                            ]
                            recent_trade_summary['top_winners'] = [
                                {'coin': coin, 'pnl_sum': data['pnl_sum']}
                                for coin, data in reversed(ordered_coins[-3:])
                            ]
        except (
            OSError,
            UnicodeError,
            pd.errors.ParserError,
            pd.errors.EmptyDataError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning('Unable to build AI co-pilot snapshot: %s', exc)
            trades_recent = []
            recent_trade_summary = {
                'window_rows': 0,
                'recent_buy_count': 0,
                'recent_sell_count': 0,
                'recent_break_even_count': 0,
                'recent_win_rate_pct': 0.0,
                'recent_avg_pnl_base': 0.0,
                'recent_avg_hold_seconds': 0.0,
                'recent_max_hold_exit_ratio': 0.0,
                'recent_exit_reasons': {},
                'coin_sell_summary': {},
                'top_winners': [],
                'top_losers': [],
            }

    by_source = {}
    for source, data in performance['by_source'].items():
        by_source[source] = {
            'buys': int(data.get('buys', 0)),
            'sells': int(data.get('sells', 0)),
            'pnl': float(data.get('pnl', 0.0)),
        }

    return {
        'timestamp_utc': datetime.now(UTC).isoformat(),
        'iteration': int(iteration),
        'config': {
            'min_entry_score': int(config.min_entry_score),
            'reentry_cooldown_seconds': int(config.reentry_cooldown_seconds),
            'reentry_cooldown_max_seconds': int(config.reentry_cooldown_max_seconds),
            'tabular_buy_min_confidence': float(config.tabular_buy_min_confidence),
            'tabular_min_confidence': float(config.tabular_min_confidence),
            'max_hold_seconds': int(config.max_hold_seconds),
            'fallback_min_score': int(config.fallback_min_score),
        },
        'performance': {
            'buys': int(performance['buys']),
            'sells': int(performance['sells']),
            'wins': int(performance['wins']),
            'losses': int(performance['losses']),
            'break_even': int(performance['break_even']),
            'closed_trades': int(performance['closed_trades']),
            'win_rate_pct': (
                performance['wins'] / performance['closed_trades'] * 100.0
                if performance['closed_trades'] > 0 else 0.0
            ),
            'realized_pnl_base': float(performance['realized_pnl_base']),
            'consecutive_losses': int(consecutive_losses),
            'by_source': by_source,
        },
        'portfolio': {
            'cash': float(portfolio.cash),
            'open_trades_count': len(portfolio.open_trades),
            'open_trade_coins': sorted(portfolio.open_trades.keys()),
            'holding_coins': sorted(portfolio.holdings.keys()),
        },
        'recent_trade_summary': recent_trade_summary,
        'recent_trades': trades_recent,
        'guardrails': {
            'min_entry_score_min': config.ai_copilot_min_entry_score_min,
            'min_entry_score_max': config.ai_copilot_min_entry_score_max,
            'reentry_cooldown_min': config.ai_copilot_reentry_cooldown_min,
            'reentry_cooldown_max': config.ai_copilot_reentry_cooldown_max,
            'tabular_buy_conf_min': config.ai_copilot_tabular_buy_conf_min,
            'tabular_buy_conf_max': config.ai_copilot_tabular_buy_conf_max,
        },
    }


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    start = text.find('{')
    end = text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return None
    return None


def call_ai_copilot(config: Any, snapshot: dict[str, Any], *, model: str, temperature: float, max_output_tokens: int) -> tuple[dict[str, Any] | None, int, int]:
    system_prompt = (
        "You are a conservative trading bot co-pilot. "
        "Return ONLY JSON with keys: proposed_changes (object), reason (string), confidence (0..1), risk_level (low|medium|high). "
        "Allowed proposed_changes keys: min_entry_score, reentry_cooldown_seconds, tabular_buy_min_confidence. "
        "Use the recent_trade_summary, exit-reason mix, churn indicators, and per-coin sell summary to reason from observed behavior instead of generic advice. "
        "Prefer no change when evidence is weak or sample size is small. Respect provided guardrails. Propose at most one parameter change at a time."
    )
    user_payload = json.dumps(snapshot, ensure_ascii=True)
    request_body = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_payload},
        ],
        'temperature': temperature,
        'max_tokens': max_output_tokens,
    }

    req = urllib.request.Request(
        config.ai_copilot_api_url,
        data=json.dumps(request_body).encode('utf-8'),
        headers={
            'Authorization': f"Bearer {config.ai_copilot_api_key}",
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'trading-bot/1.0 (+https://github.com/siegfried/trading_2)',
        },
        method='POST',
    )

    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
    parsed = json.loads(raw)
    usage = parsed.get('usage', {}) if isinstance(parsed, dict) else {}
    prompt_tokens = int(usage.get('prompt_tokens', 0) or 0)
    completion_tokens = int(usage.get('completion_tokens', 0) or 0)

    content = ''
    if isinstance(parsed, dict):
        choices = parsed.get('choices', [])
        if choices and isinstance(choices[0], dict):
            message = choices[0].get('message', {})
            if isinstance(message, dict):
                content = str(message.get('content', '') or '')

    result = extract_json_object(content)
    return result, prompt_tokens, completion_tokens


def clamp_ai_changes(raw_changes: dict[str, Any], config: Any) -> dict[str, Any]:
    if not isinstance(raw_changes, dict):
        return {}
    out: dict[str, Any] = {}

    if 'min_entry_score' in raw_changes:
        try:
            v = round(float(raw_changes['min_entry_score']))
            v = min(max(v, config.ai_copilot_min_entry_score_min),
                    config.ai_copilot_min_entry_score_max)
            out['min_entry_score'] = v
        except (TypeError, ValueError, OverflowError):
            pass

    if 'reentry_cooldown_seconds' in raw_changes:
        try:
            cooldown = round(float(raw_changes['reentry_cooldown_seconds']))
            cooldown = min(max(cooldown,
                               config.ai_copilot_reentry_cooldown_min),
                           config.ai_copilot_reentry_cooldown_max)
            out['reentry_cooldown_seconds'] = cooldown
        except (TypeError, ValueError, OverflowError):
            pass

    if 'tabular_buy_min_confidence' in raw_changes:
        try:
            conf = float(raw_changes['tabular_buy_min_confidence'])
            conf = min(max(conf,
                           config.ai_copilot_tabular_buy_conf_min),
                       config.ai_copilot_tabular_buy_conf_max)
            out['tabular_buy_min_confidence'] = round(conf, 4)
        except (TypeError, ValueError, OverflowError):
            pass

    return out


def apply_ai_changes(changes: dict[str, Any], config: Any) -> dict[str, tuple[Any, Any]]:
    applied: dict[str, tuple[Any, Any]] = {}

    if 'min_entry_score' in changes:
        old_score = config.min_entry_score
        new_score = int(changes['min_entry_score'])
        if old_score != new_score:
            config.min_entry_score = new_score
            applied['min_entry_score'] = (old_score, new_score)

    if 'reentry_cooldown_seconds' in changes:
        old_cooldown = config.reentry_cooldown_seconds
        new_cooldown = int(changes['reentry_cooldown_seconds'])
        if old_cooldown != new_cooldown:
            config.reentry_cooldown_seconds = new_cooldown
            applied['reentry_cooldown_seconds'] = (old_cooldown, new_cooldown)

    if 'tabular_buy_min_confidence' in changes:
        old_conf = config.tabular_buy_min_confidence
        new_conf = float(changes['tabular_buy_min_confidence'])
        if abs(old_conf - new_conf) > 1e-9:
            config.tabular_buy_min_confidence = new_conf
            applied['tabular_buy_min_confidence'] = (old_conf, new_conf)

    return applied


def maybe_run_ai_copilot_variant(
    config: Any,
    logger: logging.Logger,
    snapshot: dict[str, Any],
    *,
    label: str,
    state_file: str,
    enabled: bool,
    model: str,
    shadow_mode: bool,
    max_calls_per_day: int,
    max_calls_per_month: int,
    max_budget_usd_per_month: float,
    max_output_tokens: int,
    temperature: float,
    max_consecutive_errors: int,
    cost_input_per_mtok: float,
    cost_output_per_mtok: float,
    interval_minutes: int,
    allow_apply: bool,
):
    state = normalize_ai_state(
        read_ai_copilot_state(state_file),
        max_calls_per_month,
        max_budget_usd_per_month,
    )
    state['model'] = model
    can_run, reason = can_run_ai_copilot(
        state,
        enabled=enabled,
        model=model,
        api_key=config.ai_copilot_api_key,
        max_consecutive_errors=max_consecutive_errors,
        max_calls_per_day=max_calls_per_day,
        max_calls_per_month=max_calls_per_month,
        max_budget_usd_per_month=max_budget_usd_per_month,
        interval_minutes=interval_minutes,
        logger=logger,
    )
    if not can_run:
        if reason not in {'interval_not_reached', 'disabled'}:
            logger.info(f"{label} skipped: {reason}")
        write_ai_copilot_state(state, state_file)
        return

    try:
        result, prompt_tokens, completion_tokens = call_ai_copilot(
            config,
            snapshot,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        estimated_cost = estimate_ai_cost_usd(
            prompt_tokens, completion_tokens,
            cost_input_per_mtok, cost_output_per_mtok)

        # Final budget check before recording the call.
        projected = state.get('monthly_spend_usd', 0.0) + estimated_cost
        if (
            max_budget_usd_per_month > 0
            and projected > max_budget_usd_per_month
        ):
            logger.warning(
                f"{label} call ignored due to monthly budget cap: projected {projected:.4f} USD")
            state['last_attempt_at'] = datetime.now(
                UTC).isoformat()
            write_ai_copilot_state(state, state_file)
            return

        state['monthly_calls'] = int(state.get('monthly_calls', 0)) + 1
        state['daily_calls'] = int(state.get('daily_calls', 0)) + 1
        state['monthly_spend_usd'] = projected
        state['last_run_at'] = datetime.now(UTC).isoformat()
        state['last_attempt_at'] = state['last_run_at']
        state['last_prompt_tokens'] = prompt_tokens
        state['last_completion_tokens'] = completion_tokens
        state['last_estimated_cost_usd'] = round(estimated_cost, 6)
        state['consecutive_errors'] = 0
        state.pop('last_suspended_at', None)

        if not result:
            logger.warning(f'{label} returned no valid JSON payload')
            write_ai_copilot_state(state, state_file)
            return

        changes = clamp_ai_changes(
            result.get('proposed_changes', {}), config)
        reason_text = str(result.get('reason', ''))
        risk_level = str(result.get('risk_level', 'unknown'))
        confidence = result.get('confidence', None)

        if not changes:
            state['last_suggestion'] = {
                'changes': {},
                'risk_level': risk_level,
                'confidence': confidence,
                'reason': reason_text,
                'mode': 'no_change',
                'model': model,
            }
            logger.info(
                f"{label} suggestion: no change (model={model}, risk={risk_level}, confidence={confidence}, reason={reason_text})")
            write_ai_copilot_state(state, state_file)
            return

        # Keep adaptation conservative: apply at most one parameter per run.
        first_key = next(iter(changes.keys()))
        single_change = {first_key: changes[first_key]}
        state['last_suggestion'] = {
            'changes': single_change,
            'risk_level': risk_level,
            'confidence': confidence,
            'reason': reason_text,
            'mode': 'shadow' if shadow_mode or not allow_apply else 'applied',
            'model': model,
        }

        if shadow_mode or not allow_apply:
            logger.info(
                f"{label} shadow suggestion: {single_change} "
                f"(model={model}, risk={risk_level}, confidence={confidence}, reason={reason_text})")
        else:
            applied = apply_ai_changes(single_change, config)
            if applied:
                logger.info(
                    f"{label} applied: {applied} "
                    f"(model={model}, risk={risk_level}, confidence={confidence}, reason={reason_text})")
                state['last_applied_at'] = datetime.now(
                    UTC).isoformat()
                state['last_applied_changes'] = {
                    k: {'old': v[0], 'new': v[1]} for k, v in applied.items()
                }
            else:
                logger.info(
                    f"{label} suggested unchanged values: {single_change}")

        write_ai_copilot_state(state, state_file)

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as e:
        state['last_attempt_at'] = datetime.now(UTC).isoformat()
        state['consecutive_errors'] = int(
            state.get('consecutive_errors', 0)) + 1
        state['last_error'] = str(e)
        state['last_error_at'] = state['last_attempt_at']
        logger.warning(f"{label} call failed: {e}")
        if (
            max_consecutive_errors > 0
            and state['consecutive_errors'] >= max_consecutive_errors
        ):
            logger.error(
                f"{label} suspended after {state['consecutive_errors']} consecutive errors")
            state['last_suspended_at'] = datetime.now(
                UTC).isoformat()
        write_ai_copilot_state(state, state_file)
