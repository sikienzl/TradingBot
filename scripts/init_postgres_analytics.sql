create schema if not exists trading_analytics;

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