# Changelog

Notable changes are recorded here. Versions are assigned when a release is
prepared; this file intentionally remains under **Unreleased** until then.

## Unreleased

### Added

- Add a rebuildable Hailo DFC compiler Dockerfile and compilation wrapper;
  SDK packages are supplied separately and are not distributed.
- Prepare an optional remote Hailo-8 inference backend, fixed feature/metadata
  contract, MLP-to-ONNX exporter, authenticated service, and systemd template;
  CatBoost remains the default.
- Fail closed on remote Hailo prediction errors and document the separate
  `.75` runtime host and later DFC/HEF compilation step.
- Add per-process `run_id` and a SHA-256 fingerprint of non-secret bot settings
  to CSV and PostgreSQL trade events.
- Record observed maximum favorable and adverse excursion (`mfe_pct`,
  `mae_pct`) for closed trades, and preserve updated dry-run position
  extremes across restarts.
- Report journal freshness, strict BUY candidates withheld by the recent-PnL
  guard, and the age of the last closed trade without bypassing the guard.
- Break journal results down by run/configuration and exit reason, including
  observed excursions; flag coin/source samples below 20 trades as descriptive
  only.
- Add an offline historical OHLCV replay using shared rule recommendations,
  next-candle execution, transaction costs, and a chronological holdout.

### Changed

- Redact the Hailo service token from startup configuration logs.
- Extend existing CSV journals atomically with the new columns and mark
  historical rows as `legacy`.
- Add the new trade-event columns to PostgreSQL schemas, with additive
  migrations for existing tables.
- Keep confidence-threshold selection chronological: select on the first 70%
  of trades and report on the final 30% holdout.
- Make the example backtester use next-bar execution, chronological multi-coin
  prices, and configurable fee/slippage assumptions.
- Consolidate duplicated PnL metric code: `pnl_exporter.py` now delegates
  metric computation to `export_pnl_metrics.py`, with the shared
  `format_prometheus_metrics` emitting snapshot metrics only when present so
  both the HTTP exporter and the one-shot script keep their exact output.
- Split oversized `CryptoTradingBot` methods to cut cyclomatic complexity:
  `_execute_trade` into dry-run/real branches, and `run()` into reusable
  helpers (`_entry_pair_allowed`, `_entry_guards_allow`,
  `_log_portfolio_status`).
- Split `_manage_open_trades` (complexity 34 → 12) into focused exit helpers
  (`_maybe_partial_take_profit`, `_partial_tp_remainder_exit_reason`,
  `_downtrend_reversal_exit_reason`, `_uptrend_rules_exit_reason`,
  `_core_exit_reason`), each below the complexity gate.
- Extract `_build_tabular_features` and `_apply_ml_signal` from `_analyze_coin`
  (complexity 27 → 15, now under the gate so its `# noqa: C901` is removed):
  the tabular indicator/feature-row construction is a pure helper returning the
  feature row and MACD histogram, and the optional ML/LLM ensemble signal is a
  separate helper returning the updated recommendation, score and signal source.
- Split `BotConfig.__init__` (500 lines, complexity 32 → 1) into nine focused
  loader methods (`_load_exchange_settings`, `_load_risk_management_settings`,
  `_load_trading_parameters`, `_load_entry_strategy_settings`,
  `_load_entry_filter_and_reentry_settings`, `_load_exit_management_settings`,
  `_load_ml_settings`, `_load_analytics_and_guardrail_settings`,
  `_load_ai_copilot_settings`); the six nested env-parsing helpers moved to
  module level so they are importable and testable on their own.
- Split `_update_portfolio_balance` (complexity 27 → 4, `# noqa: C901` removed)
  into one dispatcher plus four mode-specific initialisers
  (`_initialize_simulation_portfolio_from_kraken`,
  `_initialize_simulated_portfolio`, `_initialize_dry_run_portfolio`,
  `_refresh_live_portfolio_balance`); the nested balance-parsing helpers became
  module-level `_currency_aliases` / `_extract_cash_and_holdings`.
- Split `_execute_dry_run_trade` (complexity 28 → 4, `# noqa: C901` removed)
  into a dispatcher plus `_plan_dry_run_fill` (request validation and
  slippage/fee fill calculation), `_dry_run_buy` and `_dry_run_sell`; the
  journal-row construction and paper-trade commit logic stay inside their
  branch so behaviour is unchanged.
- Remove the stale `train_trading_model_backup.py` and ignore `.scannerwork/`
  and `.github/copilot-instructions.md`.
- Centralize Ruff configuration in `pyproject.toml` (excludes plus a
  cyclomatic-complexity gate at `max-complexity = 25`) so local `ruff check .`
  matches CI; existing hotspots carry `# noqa: C901` to freeze current
  complexity while preventing regressions.
- Fix the previously failing lint gate: sort imports, mark shebang scripts
  executable, and collapse nested `with` blocks in the cluster-starter and
  release scripts.
- Make CI tests more informative: drop `--maxfail=1` and report `src/`
  coverage (`--cov=src`, term-missing and XML).

### Safety and rollout

- No entry/exit thresholds or PnL-guard behavior were changed by these
  diagnostics and analysis updates.
- Hailo support is preparation-only: no trading HEF is compiled, no cluster
  service is deployed, and the backend is not enabled by default.
- The historical replay is offline-only and does not change live trading
  behavior; its holdout results are not a profitability guarantee.
- The PnL consolidation and method splits are behavior-preserving refactors:
  exported metric values and trading decisions are unchanged, verified by the
  existing test suite.
- Run in dry-run/simulation first and compare results by `run_id` and
  `config_hash`; these reports do not guarantee profitability.
