# Changelog

Notable changes are recorded here. Versions are assigned when a release is
prepared; this file intentionally remains under **Unreleased** until then.

## Unreleased

### Added

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

- Extend existing CSV journals atomically with the new columns and mark
  historical rows as `legacy`.
- Add the new trade-event columns to PostgreSQL schemas, with additive
  migrations for existing tables.
- Keep confidence-threshold selection chronological: select on the first 70%
  of trades and report on the final 30% holdout.
- Make the example backtester use next-bar execution, chronological multi-coin
  prices, and configurable fee/slippage assumptions.

### Safety and rollout

- No entry/exit thresholds or PnL-guard behavior were changed by these
  diagnostics and analysis updates.
- Hailo support is preparation-only: no trading HEF is compiled, no cluster
  service is deployed, and the backend is not enabled by default.
- The historical replay is offline-only and does not change live trading
  behavior; its holdout results are not a profitability guarantee.
- Run in dry-run/simulation first and compare results by `run_id` and
  `config_hash`; these reports do not guarantee profitability.
