# Changelog

Notable changes are recorded here. New work is collected under **Unreleased**;
a version heading is assigned when a release is prepared.

## Unreleased

### Added

- `tests/test_architecture.py` locks the package layout that the refactor
  established. Every module under `src/` is assigned a layer, upward imports
  (a foundational module importing a composition-root module) and import cycles
  fail the test suite, `src/trading_bot.py` must stay a leaf apart from the
  `init_kraken_paper` bootstrap, and the three oversized modules may not grow
  past their current line counts. This is the guard that keeps the God-class
  extraction from sliding back.
- Unit tests for three modules that previously had no coverage at all:
  `src/get_data.py` (86%), `src/predict.py` (91%) and
  `scripts/recommend_tuning_change.py` (99%). The exchange tests drive a fake
  ccxt exchange, so no network access is needed, and the LLM tests stub
  `torch`/`transformers` when they are not installed.
- The CI unit-test job now measures branch coverage and includes `scripts/`,
  matching the command documented in `CONTRIBUTING.md`.
- Three focused collaborators extracted from `src/trading_bot.py`, each with
  its own unit tests: `src/indicator_service.py` (RSI/SMA/MACD/ATR maths),
  `src/state_store.py` (auto-tune and AI co-pilot state files, cooldown and
  quota normalisation) and `src/market_data.py` (OHLCV fetch and batched
  ticker fetch with retry policy). The bot keeps thin delegators with the
  previous method names, so behaviour and existing tests are unchanged.
- Unit tests for the new modules: `tests/test_indicator_service.py`,
  `tests/test_state_store.py` and `tests/test_market_data.py` (indicator
  values, `.bak` recovery, atomic writes, retry/back-off and batch handling).
- `src/entry_selection.py`, a pure entry-selection collaborator holding the
  rules that used to be buried inside the bot's main loop: market-mode
  classification from the signal mix, filter-reason codes and reason mixes,
  the shared blocked-candidate diagnostics for the uptrend / downtrend-reversal
  / fallback / buy-attempt gates, and the strict, fallback and force-fill
  candidate ranking (including the recent-PnL guard and the entry-guard cache).
  `TradingBot._select_entry_candidates()` is now a thin delegator that keeps the
  bot-owned state (occupied positions, free slots, excluded pairs, degraded
  sources) and the logging, so behaviour and the existing tests are unchanged.
  Covered by 28 new tests in `tests/test_entry_selection.py`.
- `src/pnl_prometheus_format.py`, the Prometheus exposition renderer that used to
  be one 308-line function inside `src/export_pnl_metrics.py`. Gauge and
  labelled-gauge helpers, one section per metric family (trades, portfolio, AI
  co-pilot, AI shadow suggestions, recent-PnL guard, per-coin) and proper label
  escaping replace the single flat writer, and the module has no I/O so it can be
  reused by both PnL exporters. Covered by 24 new tests in
  `tests/test_pnl_prometheus_format.py`, including the invariant that every
  emitted series also declares its HELP and TYPE header.
- `src/pnl_metrics.py`, the trade-journal aggregator that used to be the 141-line
  `calculate_pnl_metrics()` in `src/export_pnl_metrics.py` (cyclomatic complexity
  25, the last function above the lint threshold there). A `PnLAccumulator` folds
  one journal row at a time — buy volume, win/loss/break-even counts, per-coin
  maps, the peak-to-trough equity drawdown and the sample Sharpe ratio — and
  `metrics()` renders the report the renderers consume. Like the formatter, the
  module performs no I/O, so both PnL exporters share it. Covered by 26 new tests
  in `tests/test_pnl_metrics.py`, including the malformed-row paths and the
  quirk that a naive journal timestamp still contributes to all-time PnL.
- The remaining readers in `src/export_pnl_metrics.py` are now four focused
  modules, each with its own tests: `src/ai_copilot_log.py` (the co-pilot log
  parser — the three suggestion markers, the recency tail and the rank limit,
  split out of the 81-line `read_ai_shadow_suggestions()`, complexity 15),
  `src/recent_pnl_guard.py` (the flat `recent_pnl_guard_*` gauges the exporters
  publish, split out of the 65-line `compute_recent_pnl_guard_state()`,
  complexity 18; the bot's own journal guard in `src/journal_guards.py` is a
  different shape and stays separate), `src/env_file.py` (the four `.env`
  readers the exporters use instead of the process environment) and
  `src/exporter_paths.py` (the four deployment paths, which both exporters used
  to declare separately).
- `tests/test_ai_copilot_log.py` covers the co-pilot log reader, including the
  clock handling: the bot writes log timestamps with `logging.Formatter`'s naive
  `asctime`, while `read_ai_shadow_suggestions()` subtracts them from
  `datetime.now(UTC)`, so parsing a real co-pilot line raised `TypeError` that
  the narrow `except (IndexError, SyntaxError, ValueError)` did not catch. It was
  masked until the bare `except Exception` was narrowed. The extraction itself
  left that behaviour untouched; the defect is fixed separately below.
- `src/execution_service.py`, the live order-execution collaborator that used to
  be four blocks inside `src/trading_bot.py`: the fill-details validator
  (`order_fill_details`), the fill-confirmation state machine
  (`confirm_order_fill`, with the terminal-status set now a named constant), the
  real-money market-order path (`execute_real_trade` plus its buy and sell
  halves) and the candidate entry loop (`open_new_positions`). The ccxt exchange
  is injected and every piece of bot-owned state — portfolio, config,
  performance counters, journal, cooldown and balance bookkeeping — arrives as an
  argument or through the `RealTradeHooks` / `EntryHooks` callbacks, so the money
  paths are now unit-testable against a stub exchange without importing the bot.
  Covered by 26 new tests in `tests/test_execution_service.py`.
- `.github/skills/extract-module-refactor/`, a skill that records how the
  extractions above are done so the next one follows the same recipe: the
  verbatim-copy and thin-delegator rules, the layer registration and line-count
  ratchet, the full local gate list, and a reference for the equivalence
  harness. It bundles `scripts/complexity_report.py` (cyclomatic-complexity
  report used to pick extraction targets) and `scripts/ci_env_sim.py` (runs
  pytest with `talib`/`torch`/`transformers`/`onnx` hidden, reproducing the slim
  CI test job locally).
- `src/exit_policy.py`, the exit-decision collaborator that used to be eight
  methods inside `src/trading_bot.py`: the dynamic stop-level maths
  (`effective_stop_loss_level` with trailing stop and break-even protection), the
  two trade-class predicates (`is_rules_uptrend_trade`,
  `is_downtrend_reversal_trade`), the partial take-profit sell
  (`maybe_partial_take_profit`) and the four ranked exit-reason checks
  (`partial_tp_remainder_exit_reason`, `downtrend_reversal_exit_reason`,
  `uptrend_rules_exit_reason`, `core_exit_reason`). The config and portfolio are
  injected and the bot-owned pieces — the trade executor, the public-paper-mode
  probe and the clock — arrive as arguments or callbacks, so every exit branch is
  now unit-testable without constructing a bot. Covered by 41 new tests in
  `tests/test_exit_policy.py`.
- `src/entry_filters.py`, the five entry gates that used to be methods inside
  `src/trading_bot.py`: `passes_entry_momentum_filter` (short-term momentum,
  EMA(20) alignment and the sharp-pump screen), `passes_uptrend_entry_filter`,
  `passes_downtrend_reversal_filter`, `passes_fallback_entry_filter` and
  `entry_volatility_guard` (ATR(14)/price). Each gate is a pure
  `(config, coin_data) -> (bool, reason)` predicate with the config injected, so
  every rejection reason and per-coin override is unit-testable without a bot,
  an exchange or a model. Covered by 72 new tests in `tests/test_entry_filters.py`.
- `src/coin_analysis.py`, the per-coin analysis cluster that used to be five
  methods inside `src/trading_bot.py`: `analyze_coin` (the whole indicator,
  rule-signal and model-overlay path for one coin, previously cyclomatic
  complexity 31), `apply_ml_signal` (the optional LLM predictor and its ensemble
  weaving), `build_tabular_features` (the 22-column CatBoost/Hailo feature row),
  `should_apply_tabular_signal` (the rule-vs-model source gate with its seven
  gate reasons) and `recommendation_bias`. The config, the OHLCV fetcher, both
  predictors and the rule recommender are injected, so the complete analysis
  path — including the sell veto, the gated-signal diagnostics and the Hailo
  failure suppression — is unit-testable without an exchange, a database or a
  model. Covered by 36 new tests in `tests/test_coin_analysis.py`.
- `src/bot_bootstrap.py`, the startup wiring that used to be inline in
  `CryptoTradingBot.__init__()` (cyclomatic complexity 30):
  `validate_startup_config` (the two Kraken-simulation / public-paper start-up
  errors), `log_startup_details` (the run banner and the DRY-RUN execution-cost
  line), `load_ml_predictor` (the optional LLM predictor with its import-failure
  fallback) and `load_tabular_predictor` (the CatBoost model, or the remote
  Hailo backend, with the same fallback). The logger and the config are
  injected, so the whole start-up contract — including which errors abort the
  process and which are swallowed into rule-only mode — is unit-testable
  without an exchange. Covered by 31 new tests in
  `tests/test_bot_bootstrap.py`.
- `src/ai_copilot_service.py`, the AI co-pilot cluster that used to live in
  `CryptoTradingBot`: `estimate_ai_cost_usd` (the per-call USD cost),
  `can_run_ai_copilot` (the nine gate reasons - disabled, missing model,
  missing API key, error suspension, daily and monthly call quotas, monthly
  budget quota, interval not reached and invalid attempt timestamp),
  `ai_copilot_snapshot` (the journal window, per-coin aggregates, top
  winners/losers and the performance, portfolio and guardrail summary with its
  reset-on-error contract), `extract_json_object`, `call_ai_copilot` (the HTTP
  call and its token usage), `clamp_ai_changes` (the min/max guardrail clamp)
  and `maybe_run_ai_copilot_variant` (the full orchestration shared by the
  live and benchmark variants: quota counters, budget cap applied after the
  call, no-result / no-change / shadow / applied logging and error
  suspension). The config, logger, portfolio and performance summary are
  injected, so the complete guardrail matrix is unit-testable without an
  exchange, an API key or a network. Covered by 72 new tests in
  `tests/test_ai_copilot_service.py`.
- `src/dry_run_execution.py`, the simulated (dry-run / paper) execution cluster
  that used to live in `CryptoTradingBot`: `execute_dry_run_trade` (the cost-mode
  aware entry point), `plan_dry_run_fill` (the slippage/fee fill maths and its
  invalid-request guards), `dry_run_buy` and `dry_run_sell` (the simulated
  accounting, journal row and persistence branches), plus
  `resolve_dry_run_sell_entry`, `dry_run_sell_proceeds`,
  `apply_dry_run_sell_to_holdings`, `record_dry_run_sell_close`,
  `dry_run_sell_gross_net_pnl` and `update_open_trades_after_dry_run_sell`. The
  config, portfolio and performance dict are injected and everything the bot owns
  — the journal writers, the public-paper transaction commit, the close
  performance bookkeeping, the excursion metrics, the cost-basis helper, the
  cooldown timestamps and the clock — arrives through a frozen `DryRunHooks`
  dataclass, so the fill maths and the simulated accounting are unit-testable
  without constructing a bot. Covered by 39 new tests in
  `tests/test_dry_run_execution.py`.
- `src/market_analysis.py`, the market screening cluster that used to live in
  `CryptoTradingBot`: `dynamic_excluded_coins` (the lossmaker exclusion set built
  from recent sell-side underperformance - minimum sell count, cumulative loss,
  win-rate and MAX-HOLD-TIME exit ratio gates over the configured journal
  window), `select_analysis_coins` (the base-currency volume gate, the Top-N by
  volume and the "always analyse open positions" extra-coins rule) and
  `analyze_markets` (the scored per-coin analysis loop with its progress logging
  and slow-coin warning). The `BotConfig` is injected and the two pieces of bot
  state the loop touches - the dynamic exclusion set and the per-coin analyser -
  arrive as explicit callables, so the volume gate, the Top-N selection and the
  scored map are unit-testable without an exchange or a model. Covered by 28 new
  tests in `tests/test_market_analysis.py`.
- `src/trade_journal.py`, the trade-journal and paper-trade-transaction cluster
  that used to live in `CryptoTradingBot`: `init_journal` (create the journal or
  safely extend its CSV schema in place, keeping the existing file mode and
  filling legacy rows with their defaults), `sync_directory` (fsync a directory
  entry so a rename survives a power cut), `journal_row` (the two-stage default
  filling a journal row gets before it is written), `append_trade_journal`,
  `recover_paper_trade` and `commit_paper_trade` (the write-ahead intent file,
  checksum and offset handshake that makes a public-paper trade either fully
  committed or fully rolled back after a crash) and
  `recommend_tabular_threshold` (the auto-tune threshold sweep over the journal).
  The `BotConfig` is injected and everything else the cluster touches — the
  analytics writer, the run id and config hash, the journal schema and row
  formatter — arrives through a frozen `JournalContext` dataclass, while the
  public-paper sync/recover/failure-flag hooks arrive through `PaperTradeHooks`,
  so the schema migration, the crash recovery and the threshold sweep are
  unit-testable without an exchange or a portfolio. The `Portfolio` stays with
  the bot and is passed explicitly, because the tests that build a partial bot
  have no portfolio of their own. Covered by 56 new tests in
  `tests/test_trade_journal.py`.
- `src/open_trade_manager.py`, the open-position sweep that used to be
  `CryptoTradingBot._manage_open_trades()` (cyclomatic complexity 17): the
  per-position loop that maintains the peak/trough excursions, fetches the ATR,
  derives the dynamic stop, take-profit and partial-take-profit levels, warns
  when the hold limit is overdue, runs the ranked exit-reason chain and sells,
  retries and removes the position. The `BotConfig` and the `Portfolio` arrive
  through a frozen `OpenTradeContext`, and the three collaborators the sweep
  calls but does not own — the ATR cache, the trade executor and the
  public-paper-mode probe — arrive through `OpenTradeHooks`. The exit policy is a
  layer-1 peer, so the chain is called directly instead of bouncing back through
  the bot. The whole sweep is therefore unit-testable without an exchange, a
  portfolio implementation or a bot. Covered by 23 new tests in
  `tests/test_open_trade_manager.py`.
- The entry-candidate sweep that used to sit in `TradingBot._select_entry_candidates`
  now lives in `src/entry_selection.py` next to the pure ranking rules it already
  delegates to: `select_entry_candidate_shortlist()` (occupied positions, free
  slots, the dynamic lossmaker/source gates, the recent-PnL guard report and the
  empty-shortlist diagnostics), `entry_guards_allow()` (the tabular SELL veto and
  the ATR/price volatility proxy with its per-iteration cache) and
  `log_no_entry_candidates()` (signal mix, blocked-gate diagnostics and the
  per-coin detail lines). The bot-owned state and collaborators are passed in as
  two frozen dataclasses, `EntryCandidateContext` (config, portfolio) and
  `EntryCandidateHooks` (the twelve bot methods the sweep calls), so the whole
  sweep is unit-testable without an exchange or a live bot.
- `src/portfolio_bootstrap.py`, the portfolio startup path that used to be five
  `CryptoTradingBot` methods plus two module helpers: `update_portfolio_balance()`
  (the mode dispatcher), `initialize_simulation_portfolio_from_kraken()` (the
  one-time Kraken free-EUR bootstrap, the restart restore with its iteration bump
  and the fail-closed guards for corrupt or invalid persisted state),
  `initialize_simulated_portfolio()` (restore or seed the simulated/offline
  capital), `initialize_dry_run_portfolio()` (restore from state, else seed once
  from the exchange) and `refresh_live_portfolio_balance()`, together with the
  `currency_aliases()` / `extract_cash_and_holdings()` balance helpers. The config,
  portfolio and exchange arrive through a frozen `PortfolioBootstrapContext`, and
  the three things the bootstrap reads or writes on the bot — the public-paper-mode
  probe, the paper-trade recovery and the iteration counter — arrive through
  `PortfolioBootstrapHooks`, so the whole startup path is unit-testable without a
  live bot. Covered by 40 new tests in `tests/test_portfolio_bootstrap.py`.
### Changed

- `src/trading_bot.py` shrank from 2237 to 2120 lines now that the portfolio
  startup path lives in `src/portfolio_bootstrap.py`. `_update_portfolio_balance()`
  stays a thin delegator with its previous name — it is called from the run loop,
  from `src/init_kraken_paper.py` and as the `update_portfolio_balance` hook of the
  public-paper dashboard — and builds a `PortfolioBootstrapContext` from
  `self.config` / `self.portfolio` / `self.exchange` plus a `PortfolioBootstrapHooks`
  from `_public_paper_mode()`, `_recover_paper_trade()` and the `iteration`
  attribute, so tests that patch those bot attributes still take effect. The four
  sibling initializers and the two balance helpers lost their delegators: nothing
  outside the startup path called them, and `_currency_aliases` /
  `_extract_cash_and_holdings` had no users outside the two methods. `_initialize_exchange()`
  and `_load_market_info()` deliberately stayed in the class because tests patch
  them at class level. The architecture ceiling was ratcheted from 2237 to 2120
  lines. The old and new code were compared over 21 scenarios (the first Kraken
  bootstrap, a restart that restores state and bumps the iteration, a zero balance,
  a negative / `NaN` / missing Kraken balance, an unreachable exchange, a corrupt
  and an invalid persisted state file, public-market paper data without a portfolio,
  missing read-only credentials, an already initialized portfolio, the simulated
  seed with existing cash / holdings / a preinitialized value, a dry-run restore,
  dry-run seeding from the exchange, a failing dry-run exchange call, the live
  refresh with `ZEUR`/`XBT` aliases, a live network error and a BTC base currency)
  and produced byte-identical portfolio state, exchange calls, files written,
  raised exceptions and log lines. All seven moved functions are also
  AST-identical to the original after the documented `self.config` → `config`,
  `self.exchange` → `context.exchange`, `self.iteration = …` → `hooks.set_iteration(…)`
  and `logger` → `log` rewrites.
- The `mypy` type check is clean: all 11 remaining `arg-type` errors across seven
  modules are gone and `mypy src/` now exits 0 (it previously reported 11 errors in
  7 files). Ten of them were `float()` calls on values from untyped `dict.get()` /
  `object` sources whose `TypeError` path is already handled by a surrounding
  `try`/`except` or an explicit `None` guard; those arguments now carry a
  `cast(Any, ...)` (a no-op at runtime) so the checker accepts them without any
  change to control flow or messages. The eleventh was `torch.onnx.export()` in
  `src/train_hailo_model.py`, which is typed to take a `tuple` of inputs: the
  example tensor is now wrapped in a one-element tuple, which produces a
  byte-identical ONNX graph (verified by hashing the export output). No behaviour
  changed — the full suite, the CI environment simulation and the Hailo end-to-end
  export test all pass as before.
- `src/trading_bot.py` shrank from 2375 to 2237 lines now that the entry-candidate
  sweep lives in `src/entry_selection.py`. `_select_entry_candidates()` and
  `_entry_guards_allow()` stay as thin delegators with their previous signatures —
  the delegator builds the `EntryCandidateContext` / `EntryCandidateHooks` from the
  bot's own bound methods, so tests that patch `bot._entry_volatility_guard()`,
  `bot._passes_uptrend_entry_filter()` or `bot._entry_market_mode()` still take
  effect — and the blocked-gate delegators (`_log_blocked_uptrend_candidates()`,
  `_log_blocked_downtrend_reversal_candidates()`, `_log_fallback_entry_diagnostics()`)
  stay because they are also called from the entry-execution path. The
  `_log_no_entry_candidates()` delegator was removed: nothing outside the sweep
  called it. The now-unused `_entry_select_candidates` import alias and the
  `Counter` import were dropped from the bot. The architecture ceiling was
  ratcheted from 2375 to 2237 lines and `entry_selection.py` got its own ceiling of
  750 lines. The old and new code were compared over 16 scenarios (strict winners
  with occupied positions, the recent-PnL guard withholding non-catboost candidates,
  fallback activation, force-fill, defensive-mode suppression, fallback disabled,
  an empty shortlist with blocked uptrend / downtrend-reversal / fallback-RSI gates,
  a tabular SELL veto, an unavailable and a below-minimum volatility proxy, dynamic
  lossmaker pair exclusions, degraded signal sources, no free slots, an empty market
  analysis and a seven-coin cautious mix) and produced byte-identical shortlists and
  log lines. All three moved functions are also AST-identical to the original after
  the documented `self.config` → `context.config`, `self.portfolio` →
  `context.portfolio`, `self._x()` → `hooks.x()` and `logger` → `log` rewrites.
- `src/public_paper_dashboard.py` shrank from 1075 to 695 lines now that the
  market panel lives in `src/market_observations.py`. The `/api/market-observations`
  route is a thin delegator that calls `load_market_observations()`, and the
  helpers the snapshot code still uses (`_parse_utc`, `_number`) plus the two
  field tuples are re-imported, so the dashboard keeps its public shape and the
  existing dashboard tests still apply — only the test that patches the bounded
  read window had to target the module that now owns the limit. The architecture
  ceiling was ratcheted from 1100 to 695 lines and `market_observations` was
  registered as a layer-1 module; `docker/public-paper-dashboard.Dockerfile`
  copies the new file because the image is assembled file by file. The old and
  new code were compared over 32 scenarios (missing directory, missing files,
  malformed/oversized metadata, bad exchange and symbol lists, duplicate
  symbols, unexpected CSV headers, partial last lines, a read window with no
  complete row, invalid ticker and candle rows, window-boundary rows, unsorted
  rows, all-rows-outside-the-window fallback, zero spread/volume, fresh and stale
  data, every 404 symbol and the full multi-symbol payload) and produced
  byte-identical JSON payloads and log lines. The twelve moved statements and the
  fifteen statements that stayed are also AST-identical to the original.
- `src/trading_bot.py` shrank from 2589 to 2375 lines now that the open-position
  sweep lives in `src/open_trade_manager.py`. `_manage_open_trades()` is a thin
  delegator with the previous signature, so the trade loop, the ATR cache, the
  executor and the portfolio stay with the bot and the existing tests that patch
  `bot._get_atr_for_coin()` / `bot._execute_trade()` and call
  `bot._manage_open_trades()` still apply. The five exit-policy delegators that
  only this loop used (`_maybe_partial_take_profit()`,
  `_partial_tp_remainder_exit_reason()`, `_downtrend_reversal_exit_reason()`,
  `_uptrend_rules_exit_reason()`, `_core_exit_reason()`) and their now-unused
  import aliases were removed; `_effective_stop_loss_level()`,
  `_is_rules_uptrend_trade()`, `_is_downtrend_reversal_trade()` and
  `_public_paper_mode()` stay, because other bot code and tests still call them.
  The architecture ceiling was ratcheted to the new size and `open_trade_manager`
  was registered as a layer-1 module. The old and new code were compared over 44
  scenarios (missing market data, new peak / lowered trough / unchanged
  excursions, dry-run and live persistence, NaN and zero ATR, the global and
  per-coin hold limits, every partial-take-profit gate including the
  public-paper bookkeeping and a failed partial sell, stop-loss, take-profit,
  trailing and break-even exits, a failed sell, a partial fill below and at the
  original amount, every exit-reason precedence winner and the multi-position
  sweep) and produced identical log lines, hook call order, removals,
  `save_state()` counts and resulting trade state. The moved body is also
  AST-identical to the original after the documented `self.config` → `config`,
  `self.portfolio` → `portfolio` and hook rewrites.
- `src/market_observations.py`, the market panel of the read-only dashboard that
  used to sit inline in `src/public_paper_dashboard.py`: the recorder file
  readers (`_parse_utc`, `_number`, `_read_csv_tail` with its bounded 16 MiB
  read window), the metadata/CSV validation and availability fallbacks, the
  per-symbol quality summary (`_valid_rows`, `_build_market_summary`) and the
  chart downsampling (`_downsample`). The module is a layer-1 peer of the
  dashboard, so it reads `DASHBOARD_MARKET_DATA_DIR` itself and raises
  `HTTPException(404)` for a symbol that is not part of the recording; the route
  keeps authentication, caching and response shape. Covered by 62 new tests in
  `tests/test_market_observations.py`.
- The README header now follows the GitHub colour scheme: `assets/logo-dark.png`
  (the same emblem in light strokes, 896x1200) is served to dark mode and the
  dark-stroke transparent emblem to light mode through a `<picture>` element, so
  the logo stays readable on both backgrounds. The display width is declared per
  source because the dark artwork has more canvas padding around the emblem; the
  two values render the scales at the same visual size.
- The project logo is now a single balanced-scale emblem (Kraken/BTC on one pan,
  the risk shield with a confirmed uptrend in the middle, the model graph on the
  other) at 1376x768 with a transparent background; the previous 353x220 artwork
  is kept as `assets/old-logo-transparent.png` for reference. `README.md` also
  gained a **Monitoring Dashboard** section with a screenshot of the read-only
  paper-trading dashboard (`assets/dashboard-preview.png`).
- `read_ai_copilot_usage()` now exists once, in `src/export_pnl_metrics.py`. The
  HTTP exporter's near-identical 50-line copy is gone and
  `pnl_exporter.MetricsHandler` delegates to the shared implementation, which
  gained `quiet=` to suppress the stderr diagnostics and `backup_state_path=` to
  fall back to the `.bak` state file; the exporter keeps its own AI spend lookup
  on top. The dead `MIN_DRAWDOWN_PCT_BASE_USD` and `SHADOW_SUGGESTION_META_RE`
  constants in `src/pnl_exporter.py` were removed with it.
- The rendered exposition text is byte-identical to before: the previous
  formatter and the new module were diffed over five scenarios (full HTTP
  metrics, a one-shot run without freshness keys, empty coin maps, a guard
  reason that needs escaping, and an infinite profit factor).
- `src/export_pnl_metrics.py` shrank from 1024 to 731 lines and
  `src/pnl_exporter.py` from 281 to 246; the architecture ceiling for the
  former was ratcheted to the new size.
- `src/export_pnl_metrics.py` shrank further, from 731 to 559 lines, now that the
  aggregator lives in `src/pnl_metrics.py`; it re-exports
  `calculate_pnl_metrics`, so `pnl_exporter` and the CLI entry point are
  untouched, and the ceiling was ratcheted again. The two implementations were
  compared over 400 randomised journals (including NaN/inf PnL, naive and
  unparseable timestamps, and unknown actions) and produced identical reports.
- `src/export_pnl_metrics.py` shrank once more, from 559 to 366 lines, and is now
  only the journal/portfolio log readers plus the one-shot CLI. It re-exports
  `read_ai_shadow_suggestions`, `compute_recent_pnl_guard_state` and
  `_read_env_value`, so `pnl_exporter` and the CLI entry point are untouched;
  `pnl_exporter` now imports the moved helpers from their own modules and takes
  the deployment paths from `src/exporter_paths.py` instead of redeclaring them.
  The old and new readers were compared over 400 randomised log and `.env`
  fixtures plus 5,748 individual assertions (all three suggestion markers,
  malformed lines, the tail limit, quoted and unparsable env values, and every
  guard branch) with no differences.
- The duplicated uptrend and downtrend-reversal candidate loggers are now one
  `log_blocked_gate_candidates()` helper with a gate label, and the
  no-candidates summary moved to its own `_log_no_entry_candidates()` method.
  Cyclomatic complexity of `_select_entry_candidates()` dropped from 52 to 13,
  and `src/trading_bot.py` shrank from 4581 to 4306 lines.
- `src/trading_bot.py` shrank from 4306 to 4026 lines now that the execution
  logic lives in `src/execution_service.py`. `_confirm_order_fill()`,
  `_execute_real_trade()` and `_open_new_positions()` are thin delegators with
  the previous signatures, `_order_fill_details` is re-exported from the new
  module, and the risk gates, position sizing, momentum filter and cooldown
  bookkeeping stay with the bot — so the existing tests that patch bot methods
  still apply. The architecture ceiling was ratcheted to the new size and
  `execution_service` was registered as a layer-1 module. The old and new code
  were compared over 15 scenarios (partial and full sells, an unconfirmable
  fill, the exchange minimum, all five ccxt error classes, every entry-loop
  blocked reason, the guardrail / max-trades / insufficient-cash branches) and
  produced identical log lines, hook call order and portfolio/performance state.
  The entry loop no longer re-assigns `available_funds_for_trade` and
  `current_open_trades_count` after each buy; both are only read by the outer
  log branches, which are evaluated before the loop, so the values were dead.
- `src/trading_bot.py` shrank from 4026 to 3931 lines now that the exit policy
  lives in `src/exit_policy.py`. `_effective_stop_loss_level()`,
  `_is_rules_uptrend_trade()`, `_is_downtrend_reversal_trade()`,
  `_maybe_partial_take_profit()`, `_partial_tp_remainder_exit_reason()`,
  `_downtrend_reversal_exit_reason()`, `_uptrend_rules_exit_reason()` and
  `_core_exit_reason()` are thin delegators with the previous signatures, so the
  trade loop, the ATR cache and the journal stay with the bot and the existing
  tests that call bot methods still apply. The architecture ceiling was
  ratcheted to the new size and `exit_policy` was registered as a layer-1 module.
  The old and new code were compared over 53 scenarios (the base / trailing /
  break-even stop branches, missing and non-positive buy prices, every
  partial-take-profit gate including the public-paper and live bookkeeping, all
  remainder timeout and weak-signal signals, the full downtrend-reversal and
  uptrend-rules hold/PnL/signal matrices with their per-coin overrides, and every
  core stop-loss, take-profit, max-hold and signal branch) and produced identical
  return values, log lines and portfolio state.
- `src/trading_bot.py` shrank from 3931 to 3772 lines now that the entry gates
  live in `src/entry_filters.py`. `_passes_entry_momentum_filter()`,
  `_passes_uptrend_entry_filter()`, `_passes_downtrend_reversal_filter()`,
  `_passes_fallback_entry_filter()` and `_entry_volatility_guard()` are thin
  delegators with the previous signatures, so the candidate shortlist, the
  diagnostics logs and the `src/entry_selection.py` helpers stay with the bot and
  the existing tests that call bot methods still apply. The architecture ceiling
  was ratcheted to the new size and `entry_filters` was registered as a layer-1
  module. The old and new code were compared over 439 scenarios (every
  recommendation, missing/NaN/inf metric, per-coin override, allow-list and
  disabled-flag branch of all five gates) and produced identical return values
  and log output; the moved bodies are also AST-identical apart from the
  `self.config` to `config` rename.
- `src/trading_bot.py` shrank from 3772 to 3476 lines now that the per-coin
  analysis lives in `src/coin_analysis.py`. `_analyze_coin()`, `_apply_ml_signal()`,
  `_build_tabular_features()`, `_should_apply_tabular_signal()` and
  `_recommendation_bias()` are thin delegators with the previous signatures, so
  the bot still owns the config, the predictors and the OHLCV fetch, and the
  existing tests that call bot methods or patch
  `src.trading_bot.get_rule_recommendation` still apply. The architecture ceiling
  was ratcheted to the new size and `coin_analysis` was registered as a layer-1
  module. The old and new code were compared over 2,068 scenarios (every rule
  label, all seven gate reasons and gate configurations, six OHLCV shapes from a
  single row to NaN volume through the feature builder, the full ML decision /
  confidence / starting-recommendation matrix, and the end-to-end analysis across
  nine tabular-predictor and config combinations including the Hailo failure
  path) and produced identical return values, raised exceptions and log output;
  the moved bodies are also AST-identical apart from the injected collaborators.
- `src/trading_bot.py` shrank from 3476 to 3416 lines now that the start-up
  wiring lives in `src/bot_bootstrap.py`. `__init__()` keeps the exchange,
  market-info, journal, paper-trade recovery, simulation-portfolio and
  auto-tune steps — everything the existing tests patch — and only delegates
  the two config validations, the start-up logging and the two predictor
  loads. The architecture ceiling was ratcheted to the new size and
  `bot_bootstrap` was registered as a layer-1 module. The old and new code were
  compared over 16 start-up scenarios (both validation errors and their
  boundary combinations, the banner with both cost modes, the ML and CatBoost
  loads with success, six swallowed error types and a propagating one, and the
  Hailo backend with its non-swallowed failures) and produced identical
  exceptions, log lines and bot attribute state; the moved bodies are also
  AST-identical apart from the `self.config` to `config` and
  `self.run_id`/`self.config_hash` to local renames.
- `src/trading_bot.py` shrank from 3416 to 3001 lines now that the AI co-pilot
  cluster lives in `src/ai_copilot_service.py`. The eight bot methods keep
  their names and signatures as thin delegators, and
  `_maybe_run_ai_copilot_variant()` keeps the empty-path fallback to
  `config.ai_copilot_state_file` that the old state helpers provided, so the
  existing tests that call bot methods or patch the co-pilot state files still
  apply. The architecture ceiling was ratcheted to the new size and
  `ai_copilot_service` was registered as a layer-1 module. The old and new
  code were compared over 81 scenarios (all nine gate reasons and their
  boundary combinations, the cost estimator, the JSON extractor with malformed
  payloads, the clamp matrix across min/max and non-numeric values, the
  snapshot with a populated journal, an empty journal and a corrupt CSV, and
  the full variant orchestration including corrupt state recovery, quota
  accumulation across repeated calls, budget suspension and the empty state
  file fallback) and produced identical return values, config mutations, state
  files, raised exceptions and log output; the moved bodies are also
  AST-identical apart from the injected collaborators.
- `src/trading_bot.py` shrank from 3001 to 2884 lines now that the simulated
  execution cluster lives in `src/dry_run_execution.py`. The ten bot methods keep
  their names and signatures as thin delegators and `_dry_run_hooks()` builds the
  injected callbacks per call, so the existing tests that drive
  `_execute_trade()` or patch a bot method still apply. The architecture ceiling
  was ratcheted to the new size and `dry_run_execution` was registered as a
  layer-1 module. The old and new code were compared over six end-to-end
  scenarios driven through `_execute_trade()` on a real bot (costs on and off,
  zero fee/slippage, public-paper mode, a restart that switches cost tracking on
  under a legacy position, and seeded edge positions - missing basis, corrupt
  quantity, NaN buy price, negative basis, repeated partial take-profits),
  covering 38 trade calls, 122 log lines and 25 journal rows, and produced
  identical return values, log output, journal rows and portfolio/performance
  state; the moved bodies are also AST-identical apart from the injected
  collaborators.
- `src/trading_bot.py` shrank from 2884 to 2776 lines now that the market
  screening cluster lives in `src/market_analysis.py`. `_analyze_markets()` and
  `_dynamic_excluded_coins()` keep their names and signatures as thin delegators
  and pass their own bound methods as the injected callables, so the existing
  tests that patch or call a bot method still apply. The architecture ceiling was
  ratcheted to the new size and `market_analysis` was registered as a layer-1
  module. The old and new code were compared over 18 scenarios driven through
  `_analyze_markets()` and `_dynamic_excluded_coins()` on a real bot (default
  screening, Top-N trimming, the volume gate, no filtering, static exclusions,
  a lossmaker journal with and without the feature flag, journal disabled,
  missing, unreadable, header-only, missing columns, other exit reasons, window
  and threshold variations, an empty market feed, extra coins only and a forced
  slow-analysis warning), covering 172 log lines, and produced identical return
  values, exclusion sets and log output; the moved bodies are also AST-identical
  apart from the injected collaborators.
- `src/trading_bot.py` shrank from 2776 to 2589 lines now that the trade journal
  and paper-trade transaction cluster lives in `src/trade_journal.py`.
  `_init_trade_journal()`, `_sync_directory()`, `_recover_paper_trade()`,
  `_commit_paper_trade()`, `_journal_row()`, `_append_trade_journal()` and
  `_recommend_tabular_threshold_from_journal()` keep their names and signatures
  as thin delegators, `_journal_context()` and `_paper_trade_hooks()` build the
  injected dataclasses per call, and `_journal_fields` stays on the bot, so the
  existing tests that patch or call a bot method, drive a public-paper trade or
  inspect the journal file still apply. The architecture ceiling was ratcheted to
  the new size and `trade_journal` was registered as a layer-1 module. The old
  and new code were compared over 58 scenarios driven through the bot
  (`init_journal` on a missing, header-only, legacy-schema, extra-column and
  unreadable file, the full commit/recover handshake with a clean commit, a
  restart recovery, a stale intent file and every tampered envelope field,
  journal writes with the analytics writer enabled and disabled, the append and
  commit failure paths, and the threshold sweep across empty, short, unreadable
  and tied journals), covering 118 log lines, and produced identical return
  values, raised exceptions, journal bytes, intent files and analytics rows; the
  moved bodies are also AST-identical apart from the injected collaborators.

### Fixed

- `tests/test_public_paper_dashboard.py` failed whenever the suite ran within two
  hours of UTC midnight. Its fixture stamped the two journal rows at
  `now - 2h` / `now - 1h`, but `load_dashboard_data()` buckets rows with
  `stamp.date() == datetime.now(UTC).date()`, so shortly after midnight the
  "two hours ago" row belongs to the previous UTC day and `today_buys` came back
  as 0. The fixture now derives both timestamps from the elapsed part of the
  current UTC day, so they stay ordered, in the past and inside today's bucket at
  any hour.
- `read_ai_shadow_suggestions()` crashed on every real co-pilot log line. The bot
  writes `asctime` timestamps in local time with no offset, and the reader
  subtracted them from `datetime.now(UTC)`, so the `TypeError` escaped the narrow
  `except (IndexError, SyntaxError, ValueError)` and the whole exporter request
  failed — one malformed-looking line in `bot.log` took the PnL endpoint down.
  `_parse_log_timestamp()` now interprets the naive timestamp as local time and
  converts it to UTC, the same assumption `pnl_exporter` already makes when it
  turns a log timestamp into a unix time. The defect reached `main` when the
  bare `except Exception` around the parser was narrowed and shipped in
  v1.0.25-v1.0.27; the pinned test that documented it is now a regression test
  that asserts the age is computed correctly.
- `fetch_ohlcv_with_retry()` slept for the back-off delay after its final
  attempt, so an unreachable exchange stalled for an extra 20 seconds per
  symbol before giving up.
- `calculate_indicators()` only caught `TypeError` and `ValueError`, so a frame
  missing the `high`/`low`/`volume` columns escaped as an unhandled `KeyError`
  instead of returning the documented `False`.
- `process_symbol()` split the pair with `symbol.split("/")[0]`, which raised
  `IndexError` for a symbol without a quote currency; it now uses `partition()`.

## v1.0.27 - 2026-11-24

### Fixed

- The cluster archive now ships the generic `cluster-starter/` two-node CM5/K3s
  starter, which `SERVER_README.md` and the `docs/` guides link. The published
  `trading-cluster-<version>.tar.gz` previously omitted the whole tree, so those
  links resolved to nothing after unpacking. The same allowlist as the standalone
  starter pre-release applies: tracked example inventories plus the
  `playbooks/`, `roles/` and `tests/` trees, never real inventories, vault files
  or dot-files, and the ansible credential scan now covers the starter YAML too.
- The Pi archive now ships the analytics PostgreSQL backup wrappers and their
  service/timer units that `SERVER_README.md` tells operators to install, so the
  instructions no longer reference files the package does not contain. The
  wrappers exit rather than write when the expected NAS mount or the PostgreSQL
  container is absent, so a single-host install can leave the timers disabled.
- `pnl-exporter.service` moved from the repository root into `deploy/`, matching
  every other systemd unit. The Pi installer already copied it from `deploy/`, and
  the release workflow had to relocate it as a special case; the cluster archive
  was missing the unit entirely.

### Added

- The `cluster-starter/` unit tests now run in the CI quality job instead of only
  in the manually triggered starter pre-release workflow.

## v1.0.26 - 2026-11-24

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
