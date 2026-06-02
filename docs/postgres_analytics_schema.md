# PostgreSQL Analytics Schema Proposal

This schema is intended for optional analytics-only writes from the trading bot.
The bot remains operational without PostgreSQL. When enabled, it only appends rows.

## Goals

- Keep the runtime path low-risk: inserts only, no read dependency.
- Make trade and portfolio history queryable with standard SQL.
- Stay close to the existing file outputs such as `trade_journal.csv`.
- Support later expansion without breaking the initial tables.

## Recommended Schema Name

Use a dedicated schema instead of `public` when possible:

```sql
create schema if not exists trading_analytics;
```

If you want to keep setup simpler, `public` is also acceptable.

## Phase 1: Minimal Useful Tables

These two tables match the current optional integration points in the bot.

### `trade_events`

Purpose:
- Append one row per buy or sell event.
- Replace ad-hoc CSV filtering for analysis queries.
- Keep enough context to compare signal source, reason, holding time, and PnL.

```sql
create table if not exists trading_analytics.trade_events (
    id bigserial primary key,
    event_ts timestamptz not null,
    iteration integer not null,
    coin text not null,
    action text not null,
    price double precision not null,
    amount_coin double precision not null,
    amount_base double precision not null,
    pnl_base double precision not null,
    pnl_pct double precision not null,
    hold_seconds double precision not null,
    signal_source text,
    signal_confidence double precision,
    recommendation text,
    reason text,
    dry_run boolean not null,
    base_currency text not null,
    created_at timestamptz not null default now()
);

create index if not exists trade_events_event_ts_idx
    on trading_analytics.trade_events (event_ts desc);

create index if not exists trade_events_coin_action_idx
    on trading_analytics.trade_events (coin, action);

create index if not exists trade_events_reason_idx
    on trading_analytics.trade_events (reason);
```

Typical questions this answers:
- Which coins lose most often via `MAX-HOLD-TIME`?
- How does `catboost` vs `rules` perform over the last 30 days?
- What is average `pnl_pct` by exit reason?

### `portfolio_snapshots`

Purpose:
- Append one row per iteration or per configured interval.
- Capture equity curve, cash usage, and open-position state.
- Support drawdown and utilization analysis without scraping logs.

```sql
create table if not exists trading_analytics.portfolio_snapshots (
    id bigserial primary key,
    snapshot_ts timestamptz not null,
    iteration integer not null,
    portfolio_value_base double precision not null,
    cash_base double precision not null,
    base_currency text not null,
    open_positions_count integer not null,
    analyzed_coins_count integer not null,
    buy_recommendations_count integer not null,
    dry_run boolean not null,
    holdings_json jsonb not null,
    open_trades_json jsonb not null,
    created_at timestamptz not null default now()
);

create index if not exists portfolio_snapshots_snapshot_ts_idx
    on trading_analytics.portfolio_snapshots (snapshot_ts desc);

create index if not exists portfolio_snapshots_iteration_idx
    on trading_analytics.portfolio_snapshots (iteration);
```

Typical questions this answers:
- How did portfolio value evolve during the last week?
- How often was the bot fully invested?
- How often were there many BUY candidates but low cash or open slots?

## Phase 2: Strong Next Additions

If analytics usage grows, these are the next worthwhile tables.

### `analysis_iterations`

Purpose:
- One row per bot iteration.
- Summarize recommendation mix and entry-mode state.
- Make iteration-level diagnostics queryable without log parsing.

```sql
create table if not exists trading_analytics.analysis_iterations (
    id bigserial primary key,
    iteration integer not null,
    analysis_ts timestamptz not null,
    entry_mode text,
    analyzed_coins_count integer not null,
    buy_count integer not null,
    hold_uptrend_count integer not null,
    hold_downtrend_count integer not null,
    sellish_count integer not null,
    blocked_uptrend_count integer not null default 0,
    blocked_downtrend_count integer not null default 0,
    dynamic_exclusions text[] not null default '{}',
    created_at timestamptz not null default now()
);
```

### `coin_analysis`

Purpose:
- One row per analyzed coin per iteration.
- Persist the recommendation and the gate context.
- Best source for later model and rules diagnostics.

```sql
create table if not exists trading_analytics.coin_analysis (
    id bigserial primary key,
    iteration integer not null,
    analysis_ts timestamptz not null,
    coin text not null,
    recommendation text not null,
    score integer not null,
    rule_recommendation text,
    rule_score integer,
    signal_source text,
    signal_confidence double precision,
    rsi double precision,
    buy_proba double precision,
    hold_proba double precision,
    sell_proba double precision,
    ret_1 double precision,
    ret_3 double precision,
    macd_hist double precision,
    gate_block_reason text,
    dry_run boolean not null,
    created_at timestamptz not null default now()
);

create index if not exists coin_analysis_iteration_idx
    on trading_analytics.coin_analysis (iteration);

create index if not exists coin_analysis_coin_ts_idx
    on trading_analytics.coin_analysis (coin, analysis_ts desc);
```

## Phase 3: Tuning and Governance Tables

These are useful once you want to correlate bot behavior with parameter changes.

### `tuning_events`

```sql
create table if not exists trading_analytics.tuning_events (
    id bigserial primary key,
    event_ts timestamptz not null,
    source text not null,
    parameter text not null,
    old_value text,
    new_value text,
    reason text,
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
```

### `scorecard_runs`

```sql
create table if not exists trading_analytics.scorecard_runs (
    id bigserial primary key,
    run_ts timestamptz not null,
    profile text,
    verdict text not null,
    final_exit_code integer not null,
    closed_trades integer,
    win_rate_pct double precision,
    profit_factor double precision,
    max_drawdown_pct double precision,
    realized_pnl_base double precision,
    primary_reason text,
    report_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
```

## Naming and Type Recommendations

- Use `timestamptz` consistently.
- Use `double precision` for prices and PnL because the bot already uses Python floats heavily.
- Use `jsonb` only for nested snapshots or flexible metadata, not for core query columns.
- Keep `coin`, `action`, `reason`, `signal_source`, and `recommendation` as plain `text` first; normalize later only if needed.

## Suggested Initial Queries

Top negative timeout coins in the last 30 days:

```sql
select
    coin,
    count(*) as sells,
    round(sum(pnl_base)::numeric, 4) as pnl_sum,
    round(avg(pnl_pct)::numeric, 4) as avg_pnl_pct
from trading_analytics.trade_events
where action = 'sell'
  and reason ilike '%MAX-HOLD-TIME%'
  and event_ts >= now() - interval '30 days'
group by coin
order by pnl_sum asc, sells desc;
```

Performance by signal source:

```sql
select
    signal_source,
    count(*) filter (where action = 'sell') as closed_trades,
    round(sum(pnl_base) filter (where action = 'sell')::numeric, 4) as pnl_sum,
    round(avg(pnl_pct) filter (where action = 'sell')::numeric, 4) as avg_pnl_pct
from trading_analytics.trade_events
group by signal_source
order by pnl_sum desc nulls last;
```

Recent portfolio curve:

```sql
select
    snapshot_ts,
    portfolio_value_base,
    cash_base,
    open_positions_count
from trading_analytics.portfolio_snapshots
where snapshot_ts >= now() - interval '24 hours'
order by snapshot_ts;
```

## Recommendation

Start with only:
- `trade_events`
- `portfolio_snapshots`

That is the right first cut for your current use case: optional, append-only, analysis-focused.
Add `analysis_iterations` and `coin_analysis` only when you actively need recommendation-level forensic analysis.