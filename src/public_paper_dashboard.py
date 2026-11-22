"""Read-only, authenticated dashboard for an isolated paper-trading run."""

from __future__ import annotations

import csv
import hmac
import io
import ipaddress
import json
import logging
import math
import os
import re
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("public_paper_dashboard")
security = HTTPBasic(auto_error=False)

MARKET_TICKER_FIELDS = (
    "observed_at_utc",
    "exchange_timestamp_utc",
    "symbol",
    "bid",
    "ask",
    "last",
    "base_volume",
    "quote_volume",
)
MARKET_CANDLE_FIELDS = (
    "timestamp_utc",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
MARKET_DATA_READ_LIMIT = 16 * 1024 * 1024
MARKET_DATA_WINDOW = timedelta(hours=24)


class DashboardConfigurationError(RuntimeError):
    """Raised when the private dashboard configuration is incomplete."""


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise DashboardConfigurationError(f"{name} must be configured")
    return value


def _allowed_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = _required_env("DASHBOARD_ALLOWED_NETWORKS")
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    try:
        for item in raw.split(","):
            network = ipaddress.ip_network(item.strip(), strict=False)
            if not (network.is_private or network.is_loopback):
                raise DashboardConfigurationError(
                    "DASHBOARD_ALLOWED_NETWORKS only accepts private or loopback CIDRs"
                )
            networks.append(network)
    except ValueError as exc:
        raise DashboardConfigurationError(
            "DASHBOARD_ALLOWED_NETWORKS must be a comma-separated CIDR list"
        ) from exc
    if not networks:
        raise DashboardConfigurationError("DASHBOARD_ALLOWED_NETWORKS is empty")
    return tuple(networks)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.error("Unable to read dashboard %s: %s", label, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Paper {label} is temporarily unavailable.",
        ) from exc
    if not isinstance(value, dict):
        logger.error("Dashboard %s has an invalid JSON shape", label)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Paper {label} is temporarily unavailable.",
        )
    return value


def _load_journal(path: Path) -> list[dict[str, str]]:
    try:
        if path.stat().st_size > 100 * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Paper journal exceeds the dashboard read limit.",
            )
        with path.open(encoding="utf-8", newline="") as journal:
            reader = csv.DictReader(journal)
            if not {"timestamp", "action"}.issubset(reader.fieldnames or []):
                raise ValueError("journal is missing required columns")
            return [row for row in reader if row.get("timestamp") and row.get("action")]
    except HTTPException:
        raise
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        logger.error("Unable to read paper journal: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Paper journal is temporarily unavailable.",
        ) from exc


def _net_value(rows: list[dict[str, str]]) -> float | None:
    sells = [row for row in rows if row.get("action", "").lower() == "sell"]
    if not sells:
        return 0.0
    values = [
        _number(row.get("net_pnl_base"))
        if row.get("execution_cost_mode") == "net_costs"
        and row.get("entry_cost_mode") == "net_costs"
        else None
        for row in sells
    ]
    if any(value is None for value in values):
        return None
    return round(sum(value for value in values if value is not None), 8)


def _field_total(rows: list[dict[str, str]], field: str) -> float | None:
    if not rows:
        return 0.0
    values = [_number(row.get(field)) for row in rows]
    if any(value is None for value in values):
        return None
    return round(sum(value for value in values if value is not None), 8)


def _read_csv_tail(
    path: Path, fields: tuple[str, ...]
) -> tuple[Iterator[dict[str, str]], bool]:
    with path.open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        header = source.readline(4096).decode("utf-8")
        if tuple(next(csv.reader([header]), [])) != fields:
            raise ValueError(f"{path.name} has an unexpected CSV header")
        if size > MARKET_DATA_READ_LIMIT:
            source.seek(-MARKET_DATA_READ_LIMIT, os.SEEK_END)
            content = source.read(MARKET_DATA_READ_LIMIT)
            first_newline = content.find(b"\n")
            if first_newline < 0:
                raise ValueError(f"{path.name} has no complete rows in its read window")
            content = content[first_newline + 1 :]
            truncated = True
        else:
            source.seek(0)
            content = source.read(MARKET_DATA_READ_LIMIT)
            truncated = False
    if content and not content.endswith(b"\n"):
        last_newline = content.rfind(b"\n")
        content = content[: last_newline + 1] if last_newline >= 0 else b""
    text = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) == fields:
        return reader, truncated
    if truncated:
        reader = csv.DictReader(io.StringIO(",".join(fields) + "\n" + text, newline=""))
        return reader, True
    raise ValueError(f"{path.name} has an unexpected CSV header")


def _market_unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "window_hours": int(MARKET_DATA_WINDOW.total_seconds() // 3600),
        "symbols": [],
        "markets": [],
        "selected_symbol": None,
        "ticker_series": [],
        "candle_series": [],
        "read_window_truncated": False,
    }


def load_market_observations(selected_symbol: str | None = None) -> dict[str, Any]:
    market_dir = Path(os.getenv("DASHBOARD_MARKET_DATA_DIR", "/market-data"))
    metadata_path = market_dir / "metadata.json"
    ticker_path = market_dir / "tickers.csv"
    candle_path = market_dir / "candles_1m.csv"
    if not market_dir.is_dir():
        return _market_unavailable("Market recorder data directory is not mounted.")
    if not all(path.is_file() for path in (metadata_path, ticker_path, candle_path)):
        return _market_unavailable("Market recorder data files are not available yet.")
    try:
        if metadata_path.stat().st_size > 1024 * 1024:
            raise ValueError("market recorder metadata exceeds the read limit")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise TypeError("market recorder metadata has an invalid JSON shape")
        symbols = metadata.get("symbols")
        if (
            metadata.get("exchange") != "kraken"
            or not isinstance(symbols, list)
            or not symbols
            or any(
                not isinstance(symbol, str)
                or not re.fullmatch(r"[A-Z0-9]+/EUR", symbol)
                for symbol in symbols
            )
        ):
            raise ValueError(
                "market recorder metadata has an invalid exchange or symbol list"
            )
        symbols = list(dict.fromkeys(symbols))
        if selected_symbol is not None and selected_symbol not in symbols:
            raise HTTPException(status_code=404, detail="Market is not recorded.")
        tickers, ticker_truncated = _read_csv_tail(ticker_path, MARKET_TICKER_FIELDS)
        candles, candle_truncated = _read_csv_tail(candle_path, MARKET_CANDLE_FIELDS)
    except HTTPException:
        raise
    except (OSError, UnicodeError, csv.Error, TypeError, ValueError) as exc:
        logger.error("Unable to read public market observations: %s", exc)
        return _market_unavailable(
            "Market recorder data is invalid or temporarily unavailable."
        )

    now = datetime.now(UTC)
    window_start = now - MARKET_DATA_WINDOW

    try:
        all_tickers_by_symbol, invalid_tickers = _valid_rows(
            tickers, "observed_at_utc", symbols, window_start, now)
        del tickers
        all_candles_by_symbol, invalid_candles = _valid_rows(
            candles, "timestamp_utc", symbols, window_start, now)
        del candles
    except csv.Error as exc:
        logger.error("Unable to parse public market observations: %s", exc)
        return _market_unavailable(
            "Market recorder data is invalid or temporarily unavailable."
        )
    ticker_by_symbol = {
        symbol: [entry for entry in entries if entry[0] >= window_start]
        for symbol, entries in all_tickers_by_symbol.items()
    }
    candle_by_symbol = {
        symbol: [entry for entry in entries if entry[0] >= window_start]
        for symbol, entries in all_candles_by_symbol.items()
    }
    summaries = [
        _build_market_summary(
            symbol, ticker_by_symbol, candle_by_symbol,
            all_tickers_by_symbol, all_candles_by_symbol)
        for symbol in symbols
    ]

    selected = selected_symbol or symbols[0]
    ticker_series = [
        {
            "timestamp": timestamp.isoformat(),
            "bid": _number(row.get("bid")),
            "ask": _number(row.get("ask")),
            "last": _number(row.get("last")),
        }
        for timestamp, row in ticker_by_symbol.get(selected, [])
    ]
    candle_series = [
        {
            "timestamp": timestamp.isoformat(),
            "close": _number(row.get("close")),
        }
        for timestamp, row in candle_by_symbol.get(selected, [])
    ]

    ticker_stat = ticker_path.stat()
    candle_stat = candle_path.stat()
    newest_timestamps = [
        timestamp
        for market in summaries
        for timestamp in (
            _parse_utc(market["latest_observed_at"]),
            _parse_utc(market["latest_candle_at"]),
        )
        if timestamp is not None
    ]
    newest = max(newest_timestamps, default=None)
    return {
        "available": True,
        "reason": None,
        "generated_at": now.isoformat(),
        "window_hours": int(MARKET_DATA_WINDOW.total_seconds() // 3600),
        "symbols": symbols,
        "selected_symbol": selected,
        "markets": summaries,
        "ticker_series": _downsample(ticker_series, 300),
        "candle_series": _downsample(candle_series, 360),
        "latest_data_at": newest.isoformat() if newest else None,
        "latest_data_age_seconds": round((now - newest).total_seconds(), 1)
        if newest
        else None,
        "freshness_status": (
            "unavailable"
            if newest is None
            else "stale"
            if (now - newest).total_seconds() > 60
            else "fresh"
        ),
        "ticker_file_bytes": ticker_stat.st_size,
        "candle_file_bytes": candle_stat.st_size,
        "read_window_truncated": ticker_truncated or candle_truncated,
        "invalid_ticker_rows": invalid_tickers,
        "invalid_candle_rows": invalid_candles,
        "metadata": {
            "created_at_utc": metadata.get("created_at_utc"),
            "ticker_interval_seconds": metadata.get("ticker_interval_seconds"),
            "candle_timeframe": metadata.get("candle_timeframe"),
            "candle_backfill_limit": metadata.get("candle_backfill_limit"),
        },
        "limitations": [
            "Public quotes and candles are observations, not executable fills.",
            "Ticker sampling does not establish intrainterval price paths or order-book depth.",
            "The 24-hour price change uses available first and last ticker observations.",
        ],
    }


def _valid_rows(
    rows: Iterator[dict[str, str]],
    timestamp_field: str,
    symbols: list[str],
    window_start: datetime,
    now: datetime,
) -> tuple[dict[str, list[tuple[datetime, dict[str, str]]]], int]:
    """Group valid ticker/candle rows by symbol within the data window."""
    grouped: dict[str, list[tuple[datetime, dict[str, str]]]] = defaultdict(list)
    older_latest: dict[str, tuple[datetime, dict[str, str]]] = {}
    invalid = 0
    for row in rows:
        symbol = row.get("symbol", "")
        timestamp = _parse_utc(row.get(timestamp_field))
        if symbol not in symbols or timestamp is None:
            invalid += 1
            continue
        numeric_fields = (
            ("bid", "ask", "last")
            if timestamp_field == "observed_at_utc"
            else ("open", "high", "low", "close", "volume")
        )
        values = [_number(row.get(field)) for field in numeric_fields]
        if any(value is None for value in values):
            invalid += 1
            continue
        if timestamp_field == "observed_at_utc":
            bid, ask = values[:2]
            if bid <= 0 or ask < bid or values[2] <= 0:
                invalid += 1
                continue
        else:
            open_price, high, low, close, volume = values
            if min(open_price, high, low, close) <= 0 or volume < 0 or high < low:
                invalid += 1
                continue
        if window_start <= timestamp <= now:
            grouped[symbol].append((timestamp, row))
        elif timestamp < window_start and (
            symbol not in older_latest or timestamp > older_latest[symbol][0]
        ):
            older_latest[symbol] = (timestamp, row)
    for symbol, entry in older_latest.items():
        if not grouped[symbol]:
            grouped[symbol].append(entry)
    for entries in grouped.values():
        entries.sort(key=lambda item: item[0])
    return grouped, invalid


def _build_market_summary(
    symbol: str,
    ticker_by_symbol: dict[str, list[tuple[datetime, dict[str, str]]]],
    candle_by_symbol: dict[str, list[tuple[datetime, dict[str, str]]]],
    all_tickers_by_symbol: dict[str, list[tuple[datetime, dict[str, str]]]],
    all_candles_by_symbol: dict[str, list[tuple[datetime, dict[str, str]]]],
) -> dict[str, Any]:
    """Compute the per-symbol market quality summary row."""
    ticker_rows = ticker_by_symbol.get(symbol, [])
    candle_rows = candle_by_symbol.get(symbol, [])
    all_ticker_rows = all_tickers_by_symbol.get(symbol, [])
    all_candle_rows = all_candles_by_symbol.get(symbol, [])
    first_ticker = ticker_rows[0] if ticker_rows else None
    last_ticker = all_ticker_rows[-1] if all_ticker_rows else None
    latest_candle = all_candle_rows[-1] if all_candle_rows else None
    bid = _number(last_ticker[1].get("bid")) if last_ticker else None
    ask = _number(last_ticker[1].get("ask")) if last_ticker else None
    last = _number(last_ticker[1].get("last")) if last_ticker else None
    spread_bps = (
        round((ask - bid) / ((ask + bid) / 2) * 10_000, 2)
        if bid is not None and ask is not None and ask >= bid and ask + bid > 0
        else None
    )
    first_price = _number(first_ticker[1].get("last")) if first_ticker else None
    change_pct = (
        round((last / first_price - 1) * 100, 4)
        if last is not None and first_price is not None and first_price > 0
        else None
    )
    spreads = []
    for _, row in ticker_rows:
        row_bid = _number(row.get("bid"))
        row_ask = _number(row.get("ask"))
        if (
            row_bid is not None
            and row_ask is not None
            and row_ask >= row_bid
            and row_ask + row_bid > 0
        ):
            spreads.append((row_ask - row_bid) / ((row_ask + row_bid) / 2) * 10_000)
    candle_times = [timestamp for timestamp, _ in candle_rows]
    gap_count = 0
    max_gap_minutes = 0
    for earlier, later in pairwise(candle_times):
        difference_minutes = int((later - earlier).total_seconds() // 60)
        if difference_minutes > 1:
            gap_count += difference_minutes - 1
            max_gap_minutes = max(max_gap_minutes, difference_minutes - 1)
    ticker_span_seconds = (
        (ticker_rows[-1][0] - ticker_rows[0][0]).total_seconds()
        if len(ticker_rows) > 1
        else 0
    )
    expected_candles = (
        int((candle_times[-1] - candle_times[0]).total_seconds() // 60) + 1
        if len(candle_times) > 1
        else len(candle_times)
    )
    return (
        {
            "symbol": symbol,
            "latest_observed_at": last_ticker[0].isoformat()
            if last_ticker
            else None,
            "ticker_samples_24h": len(ticker_rows),
            "ticker_samples_per_hour": round(len(ticker_rows) / 24, 1),
            "ticker_span_hours": round(ticker_span_seconds / 3600, 2),
            "latest_bid": bid,
            "latest_ask": ask,
            "latest_last": last,
            "latest_spread_bps": spread_bps,
            "average_spread_bps": round(sum(spreads) / len(spreads), 2)
            if spreads
            else None,
            "price_change_pct_observed": change_pct,
            "first_ticker_at": first_ticker[0].isoformat()
            if first_ticker
            else None,
            "candle_count_24h": len(candle_rows),
            "expected_candles_in_observed_span": expected_candles,
            "candle_coverage_pct": (
                round(len(candle_rows) / expected_candles * 100, 2)
                if expected_candles
                else None
            ),
            "candle_gap_minutes": gap_count,
            "largest_candle_gap_minutes": max_gap_minutes,
            "latest_candle_at": latest_candle[0].isoformat()
            if latest_candle
            else None,
        }
    )


def _downsample(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Evenly thin a point series down to at most *limit* points."""
    if len(points) <= limit:
        return points
    stride = (len(points) - 1) / (limit - 1)
    return [points[round(index * stride)] for index in range(limit)]


def _read_bot_log(
    path: Path, start: datetime
) -> tuple[dict[str, int | None], dict[str, str | int] | None]:
    counts: Counter[str] = Counter()
    last_cycle: tuple[datetime, int] | None = None
    try:
        if not path.exists():
            return {
                "atr_guard": None,
                "tabular_sell_veto": None,
                "hailo_failures": None,
            }, None
        if path.stat().st_size > 100 * 1024 * 1024:
            raise OSError("log file exceeds the dashboard read limit")
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            cycle_match = re.search(
                r"Iteration\s+(\d+)\s+-\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
                line,
            )
            if cycle_match:
                try:
                    cycle_time = datetime.strptime(
                        cycle_match.group(2), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=UTC)
                except ValueError:
                    pass
                else:
                    cycle = (cycle_time, int(cycle_match.group(1)))
                    if last_cycle is None or cycle > last_cycle:
                        last_cycle = cycle

            timestamp_text = line[:23]
            try:
                timestamp = datetime.strptime(
                    timestamp_text, "%Y-%m-%d %H:%M:%S,%f"
                ).replace(tzinfo=UTC)
            except ValueError:
                continue
            if timestamp < start:
                continue
            if "ATR(14)/price volatility proxy" in line and "Entry withheld" in line:
                counts["atr_guard"] += 1
            if "Confident tabular SELL vetoed new entry" in line:
                counts["tabular_sell_veto"] += 1
            if "Hailo prediction for" in line and "failed; suppressing" in line:
                counts["hailo_failures"] += 1
    except (OSError, UnicodeError) as exc:
        logger.error("Unable to read paper bot log: %s", exc)
        return {
            "atr_guard": None,
            "tabular_sell_veto": None,
            "hailo_failures": None,
        }, None
    log_counts = {
        "atr_guard": counts["atr_guard"],
        "tabular_sell_veto": counts["tabular_sell_veto"],
        "hailo_failures": counts["hailo_failures"],
    }
    cycle_data = (
        {"timestamp": last_cycle[0].isoformat(), "iteration": last_cycle[1]}
        if last_cycle
        else None
    )
    return log_counts, cycle_data


def load_dashboard_data() -> dict[str, Any]:
    data_dir = Path(os.getenv("DASHBOARD_DATA_DIR", "/data"))
    report_file = Path(os.getenv("DASHBOARD_REPORT_FILE", "/reports/latest.json"))
    journal_path = data_dir / "trade_journal.csv"
    state_path = data_dir / ".portfolio_state.json"
    log_path = Path(os.getenv("DASHBOARD_LOG_FILE", str(data_dir / "trading_bot.log")))

    state = _load_json(state_path, "portfolio state")
    rows = _load_journal(journal_path)
    now = datetime.now(UTC)
    today = now.date()
    since_7d = now - timedelta(days=7)
    parsed_rows: list[tuple[datetime, dict[str, str]]] = []
    for row in rows:
        timestamp = _parse_utc(row.get("timestamp"))
        if timestamp:
            parsed_rows.append((timestamp, row))
    parsed_rows.sort(key=lambda pair: pair[0])

    today_rows = [row for stamp, row in parsed_rows if stamp.date() == today]
    week_rows = [row for stamp, row in parsed_rows if stamp >= since_7d]
    all_sells = [row for row in rows if row.get("action", "").lower() == "sell"]
    today_sells = [row for row in today_rows if row.get("action", "").lower() == "sell"]
    week_sells = [row for row in week_rows if row.get("action", "").lower() == "sell"]
    daily: dict[date, list[dict[str, str]]] = defaultdict(list)
    for stamp, row in parsed_rows:
        if stamp >= now - timedelta(days=13):
            daily[stamp.date()].append(row)
    chart = [
        {
            "date": (today - timedelta(days=13 - offset)).isoformat(),
            "net_pnl": _net_value(daily.get(today - timedelta(days=13 - offset), [])),
        }
        for offset in range(14)
    ]

    open_trades = state.get("open_trades")
    if not isinstance(open_trades, dict):
        open_trades = {}
    positions = []
    for coin, trade in open_trades.items():
        if not isinstance(trade, dict):
            continue
        positions.append(
            {
                "coin": str(coin),
                "amount_coin": _number(trade.get("amount_coin")),
                "amount_base": _number(trade.get("amount_base")),
                "buy_price": _number(trade.get("buy_price")),
                "timestamp": str(trade.get("timestamp") or ""),
            }
        )

    holdings = state.get("holdings")
    if not isinstance(holdings, dict):
        holdings = {}
    safe_holdings = [
        {"coin": str(coin), "amount": amount}
        for coin, value in holdings.items()
        if (amount := _number(value)) is not None
    ]

    report: dict[str, Any] | None = None
    try:
        if report_file.exists():
            candidate = json.loads(report_file.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                report = candidate
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Unable to read optional daily report: %s", exc)

    last_event = parsed_rows[-1][0].isoformat() if parsed_rows else None
    log_counts, last_bot_cycle = _read_bot_log(
        log_path, datetime.combine(today, datetime.min.time(), UTC)
    )
    state_timestamp = str(state.get("timestamp") or "")
    recent = []
    for stamp, row in reversed(parsed_rows[-20:]):
        recent.append(
            {
                "timestamp": stamp.isoformat(),
                "coin": str(row.get("coin") or ""),
                "action": row.get("action", "").lower(),
                "amount_base": _number(row.get("amount_base")),
                "net_pnl_base": (
                    _number(row.get("net_pnl_base"))
                    if row.get("execution_cost_mode") == "net_costs"
                    and row.get("entry_cost_mode") == "net_costs"
                    else None
                ),
            }
        )

    currency = state.get("base_currency")
    if not isinstance(currency, str) or not currency.strip():
        currency = "—"
    return {
        "generated_at": now.isoformat(),
        "state_timestamp": state_timestamp,
        "last_journal_event": last_event,
        "last_bot_cycle": last_bot_cycle,
        "portfolio": {
            "base_currency": currency,
            "cash": _number(state.get("cash")),
            "initial_value": _number(state.get("initial_portfolio_value")),
            "iteration": state.get("simulation_iteration"),
            "holdings": safe_holdings,
            "positions": positions,
        },
        "activity": {
            "today_buys": sum(
                row.get("action", "").lower() == "buy" for row in today_rows
            ),
            "today_sell_events": len(today_sells),
            "today_net_pnl": _net_value(today_rows),
            "today_fees": _field_total(today_rows, "fee_base"),
            "today_slippage": _field_total(today_rows, "slippage_base"),
            "week_sell_events": len(week_sells),
            "week_net_pnl": _net_value(week_rows),
            "all_sell_events": len(all_sells),
        },
        "filters": log_counts,
        "daily_net_pnl": chart,
        "recent_events": recent,
        "daily_report": report,
    }


async def require_dashboard_access(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
) -> str:
    client_host = request.client.host if request.client else None
    try:
        client_ip = ipaddress.ip_address(client_host or "")
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied.") from None

    networks = _allowed_networks()
    if not any(client_ip in network for network in networks):
        raise HTTPException(status_code=403, detail="Access denied.")
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Basic"},
        )

    expected_user = _required_env("DASHBOARD_USERNAME").encode("utf-8")
    expected_password = _required_env("DASHBOARD_PASSWORD").encode("utf-8")
    supplied_user = credentials.username.encode("utf-8")
    supplied_password = credentials.password.encode("utf-8")
    user_ok = hmac.compare_digest(supplied_user, expected_user)
    password_ok = hmac.compare_digest(supplied_password, expected_password)
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.scheme != "https":
            response = JSONResponse(
                status_code=status.HTTP_426_UPGRADE_REQUIRED,
                content={"detail": "HTTPS is required."},
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'"
        )
        return response


@asynccontextmanager
async def lifespan(_: FastAPI):
    _allowed_networks()
    _required_env("DASHBOARD_USERNAME")
    _required_env("DASHBOARD_PASSWORD")
    yield


app = FastAPI(
    title="Paper Trading Dashboard",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(SecurityHeadersMiddleware)

_snapshot_lock = threading.Lock()
_snapshot_cache: (
    tuple[tuple[tuple[int, int] | None, ...], float, dict[str, Any]] | None
) = None


def _snapshot_signature() -> tuple[tuple[int, int] | None, ...]:
    data_dir = Path(os.getenv("DASHBOARD_DATA_DIR", "/data"))
    report_file = Path(os.getenv("DASHBOARD_REPORT_FILE", "/reports/latest.json"))
    log_file = Path(os.getenv("DASHBOARD_LOG_FILE", str(data_dir / "trading_bot.log")))
    signature = []
    for path in (
        data_dir / ".portfolio_state.json",
        data_dir / "trade_journal.csv",
        log_file,
        report_file,
    ):
        try:
            stat = path.stat()
            signature.append((stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append(None)
    return tuple(signature)


@app.get("/", response_class=HTMLResponse)
async def dashboard(_: Annotated[str, Depends(require_dashboard_access)]) -> str:
    return DASHBOARD_HTML


@app.get("/api/dashboard")
async def dashboard_data(
    _: Annotated[str, Depends(require_dashboard_access)],
) -> dict[str, Any]:
    global _snapshot_cache
    with _snapshot_lock:
        signature = _snapshot_signature()
        now = time.monotonic()
        if _snapshot_cache is not None:
            cached_signature, cached_at, cached_data = _snapshot_cache
            if signature == cached_signature and now - cached_at < 5:
                return cached_data
        data = load_dashboard_data()
        _snapshot_cache = (signature, now, data)
        return data


@app.get("/api/market-observations")
async def market_observations(
    _: Annotated[str, Depends(require_dashboard_access)],
    symbol: str | None = None,
) -> dict[str, Any]:
    return load_market_observations(symbol)


def main() -> None:
    host = os.getenv("DASHBOARD_BIND_HOST", "127.0.0.1").strip()
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise DashboardConfigurationError(
            "DASHBOARD_BIND_HOST must be an IP address"
        ) from exc
    port = int(os.getenv("DASHBOARD_PORT", "8443"))
    certfile = _required_env("DASHBOARD_TLS_CERT")
    keyfile = _required_env("DASHBOARD_TLS_KEY")
    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        access_log=False,
    )


DASHBOARD_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Paper Trading</title>
  <style>
    :root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#e8edf5;background:#0b1220}
    *{box-sizing:border-box}body{margin:0 auto;padding:24px;max-width:1280px}
    header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:20px}
    .header-tools{display:flex;align-items:center;gap:16px}
    .clock{min-width:82px;color:#e8edf5;font-size:1.05rem;font-variant-numeric:tabular-nums;font-weight:650;letter-spacing:.04em}
    .refresh-countdown{color:#90a0b7;font-size:.82rem;font-variant-numeric:tabular-nums;white-space:nowrap}
    .language-switch{display:flex;gap:4px;padding:3px;border:1px solid #26354d;border-radius:8px}
    .language-switch button{border:0;border-radius:5px;padding:5px 8px;color:#90a0b7;background:transparent;font:inherit;font-size:.78rem;cursor:pointer}
    .language-switch button[aria-pressed="true"]{color:#e8edf5;background:#26354d}
    h1{font-size:1.55rem;margin:0}h2{font-size:1rem;margin:0 0 14px;color:#c5d2e5}
    .status{color:#8ce0b1;font-size:.88rem}.muted{color:#90a0b7;font-size:.82rem}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px;margin-bottom:16px}
    .card,section{background:#121d30;border:1px solid #26354d;border-radius:12px;padding:16px}
    .label{font-size:.78rem;color:#91a2ba}.value{font-size:1.42rem;font-weight:650;margin-top:8px;overflow-wrap:anywhere}
    .positive{color:#72db9d}.negative{color:#ff8585}.neutral{color:#e8edf5}
    .columns{display:grid;grid-template-columns:1.3fr 1fr;gap:14px;margin-bottom:14px}
    .chart{width:100%;height:220px}.chart text{fill:#90a0b7;font-size:11px}
    .market-section{margin:14px 0}.market-controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:12px 0}
    select{background:#1b2b42;color:#e8edf5;border:1px solid #52647e;border-radius:6px;padding:8px;font:inherit}
    .market-chart{width:100%;height:280px}.market-chart text{fill:#90a0b7;font-size:11px}
    .legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px}.legend span{font-size:.8rem;color:#c5d2e5}
    .legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
    .notice{border-left:3px solid #e8b65c;padding:8px 12px;color:#c5d2e5;font-size:.82rem;margin:10px 0}
    table{width:100%;border-collapse:collapse;font-size:.86rem}th,td{text-align:left;padding:9px 6px;border-bottom:1px solid #26354d}
    th{color:#91a2ba;font-weight:500}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
    .table-wrap{overflow:auto}.pills{display:flex;gap:8px;flex-wrap:wrap}
    .pill{background:#1b2b42;border-radius:999px;padding:7px 10px;font-size:.82rem}
    @media(max-width:720px){body{padding:14px}.columns{grid-template-columns:1fr}header{align-items:flex-start;flex-direction:column}.header-tools{gap:9px;flex-wrap:wrap}td,th{white-space:nowrap}}
  </style>
</head>
<body>
  <header><div><h1>Paper Trading</h1><div class="muted" data-i18n="readonly">Nur Anzeige · keine Order-Steuerung</div></div><div class="header-tools"><div id="updated" class="status">Aktualisiert: —</div><span id="refresh-countdown" class="refresh-countdown" role="timer" aria-live="off"></span><time id="local-clock" class="clock" aria-label="Lokale Uhrzeit">--:--:--</time><div class="language-switch" role="group" aria-label="Sprache"><button id="language-de" type="button" aria-pressed="true">DE</button><button id="language-en" type="button" aria-pressed="false">EN</button></div></div></header>
  <div id="metrics" class="grid"></div>
  <div class="columns">
    <section><h2 data-i18n="dailyPnl">Netto-PnL je UTC-Tag</h2><svg id="chart" class="chart" viewBox="0 0 700 220" role="img" aria-label="Netto-PnL der letzten 14 Tage"></svg></section>
    <section><h2 data-i18n="openPositions">Offene Positionen</h2><div id="positions" class="table-wrap"></div><h2 style="margin-top:20px" data-i18n="filtersFeed">Filter und Feed</h2><div id="filters" class="pills"></div></section>
  </div>
  <section><h2 data-i18n="recentEvents">Letzte Paper-Ereignisse</h2><div id="events" class="table-wrap"></div></section>
  <section class="market-section">
    <h2 data-i18n="marketTitle">Öffentliche Marktdaten · Kraken</h2>
    <div class="muted" data-i18n="marketSubtitle">Read-only Beobachtung · letzte 24 Stunden, soweit aufgezeichnet</div>
    <div class="market-controls"><label for="market-symbol" data-i18n="marketSelect">Markt</label><select id="market-symbol" aria-label="Markt"></select><span id="market-status" class="muted"></span></div>
    <div id="market-metrics" class="grid"></div>
    <div class="notice" data-i18n="marketNotice">Quotes und Kerzen sind öffentliche Beobachtungen, keine tatsächlichen Ausführungen. Spread, Kerzen und Kursänderungen belegen weder Orderbuchtiefe noch Profitabilität oder erzielbare Fills.</div>
    <svg id="market-chart" class="market-chart" viewBox="0 0 900 280" role="img" aria-label="Öffentliche Marktbeobachtungen"></svg>
    <div class="legend"><span><i style="background:#72db9d"></i><span data-i18n="tickerLast">Ticker: letzter Kurs</span></span><span><i style="background:#8daeff"></i><span data-i18n="candleClose">1-Minuten-Kerzenschluss</span></span><span><i style="background:#ffbf69"></i><span data-i18n="bidLine">Bid</span></span><span><i style="background:#f6df82"></i><span data-i18n="askLine">Ask</span></span></div>
    <h2 style="margin-top:20px" data-i18n="marketTableTitle">Marktübersicht</h2><div id="market-table" class="table-wrap"></div>
    <div id="market-footnote" class="muted"></div>
  </section>
  <script>
    const REFRESH_INTERVAL_MS=15000;
    const translations={
      de:{readonly:'Nur Anzeige · keine Order-Steuerung',dailyPnl:'Netto-PnL je UTC-Tag',openPositions:'Offene Positionen',filtersFeed:'Filter und Feed',recentEvents:'Letzte Paper-Ereignisse',cash:'Verfügbares Cash',initialValue:'Startwert',todayPnl:'Netto-PnL heute',weekPnl:'Netto-PnL · 7 Tage',weekSells:'Verkaufsereignisse · 7 Tage',fees:'Gebühren heute',slippage:'Slippage heute',iteration:'Iteration',lastCycle:'Letzter Bot-Zyklus',asset:'Asset',amount:'Menge',investment:'Einsatz',entry:'Einstieg',noneOpen:'Keine offenen Trades',atrFilter:'ATR-Filter',sellVeto:'Hailo-SELL-Veto',hailoErrors:'Hailo-Fehler',holdings:'Holdings',lastEvent:'Letztes Journal-Ereignis',timeLocal:'Zeit (lokal)',action:'Aktion',netPnl:'Netto-PnL',noEvents:'Noch keine Journal-Ereignisse',pnlUnavailable:'Netto-PnL nicht verfügbar',updated:'Aktualisiert',portfolioAt:'Portfolio-Stand',unavailable:'nicht verfügbar',refreshIn:'Aktualisierung in',seconds:'s',dashboardUnavailable:'Dashboard-Daten nicht erreichbar',buy:'Kauf',sell:'Verkauf',language:'Sprache',localTime:'Lokale Uhrzeit',marketTitle:'Öffentliche Marktdaten · Kraken',marketSubtitle:'Read-only Beobachtung · letzte 24 Stunden, soweit aufgezeichnet',marketSelect:'Markt',marketNotice:'Quotes und Kerzen sind öffentliche Beobachtungen, keine tatsächlichen Ausführungen. Spread, Kerzen und Kursänderungen belegen weder Orderbuchtiefe noch Profitabilität oder erzielbare Fills.',tickerLast:'Ticker: letzter Kurs',candleClose:'1-Minuten-Kerzenschluss',bidLine:'Bid',askLine:'Ask',marketTableTitle:'Marktübersicht',latest:'Letzter Kurs',bidAsk:'Bid / Ask',spreadBps:'Spread (bps)',change:'Änderung im Beobachtungszeitraum',samples:'Ticker (24 h)',candles:'Kerzen (24 h)',coverage:'Kerzenabdeckung',gaps:'Datenlücken',lastSeen:'Zuletzt beobachtet',marketUnavailable:'Marktdaten nicht verfügbar',noMarketData:'Noch keine Marktdaten aufgezeichnet oder Recorder-Dateien fehlen.',fresh:'Aktuell',stale:'Veraltet',marketLimited:'Die Auswertung ist auf die letzten 24 Stunden begrenzt; sehr große Dateien werden aus dem jüngsten Dateiausschnitt gelesen.',marketInvalidRows:'Ungültige Zeilen übersprungen',spreadAverage:'Ø Spread (bps)'},
      en:{readonly:'Read-only · no order controls',dailyPnl:'Net PnL by UTC day',openPositions:'Open positions',filtersFeed:'Filters and feed',recentEvents:'Recent paper events',cash:'Available cash',initialValue:'Starting value',todayPnl:'Net PnL today',weekPnl:'Net PnL · 7 days',weekSells:'Sell events · 7 days',fees:'Fees today',slippage:'Slippage today',iteration:'Iteration',lastCycle:'Last bot cycle',asset:'Asset',amount:'Amount',investment:'Invested',entry:'Entry',noneOpen:'No open trades',atrFilter:'ATR filter',sellVeto:'Hailo SELL veto',hailoErrors:'Hailo errors',holdings:'Holdings',lastEvent:'Last journal event',timeLocal:'Time (local)',action:'Action',netPnl:'Net PnL',noEvents:'No journal events yet',pnlUnavailable:'Net PnL unavailable',updated:'Updated',portfolioAt:'Portfolio as of',unavailable:'unavailable',refreshIn:'Next update in',seconds:'s',dashboardUnavailable:'Dashboard data unavailable',buy:'Buy',sell:'Sell',language:'Language',localTime:'Local time',marketTitle:'Public market data · Kraken',marketSubtitle:'Read-only observations · last 24 hours where recorded',marketSelect:'Market',marketNotice:'Quotes and candles are public observations, not actual executions. Spread, candles and price changes do not establish order-book depth, profitability or executable fills.',tickerLast:'Ticker: last price',candleClose:'1-minute candle close',bidLine:'Bid',askLine:'Ask',marketTableTitle:'Market overview',latest:'Latest price',bidAsk:'Bid / ask',spreadBps:'Spread (bps)',change:'Change over observed period',samples:'Ticker samples (24h)',candles:'Candles (24h)',coverage:'Candle coverage',gaps:'Data gaps',lastSeen:'Last observed',marketUnavailable:'Market data unavailable',noMarketData:'No market data recorded yet or recorder files are missing.',fresh:'Fresh',stale:'Stale',marketLimited:'Analysis is limited to the last 24 hours; very large files are read from their newest tail.',marketInvalidRows:'Invalid rows skipped',spreadAverage:'Average spread (bps)'}
    };
    let locale='de';
    try{const saved=localStorage.getItem('paper-dashboard-language');if(saved==='de'||saved==='en')locale=saved}catch(e){console.warn('Language preference could not be loaded',e)}
    let nextRefreshAt=Date.now()+REFRESH_INTERVAL_MS;
    const t=key=>translations[locale][key];
    const fmt=(n,c='EUR')=>n===null||n===undefined?'—':new Intl.NumberFormat(locale==='de'?'de-DE':'en-US',{style:'currency',currency:c,maximumFractionDigits:2}).format(n);
    const esc=(s)=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const cls=n=>n===null||n===undefined?'neutral':n<0?'negative':n>0?'positive':'neutral';
    const localDateTime=value=>{
      const timestamp=Date.parse(value);
      return Number.isNaN(timestamp)?'—':new Intl.DateTimeFormat(locale==='de'?'de-DE':'en-US',{
        year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'
      }).format(timestamp);
    };
    function metric(label,value,color='neutral'){return `<div class="card"><div class="label">${label}</div><div class="value ${color}">${value}</div></div>`}
    function updateClock(){
      const clock=document.getElementById('local-clock'),now=new Date();
      clock.textContent=new Intl.DateTimeFormat(locale==='de'?'de-DE':'en-US',{
        hour:'2-digit',minute:'2-digit',second:'2-digit'
      }).format(now);
      clock.setAttribute('aria-label',`${t('localTime')}: ${clock.textContent}`);
    }
    function updateCountdown(){
      const remaining=Math.max(0,Math.ceil((nextRefreshAt-Date.now())/1000));
      document.getElementById('refresh-countdown').textContent=`${t('refreshIn')} ${remaining}${t('seconds')}`;
    }
    function updateLanguage(){
      document.documentElement.lang=locale;
      document.querySelectorAll('[data-i18n]').forEach(node=>{node.textContent=t(node.dataset.i18n)});
      document.getElementById('language-de').setAttribute('aria-pressed',String(locale==='de'));
      document.getElementById('language-en').setAttribute('aria-pressed',String(locale==='en'));
      document.querySelector('.language-switch').setAttribute('aria-label',t('language'));
      document.getElementById('market-symbol').setAttribute('aria-label',t('marketSelect'));
      if(!window.dashboardData)document.getElementById('updated').textContent=`${t('updated')}: —`;
      updateClock();updateCountdown();
    }
    function setLanguage(value){
      locale=value;
      try{localStorage.setItem('paper-dashboard-language',locale)}catch(e){console.warn('Language preference could not be saved',e)}
      updateLanguage();
      if(window.dashboardData)render(window.dashboardData);
    }
    function drawChart(points){
      const svg=document.getElementById('chart'), w=700,h=220,p=34;
      const values=points.map(x=>x.net_pnl).filter(x=>x!==null);
      if(!values.length){svg.innerHTML=`<text x="20" y="35">${t('pnlUnavailable')}</text>`;return}
      const min=Math.min(0,...values),max=Math.max(0,...values),span=max-min||1;
      const y=v=>p+(max-v)/span*(h-p*2),zero=y(0),step=(w-p*2)/Math.max(1,points.length-1);
      const bars=points.map((x,i)=>{const v=x.net_pnl;if(v===null)return '';const yy=y(v),top=Math.min(yy,zero),height=Math.max(2,Math.abs(zero-yy));return `<rect x="${p+i*step-8}" y="${top}" width="16" height="${height}" rx="3" fill="${v<0?'#ff8585':'#72db9d'}"><title>${esc(x.date)}: ${fmt(v)}</title></rect>`}).join('');
      const labels=points.filter((_,i)=>i%3===0).map(x=>{const i=points.indexOf(x);return `<text x="${p+i*step}" y="${h-7}" text-anchor="middle">${esc(x.date.slice(5))}</text>`}).join('');
      svg.innerHTML=`<line x1="${p}" y1="${zero}" x2="${w-p}" y2="${zero}" stroke="#52647e"/>${bars}${labels}`;
    }
    function drawMarketChart(data){
      const svg=document.getElementById('market-chart'),w=900,h=280,p=42;
      const series=[
        {points:data.ticker_series.map(x=>({timestamp:x.timestamp,value:x.last})),color:'#72db9d'},
        {points:data.candle_series.map(x=>({timestamp:x.timestamp,value:x.close})),color:'#8daeff'},
        {points:data.ticker_series.map(x=>({timestamp:x.timestamp,value:x.bid})),color:'#ffbf69'},
        {points:data.ticker_series.map(x=>({timestamp:x.timestamp,value:x.ask})),color:'#f6df82'}
      ];
      const values=series.flatMap(x=>x.points.map(y=>y.value)).filter(x=>Number.isFinite(x));
      if(!values.length){svg.innerHTML=`<text x="20" y="35">${t('noMarketData')}</text>`;return}
      const min=Math.min(...values),max=Math.max(...values),span=max-min||Math.max(Math.abs(max)*.001,1);
      const times=series.flatMap(x=>x.points.map(y=>Date.parse(y.timestamp))).filter(Number.isFinite);
      const firstTime=Math.min(...times),timeSpan=Math.max(1,Math.max(...times)-firstTime);
      const y=value=>p+(max-value)/span*(h-p*2);
      const paths=series.map(item=>{
        const points=item.points.filter(x=>Number.isFinite(x.value));
        if(!points.length)return '';
        const d=points.map((x,i)=>`${i?'L':'M'} ${p+(Date.parse(x.timestamp)-firstTime)/timeSpan*(w-p*2)} ${y(x.value)}`).join(' ');
        return `<path d="${d}" fill="none" stroke="${item.color}" stroke-width="2"/>`;
      }).join('');
      svg.innerHTML=`<line x1="${p}" y1="${p}" x2="${w-p}" y2="${p}" stroke="#26354d"/><line x1="${p}" y1="${h-p}" x2="${w-p}" y2="${h-p}" stroke="#26354d"/><text x="4" y="${p+4}">${esc(fmt(max))}</text><text x="4" y="${h-p}">${esc(fmt(min))}</text>${paths}`;
    }
    function renderMarket(data){
      const select=document.getElementById('market-symbol');
      if(data.symbols?.length){
        const current=select.value||data.selected_symbol;
        select.innerHTML=data.symbols.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('');
        select.value=data.symbols.includes(current)?current:data.selected_symbol;
      }
      if(!data.available){
        document.getElementById('market-status').textContent=`${t('marketUnavailable')}: ${esc(data.reason||t('noMarketData'))}`;
        document.getElementById('market-metrics').innerHTML='';
        document.getElementById('market-table').innerHTML=`<div class="muted">${esc(data.reason||t('noMarketData'))}</div>`;
        document.getElementById('market-chart').innerHTML=`<text x="20" y="35">${esc(data.reason||t('noMarketData'))}</text>`;
        return;
      }
      const market=data.markets.find(x=>x.symbol===data.selected_symbol);
      if(!market)return;
      document.getElementById('market-status').textContent=`${t(data.freshness_status)} · ${data.latest_data_at?esc(localDateTime(data.latest_data_at)):'—'} · ${data.latest_data_age_seconds===null?'—':Math.round(data.latest_data_age_seconds)+'s'}`;
      document.getElementById('market-metrics').innerHTML=[
        metric(t('latest'),fmt(market.latest_last)),
        metric(t('bidAsk'),`${fmt(market.latest_bid)} / ${fmt(market.latest_ask)}`),
        metric(t('spreadBps'),market.latest_spread_bps===null?'—':market.latest_spread_bps),
        metric(t('spreadAverage'),market.average_spread_bps===null?'—':market.average_spread_bps),
        metric(t('change'),market.price_change_pct_observed===null?'—':`${market.price_change_pct_observed.toFixed(2)}%`,cls(market.price_change_pct_observed)),
        metric(t('samples'),`${market.ticker_samples_24h} · ${market.ticker_samples_per_hour}/h`),
        metric(t('candles'),market.candle_count_24h),
        metric(t('coverage'),market.candle_coverage_pct===null?'—':`${market.candle_coverage_pct}%`),
        metric(t('gaps'),`${market.candle_gap_minutes} min · max ${market.largest_candle_gap_minutes} min`)
      ].join('');
      document.getElementById('market-chart').setAttribute('aria-label',`${data.selected_symbol}: ${t('tickerLast')} / ${t('candleClose')}`);
      drawMarketChart(data);
      document.getElementById('market-table').innerHTML=`<table><thead><tr><th>${t('marketSelect')}</th><th class="num">${t('latest')}</th><th class="num">${t('spreadBps')}</th><th class="num">${t('change')}</th><th class="num">${t('samples')}</th><th class="num">${t('candles')}</th><th class="num">${t('gaps')}</th><th>${t('lastSeen')}</th></tr></thead><tbody>${data.markets.map(x=>`<tr><td>${esc(x.symbol)}</td><td class="num">${fmt(x.latest_last)}</td><td class="num">${x.latest_spread_bps??'—'}</td><td class="num ${cls(x.price_change_pct_observed)}">${x.price_change_pct_observed===null?'—':x.price_change_pct_observed.toFixed(2)+'%'}</td><td class="num">${x.ticker_samples_24h}</td><td class="num">${x.candle_count_24h} · ${x.candle_coverage_pct===null?'—':x.candle_coverage_pct+'%'}</td><td class="num">${x.candle_gap_minutes}</td><td>${esc(localDateTime(x.latest_observed_at))}</td></tr>`).join('')}</tbody></table>`;
      const flags=[];
      if(data.read_window_truncated)flags.push(t('marketLimited'));
      if(data.invalid_ticker_rows||data.invalid_candle_rows)flags.push(`${t('marketInvalidRows')}: ${data.invalid_ticker_rows+data.invalid_candle_rows}`);
      document.getElementById('market-footnote').textContent=flags.join(' · ');
    }
    async function refreshMarket(){
      try{
        const symbol=document.getElementById('market-symbol').value;
        const query=symbol?`?symbol=${encodeURIComponent(symbol)}`:'';
        const response=await fetch(`/api/market-observations${query}`,{cache:'no-store'});
        if(!response.ok)throw new Error(`Market API HTTP ${response.status}`);
        renderMarket(await response.json());
      }catch(error){document.getElementById('market-status').textContent=t('marketUnavailable');console.error(error)}
    }
    function render(d){
      window.dashboardData=d;
      const c=d.portfolio.base_currency,a=d.activity;
      document.getElementById('metrics').innerHTML=[
        metric(t('lastCycle'),d.last_bot_cycle?`${esc(localDateTime(d.last_bot_cycle.timestamp))} · #${d.last_bot_cycle.iteration}`:'—'),
        metric(t('cash'),fmt(d.portfolio.cash,c)),
        metric(t('initialValue'),fmt(d.portfolio.initial_value,c)),
        metric(t('todayPnl'),fmt(a.today_net_pnl,c),cls(a.today_net_pnl)),
        metric(t('weekPnl'),fmt(a.week_net_pnl,c),cls(a.week_net_pnl)),
        metric(t('weekSells'),a.week_sell_events),
        metric(t('fees'),fmt(a.today_fees,c)),
        metric(t('slippage'),fmt(a.today_slippage,c)),
        metric(t('iteration'),d.portfolio.iteration??'—')
      ].join('');
      document.querySelector('#chart').setAttribute('aria-label',`${t('dailyPnl')} · 14 ${locale==='de'?'Tage':'days'}`);
      drawChart(d.daily_net_pnl);
      const pos=d.portfolio.positions;
      document.getElementById('positions').innerHTML=pos.length?`<table><thead><tr><th>${t('asset')}</th><th class="num">${t('amount')}</th><th class="num">${t('investment')}</th><th>${t('entry')}</th></tr></thead><tbody>${pos.map(x=>`<tr><td>${esc(x.coin)}</td><td class="num">${x.amount_coin??'—'}</td><td class="num">${fmt(x.amount_base,c)}</td><td>${esc(localDateTime(x.timestamp))}</td></tr>`).join('')}</tbody></table>`:`<div class="muted">${t('noneOpen')}</div>`;
      const f=d.filters;
      document.getElementById('filters').innerHTML=[
        `${t('atrFilter')}: ${f.atr_guard??'—'}`,`${t('sellVeto')}: ${f.tabular_sell_veto??'—'}`,`${t('hailoErrors')}: ${f.hailo_failures??'—'}`,
        `${t('holdings')}: ${d.portfolio.holdings.length}`,`${t('lastEvent')}: ${esc(localDateTime(d.last_journal_event))}`
      ].map(x=>`<span class="pill">${x}</span>`).join('');
      document.getElementById('events').innerHTML=d.recent_events.length?`<table><thead><tr><th>${t('timeLocal')}</th><th>${t('asset')}</th><th>${t('action')}</th><th class="num">${t('amount')}</th><th class="num">${t('netPnl')}</th></tr></thead><tbody>${d.recent_events.map(x=>`<tr><td>${esc(localDateTime(x.timestamp))}</td><td>${esc(x.coin)}</td><td>${x.action==='buy'?t('buy'):x.action==='sell'?t('sell'):esc(x.action)}</td><td class="num">${fmt(x.amount_base,c)}</td><td class="num ${cls(x.net_pnl_base)}">${fmt(x.net_pnl_base,c)}</td></tr>`).join('')}</tbody></table>`:`<div class="muted">${t('noEvents')}</div>`;
      refreshMarket();
    }
    async function refresh(){try{const r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw new Error(`HTTP ${r.status}`);render(await r.json());document.getElementById('updated').textContent=`${t('updated')}: ${new Date().toLocaleTimeString(locale==='de'?'de-DE':'en-US')}`}catch(e){document.getElementById('updated').textContent=t('dashboardUnavailable');console.error(e)}finally{nextRefreshAt=Date.now()+REFRESH_INTERVAL_MS;updateCountdown();setTimeout(refresh,REFRESH_INTERVAL_MS)}}
    document.getElementById('language-de').addEventListener('click',()=>setLanguage('de'));
    document.getElementById('language-en').addEventListener('click',()=>setLanguage('en'));
    document.getElementById('market-symbol').addEventListener('change',refreshMarket);
    updateLanguage();updateClock();updateCountdown();setInterval(updateClock,1000);setInterval(updateCountdown,1000);refresh();
  </script>
</body>
</html>"""


if __name__ == "__main__":
    main()
