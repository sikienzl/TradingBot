"""CSV trade journal, durable paper-trade commits and journal-derived tuning.

Layer-1 collaborator: the ``BotConfig``, the analytics writer and the run
bookkeeping reach these functions through :class:`JournalContext`, the
``Portfolio`` is handed to the two-phase paper-trade transaction explicitly, and
that transaction receives the bot-owned helpers through
:class:`PaperTradeHooks`. That keeps the on-disk journal schema, the recovery of
a half-written trade row and the confidence-threshold recommendation testable
without constructing a :class:`CryptoTradingBot`. ``CryptoTradingBot`` keeps a
thin delegator for every name below and still owns the run id, the config
fingerprint, the iteration counter and the ``_paper_trade_failed`` flag.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRADE_JOURNAL_FIELDS = [
    'timestamp', 'iteration', 'coin', 'action', 'price', 'amount_coin',
    'amount_base', 'pnl_base', 'pnl_pct', 'hold_seconds',
    'signal_source', 'signal_confidence', 'recommendation', 'reason', 'dry_run',
    'run_id', 'config_hash', 'mfe_pct', 'mae_pct',
    'execution_cost_mode', 'entry_cost_mode', 'reference_price', 'fee_base',
    'slippage_base', 'gross_pnl_base', 'net_pnl_base',
]


@dataclass(frozen=True)
class JournalContext:
    """Bot-owned journal bookkeeping handed to the journal writers.

    ``journal_fields`` and ``journal_row`` are read at call time so the bot can
    hand over its current schema and its (patchable) row enricher. The
    ``Portfolio`` is deliberately absent: only the two-phase paper-trade
    transaction needs it and it is passed there explicitly.
    """

    config: Any
    analytics_writer: Any
    run_id: str
    config_hash: str
    journal_fields: list[str]
    journal_row: Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class PaperTradeHooks:
    """Bot-owned collaborators the two-phase paper-trade commit needs.

    The bot passes these bound so the service never reaches into the bot, and
    monkeypatching a bot method in a test still takes effect.
    """

    sync_directory: Callable[[str], None]
    recover_paper_trade: Callable[[], None]
    set_paper_trade_failed: Callable[[bool], None]


def init_journal(config: Any) -> list[str]:
    """Creates or safely extends the CSV journal schema.

    Returns the field list the caller has to keep using; the bot stores it as
    ``self._journal_fields``.
    """
    path = config.performance_log_file
    if not os.path.exists(path):
        with open(path, 'w', newline='', encoding='utf-8') as journal:
            csv.writer(journal).writerow(TRADE_JOURNAL_FIELDS)
        return list(TRADE_JOURNAL_FIELDS)

    with open(path, newline='', encoding='utf-8-sig') as journal:
        existing_fields = next(csv.reader(journal), [])
    fields = list(dict.fromkeys(existing_fields + TRADE_JOURNAL_FIELDS))
    if existing_fields == fields:
        return fields

    file_mode = stat.S_IMODE(os.stat(path).st_mode)
    directory = os.path.dirname(os.path.abspath(path))
    descriptor, temporary_path = tempfile.mkstemp(
        prefix='.trade-journal-', suffix='.csv', dir=directory)
    try:
        with os.fdopen(
            descriptor, 'w', newline='', encoding='utf-8'
        ) as destination, open(path, newline='', encoding='utf-8-sig') as source:
            reader = csv.DictReader(source)
            writer = csv.DictWriter(
                destination, fieldnames=fields,
                extrasaction='ignore')
            writer.writeheader()
            for row in reader:
                row.setdefault('run_id', 'legacy')
                row.setdefault('config_hash', 'legacy')
                row.setdefault('mfe_pct', '')
                row.setdefault('mae_pct', '')
                row.setdefault('execution_cost_mode', 'legacy_unknown')
                writer.writerow(row)
        os.chmod(temporary_path, file_mode)
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return fields


def sync_directory(path: str) -> None:
    descriptor = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recover_paper_trade(
    context: JournalContext,
    portfolio: Any,
    *,
    sync_directory: Callable[[str], None] = sync_directory,
    logger: logging.Logger = logger,
) -> None:
    """Complete a durable intent before permitting another paper trade."""
    config = context.config
    intent_path = portfolio.state_file + '.trade-transaction.json'
    if not os.path.lexists(intent_path):
        return
    try:
        with open(intent_path, encoding='utf-8') as f:
            envelope = json.load(f)
        payload = envelope['payload']
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True).encode('utf-8')
        if (envelope['sha256'] != hashlib.sha256(encoded).hexdigest()
                or payload['version'] != 1
                or payload['journal'] != os.path.abspath(config.performance_log_file)
                or payload['state_file'] != os.path.abspath(portfolio.state_file)
                or not isinstance(payload['offset'], int) or payload['offset'] < 0
                or not isinstance(payload['row'], str)
                or not isinstance(payload['state'], dict)
                or payload['state'].get('base_currency') != portfolio.base_currency):
            raise ValueError("Invalid paper trade intent")
        row = payload['row'].encode('utf-8')
        if not row:
            raise ValueError("Empty paper trade row")
        with open(payload['journal'], 'r+b') as journal:
            prefix = journal.read(payload['offset'])
            if (len(prefix) != payload['offset']
                    or hashlib.sha256(prefix).hexdigest() != payload['prefix_sha256']):
                raise ValueError("Paper trade journal prefix changed")
            tail = journal.read()
            if not row.startswith(tail):
                raise ValueError("Paper trade journal has an unexpected tail")
            if tail != row:
                journal.seek(payload['offset'])
                journal.write(row)
                journal.truncate()
            journal.flush()
            os.fsync(journal.fileno())
        portfolio.save_state(state=payload['state'])
        os.unlink(intent_path)
        sync_directory(intent_path)
        logger.info("Completed public paper trade transaction")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Cannot recover paper trade transaction; stopping") from exc


def commit_paper_trade(
    context: JournalContext,
    portfolio: Any,
    row: dict,
    *,
    hooks: PaperTradeHooks,
    logger: logging.Logger = logger,
) -> None:
    config = context.config
    intent_path = portfolio.state_file + '.trade-transaction.json'
    temporary_path = None
    hooks.set_paper_trade_failed(True)
    if os.path.lexists(intent_path):
        raise RuntimeError("Pending paper trade transaction; restart to recover")
    row = context.journal_row(row)
    buffer = io.StringIO(newline='')
    csv.DictWriter(buffer, fieldnames=context.journal_fields,
                   extrasaction='ignore').writerow(row)
    try:
        journal_path = os.path.abspath(config.performance_log_file)
        with open(journal_path, 'rb') as journal:
            prefix = journal.read()
            os.fsync(journal.fileno())
        if not prefix.endswith(b'\n'):
            raise ValueError("Paper trade journal has an incomplete last row")
        hooks.sync_directory(journal_path)
        payload = {
            'version': 1,
            'journal': journal_path,
            'state_file': os.path.abspath(portfolio.state_file),
            'offset': len(prefix),
            'prefix_sha256': hashlib.sha256(prefix).hexdigest(),
            'row': buffer.getvalue(),
            'state': portfolio._state(),
        }
        # Normalize datetimes exactly as the portfolio writer does.
        payload = json.loads(json.dumps(payload, default=lambda o: o.isoformat()
                                        if isinstance(o, datetime) else str(o)))
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True).encode('utf-8')
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8',
            dir=os.path.dirname(os.path.abspath(intent_path)),
            prefix='.paper-trade-', delete=False,
        ) as f:
            temporary_path = f.name
            json.dump({'payload': payload, 'sha256': hashlib.sha256(encoded).hexdigest()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, intent_path)
        temporary_path = None
        hooks.sync_directory(intent_path)
        hooks.recover_paper_trade()
        hooks.set_paper_trade_failed(False)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("Cannot commit paper trade transaction; stopping") from exc
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    context.analytics_writer.write_trade(row, config.base_currency)


def journal_row(
    config: Any,
    run_id: str,
    config_hash: str,
    iteration: int,
    row: dict,
) -> dict:
    row = {
        **row,
        'run_id': row.get('run_id', run_id),
        'config_hash': row.get('config_hash', config_hash),
        'execution_cost_mode': row.get(
            'execution_cost_mode', 'gross_legacy' if row.get(
                'dry_run', config.dry_run) else 'live_exchange'),
    }
    return {
        **row,
        'timestamp': row.get('timestamp', datetime.now(UTC).isoformat()),
        'iteration': row.get('iteration', iteration),
        'coin': row.get('coin', ''),
        'action': row.get('action', ''),
        'price': row.get('price', 0.0),
        'amount_coin': row.get('amount_coin', 0.0),
        'amount_base': row.get('amount_base', 0.0),
        'pnl_base': row.get('pnl_base', 0.0),
        'pnl_pct': row.get('pnl_pct', 0.0),
        'hold_seconds': row.get('hold_seconds', 0.0),
        'signal_source': row.get('signal_source', ''),
        'signal_confidence': row.get('signal_confidence', ''),
        'recommendation': row.get('recommendation', ''),
        'reason': row.get('reason', ''),
        'dry_run': row.get('dry_run', config.dry_run),
        'mfe_pct': row.get('mfe_pct', ''),
        'mae_pct': row.get('mae_pct', ''),
    }


def append_trade_journal(context: JournalContext, row: dict) -> None:
    config = context.config
    row = {
        **row,
        'run_id': row.get('run_id', context.run_id),
        'config_hash': row.get('config_hash', context.config_hash),
        'execution_cost_mode': row.get(
            'execution_cost_mode', 'gross_legacy' if row.get(
                'dry_run', config.dry_run) else 'live_exchange'),
    }
    if config.performance_log_enabled:
        with open(config.performance_log_file, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(
                f, fieldnames=context.journal_fields,
                extrasaction='ignore')
            writer.writerow(context.journal_row(row))
    context.analytics_writer.write_trade(row, config.base_currency)


def recommend_tabular_threshold(
    config: Any,
    *,
    logger: logging.Logger = logger,
) -> float | None:
    """Recommends a confidence threshold based on historical sell trades."""
    path = config.performance_log_file
    if not os.path.exists(path):
        return None

    try:
        df = pd.read_csv(path)
    except (
        OSError,
        UnicodeError,
        pd.errors.ParserError,
        pd.errors.EmptyDataError,
    ) as e:
        logger.warning(
            f"Journal could not be read for auto-tuning: {e}")
        return None

    if df.empty or 'action' not in df.columns or 'pnl_base' not in df.columns or 'signal_confidence' not in df.columns:
        return None

    sells = df[df['action'] == 'sell'].copy()
    if sells.empty:
        return None

    # Prefer more recent trades
    sells = sells.tail(
        max(1, config.auto_tune_lookback_trades)).copy()
    sells['pnl_base'] = pd.to_numeric(sells['pnl_base'], errors='coerce')
    sells['signal_confidence'] = pd.to_numeric(
        sells['signal_confidence'], errors='coerce')
    sells = sells.dropna(subset=['pnl_base', 'signal_confidence'])
    if sells.empty:
        return None

    step = max(config.auto_tune_threshold_step, 0.001)
    thresholds = np.arange(
        config.auto_tune_threshold_min,
        config.auto_tune_threshold_max + (step * 0.5),
        step,
    )

    candidates = []
    for th in thresholds:
        part = sells[sells['signal_confidence'] >= th]
        n = len(part)
        if n < config.auto_tune_min_trades:
            continue
        pnl = float(part['pnl_base'].sum())
        avg = float(part['pnl_base'].mean())
        win_rate = float((part['pnl_base'] > 0).mean() * 100.0)
        candidates.append((th, n, pnl, avg, win_rate))

    if not candidates:
        return None

    # Primarily highest total PnL, secondarily higher AvgPnL, then more trades
    best = max(candidates, key=lambda x: (x[2], x[3], x[1]))
    logger.info(
        f"🔧 Auto-Tune Threshold Recommendation: {best[0]:.2f} "
        f"(trades={best[1]}, pnl={best[2]:.6f} {config.base_currency}, "
        f"avg={best[3]:.6f}, win_rate={best[4]:.2f}%)")
    return float(best[0])
