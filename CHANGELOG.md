# Changelog

Notable changes are recorded here. New work is collected under **Unreleased**;
a version heading is assigned when a release is prepared.

## Unreleased

### Added

- Publish a second package per release tag: `v*` now produces
  `trading-cluster-<version>.tar.gz` next to the Pi tarball, so the two-node CM5
  cluster (Dockerfiles, Hailo inference/tunnel/shadow units, dashboard) has an
  artifact of its own instead of only a Pi package. The cluster archive is built
  deterministically by `.github/scripts/cluster_release.py`, records version and
  commit in `version.txt`, ships the generic `ansible/` paper-dashboard starter
  (example inventories and vault templates only) plus
  `requirements-hailo-server.txt`, and excludes the LLM adapter, Hailo SDK
  packages and HEF files.
- Added the analytics PostgreSQL backup wrappers that
  `deploy/postgresql-analytics-backup.service`/`-restore-check.service` execute:
  `scripts/postgresql-analytics-backup` (custom-format `pg_dump` to the NAS
  mount with a `.sha256` sidecar and retention pruning) and
  `scripts/postgresql-analytics-restore-check` (restores the newest dump into a
  throwaway database and verifies the `trading_analytics` schema). Database name
  and user are read from the container environment, so no credentials live on
  the host. New daily/weekly timer units (`deploy/postgresql-analytics-backup.timer`,
  `deploy/postgresql-analytics-restore-check.timer`) schedule both jobs.
- The cluster archive now keeps the executable bit of tracked scripts
  (git mode 100755) instead of flattening every member to 0644, so unpacked
  wrappers can be run directly.

### Fixed

- The Pi package now contains everything `scripts/install_pi.sh` and the
  packaged READMEs expect: the `docs/` guides and the weekly auto-tuning units
  and scripts (`deploy/tuning-cycle.service`/`.timer`,
  `scripts/run_weekly_tuning_cycle.sh`, `scripts/apply_tuning_recommendation.py`,
  `scripts/recommend_tuning_change.py`), which were previously only warnings in
  the installer.

## v1.0.25 - 2026-11-24

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
- Add an informational mypy type-check job to CI (non-blocking) plus a
  `[tool.mypy]` section and `mypy`/`ruff` in the `dev` extra.

### Changed

- Fail the public paper daily observation (exit 1) when neither the journal nor
  the bot log shows activity within `--max-data-age-hours`, so a stopped
  collector can no longer look like an idle day; the JSON is still written and
  `latest.json` keeps the last healthy report.
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
- Split `export_pnl_metrics.read_latest_portfolio_snapshot` (complexity 33 → 3,
  `# noqa: C901` removed) into the log-tail scanner `_scan_portfolio_tail`
  (returns the price/open-trade maps, mutates the snapshot) and the holdings
  aggregator `_aggregate_portfolio_holdings`.
- Split `public_paper_dashboard.load_market_observations` (complexity 29 → 10,
  `# noqa: C901` removed) by promoting its nested closures to module helpers:
  `_valid_rows` (ticker/candle validation and window grouping),
  `_build_market_summary` (per-symbol quality summary, replacing the
  per-symbol loop with a list comprehension) and `_downsample` (point-series
  thinning).
- Split `CryptoTradingBot.run` (complexity 47 → 8, the last `# noqa: C901` in
  `src/` removed) into the startup banner `_log_startup_banner`, the entry
  candidate ranking/slot-filling `_select_entry_candidates`, the signal-exit
  pass `_execute_signal_exits` and the position-opening pass
  `_open_new_positions`; the main loop now just orchestrates these phases.
- De-duplicate the fallback and force-fill candidate filters in
  `_select_entry_candidates` into a single `_candidates_within_filters`
  closure (one shared predicate instead of two identical comprehensions) and
  extract the ATR/cash-reserve sizing from `_open_new_positions` into
  `_compute_entry_position_size`.
- Split `export_pnl_metrics._scan_portfolio_tail` (complexity 24 → 8) so each
  log-line pattern is a focused parser (`_parse_portfolio_value_line`,
  `_parse_cash_line`, `_parse_holdings_line`, `_parse_open_trades_line`,
  `_parse_current_price_line`) and the early-exit condition became
  `_portfolio_scan_complete`; the scanner now only dispatches per line.
- Split `CryptoTradingBot._dry_run_sell` (complexity 19 → 6) into
  `_resolve_dry_run_sell_entry` (entry validation and quantity capping),
  `_dry_run_sell_proceeds` (fee/net proceeds validation),
  `_apply_dry_run_sell_to_holdings`, `_record_dry_run_sell_close`,
  `_dry_run_sell_gross_net_pnl` and `_update_open_trades_after_dry_run_sell`;
  the journal row and commit/persist flow stay in the dispatcher so the
  sell behaviour is unchanged.
- Split `HistoricalRuleBacktester.run` (complexity 19 → 6) into
  `_select_replay_window`, `_close_signal_exits`, `_rank_entry_candidates`,
  `_open_backtest_positions`, `_apply_intrabar_exits` and
  `_close_remaining_positions`; cash flows through each phase explicitly so
  the replay order and next-bar execution semantics are unchanged.
- Burn down the informational mypy baseline (81 → 11 findings) by annotating
  loosely-typed attributes and locals on the money path: `self.performance`
  (`dict[str, Any]`), optional handles (`_conn`, `_psycopg`, `_vdevice`,
  `lock_file`, `tabular_predictor`), scan accumulators, and the reused tuning
  variables in `_clamp_ai_changes`/`_apply_ai_changes`. All `operator`, `index`
  and
  `return`-value classes are eliminated; the remaining 11 are `float(...)`
  calls already guarded by `try/except (TypeError, ValueError)`. Also fixes a
  latent `severity_rank.get` → `__getitem__` key lookup and adds the missing
  `return False` in `_maybe_partial_take_profit` (call site uses truthiness,
  so behaviour is unchanged).
- Remove the stale `train_trading_model_backup.py` and ignore `.scannerwork/`
  and `.github/copilot-instructions.md`.
- Rename the misspelled `src/data_preperation.py` module to
  `src/data_preparation.py` (test module and documentation references
  included).
- Stop tracking the hatch-vcs generated `src/_version.py`; it is written by the
  build hook and no runtime module imports it.
- Resolve `src/` imports through the repository root instead of per-file
  workarounds: removed the `sys.path.insert(0, ROOT)` hacks from `scripts/` and
  the duplicated `if __package__:` / `else` import branches from `src/`, which
  now uses absolute `from src.x import ...` throughout (relative imports only
  inside the package). Entry points were aligned accordingly: the
  `trading-bot` and `pnl-exporter` systemd units and the Raspberry Pi wrapper
  set `PYTHONPATH` and start `python -m src.<module>`, and the docs invoke
  scripts that import `src/` as `python -m scripts.<module>`. No editable
  install is required, because server and Pi deployments receive the code as a
  tarball without `.git`, where hatch-vcs versioning cannot run. The mypy
  `no-redef` exemption for the removed import pattern was dropped.
- Centralize Ruff configuration in `pyproject.toml` (excludes plus a
  cyclomatic-complexity gate at `max-complexity = 25`) so local `ruff check .`
  matches CI; existing hotspots carry `# noqa: C901` to freeze current
  complexity while preventing regressions.
- Fix the previously failing lint gate: sort imports, mark shebang scripts
  executable, and collapse nested `with` blocks in the cluster-starter and
  release scripts.
- Make CI tests more informative: drop `--maxfail=1` and report `src/`
  coverage (`--cov=src`, term-missing and XML).
- Skip the Hailo end-to-end experiment test when the optional `hailo-export`
  extra is absent, and add a dedicated CI job that installs the extra so the
  real train-to-ONNX path is still exercised. The slim test matrix installs no
  `torch`/`onnx`, so the test previously failed there while passing locally.

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
- The import cleanup changes how the services are launched, not what they do:
  `python src/pnl_exporter.py` (a plain file path) no longer works because the
  module now uses relative imports, so the refreshed `trading-bot` and
  `pnl-exporter` unit files must be installed together with the code — an old
  unit combined with new code fails at startup. Both units already set
  `PYTHONPATH`, and `systemctl daemon-reload` is required after updating them.
- Run in dry-run/simulation first and compare results by `run_id` and
  `config_hash`; these reports do not guarantee profitability.
