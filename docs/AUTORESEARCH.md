# AutoResearch signal bridge

## AutoResearch -> AI model features

The existing "AutoResearch" bridge is a generic market-signal JSON adapter for
CatBoost; it is **not** an integration with
[karpathy/autoresearch](https://github.com/karpathy/autoresearch). That project
experiments with language-model training and does not produce this JSON. The
bridge does not train or improve the bot. Its research features are not part
of the fixed Hailo trading-model contract.

CatBoost inference can consume a normalized research signal from a separate
compatible producer. The bridge preserves the producer's top-level sentiment,
confidence, and risk values (converting them to numbers, but not clamping them)
and writes the clamped model features under `normalized_features`. Inference
derives its model features from the top-level values; training uses the six
time-aligned `research_*` columns in the historical training CSV, if all are
present. Training does not read the latest signal JSON or copy it across
historical rows.

Minimum producer input JSON (example path: `data/research_signal_raw.json`):

```json
{
  "timestamp_utc": "2026-04-18T09:00:00Z",
  "sentiment_score": 0.28,
  "confidence": 0.73,
  "risk_score": 0.31,
  "market_regime": "bull",
  "citations": [
    "https://example.com/source-1",
    "https://example.com/source-2"
  ]
}
```

Mapped model features:

- `research_sentiment_score` in `[-1, 1]`
- `research_confidence` in `[0, 1]`
- `research_risk_score` in `[0, 1]`
- `research_regime_bull` / `research_regime_bear` / `research_regime_sideways`

Implementation points:

- Shared normalization logic: `research_signal.py`
- Training integration: `train_catboost_model.py`
- Live inference integration: `predict_catboost.py` and `trading_bot.py`

### AutoResearch bridge script

Use the bridge to generate/refresh the canonical signal file for inference:

```sh
python3 scripts/update_autoresearch_signal.py \
  --command "<your-autoresearch-command>" \
  --source data/research_signal_raw.json \
  --output data/research_signal_latest.json
```

If your AutoResearch command writes directly to the output path, you can omit `--source`.

The bridge:

- Runs your AutoResearch command (optional)
- Validates payload age using `timestamp_utc`
- Writes clamped values and regime indicators in `normalized_features`
- Writes canonical JSON used by inference

Only the nested `normalized_features` values are clamped to model-safe ranges.
The top-level `sentiment_score`, `confidence`, and `risk_score` remain
unclamped, and inference normalizes those values when it derives its feature
vector. The canonical output also includes `normalized_features` and
`integration` metadata; these are added by the bridge and need not be supplied
by the producer.

When `--source` differs from `--output`, the bridge copies the source to the
output **before** validation. If validation fails without neutral fallback,
the previous output may have been replaced by invalid or stale data. Preserve
any needed backup and check the command's exit status before using the output.

Fallback mode (recommended when repo/path is unknown):

```sh
python3 scripts/update_autoresearch_signal.py \
  --output data/research_signal_latest.json \
  --fallback-neutral
```

This writes a neutral signal (`sentiment=0`, `confidence=0`, `regime=sideways`) when AutoResearch is unavailable.

### Weekly automation with AutoResearch

`scripts/run_weekly_scorecard.sh` can run the bridge before scoring.
The weekly script reads these settings from its environment; it does not load
`.env` itself. Export the required settings in your shell or scheduler.

Relevant environment variables:

- `AUTORESEARCH_ENABLED=true|false` (default `false`)
- `AUTORESEARCH_REQUIRED=true|false` fail closed when the bridge fails without
  neutral fallback (default `false`). With neutral fallback enabled, a bridge
  failure can instead write a neutral signal and let scoring continue. With
  precheck enabled, strict mode runs a dry-run precheck with fallback disabled;
  that precheck can still stop the run with exit code 1 before the bridge runs.
- `AUTORESEARCH_CMD="..."` command that executes AutoResearch
- `AUTORESEARCH_SOURCE_PATH=...` JSON produced by your AutoResearch run
- `AUTORESEARCH_OUTPUT_PATH=...` canonical model signal path (default `data/research_signal_latest.json`)
- `AUTORESEARCH_MAX_AGE_MINUTES=180` freshness window
- `AUTORESEARCH_ALLOW_STALE=true|false` allow old payloads
- `AUTORESEARCH_WRITE_NEUTRAL_FALLBACK=true|false` write neutral signal when AutoResearch fails (default `true`)
- `AUTORESEARCH_PRECHECK=true|false` run setup validation before bridge (default `true`)
- `AUTORESEARCH_PRECHECK_DRY_RUN=true|false` run command/source and validate JSON contract (default `false`)
- `AUTORESEARCH_PRECHECK_TIMEOUT_SEC=90` timeout for dry-run precheck execution
- With precheck enabled, strict mode (`AUTORESEARCH_REQUIRED=true`) forces
  dry-run precheck on. `AUTORESEARCH_PRECHECK=false` skips it even in strict mode.
- `AUTORESEARCH_STRICT_ALLOW_MAINTENANCE_FALLBACK=true|false` allow one-run fallback continuation after strict precheck failure (default `false`)
- `AUTORESEARCH_MAINTENANCE_EXIT_CODE=4` dedicated exit code when maintenance override is active
- Pre-filled template: `.env.autoresearch.example`
- Strict execution template: `.env.autoresearch.active.example`

Manual precheck:

```sh
bash scripts/check_autoresearch_setup.sh
```

Manual precheck with dry-run validation:

```sh
AUTORESEARCH_ENABLED=true \
AUTORESEARCH_PRECHECK_DRY_RUN=true \
bash scripts/check_autoresearch_setup.sh
```

Maintenance override example (strict mode; continue if precheck fails):

```sh
AUTORESEARCH_ENABLED=true \
AUTORESEARCH_REQUIRED=true \
AUTORESEARCH_STRICT_ALLOW_MAINTENANCE_FALLBACK=true \
bash scripts/run_weekly_scorecard.sh
```

The override applies only when strict precheck fails: it disables the
required hard-fail for that run and enables the bridge's neutral fallback.
If the producer succeeds, its signal is still used; a neutral signal is written
only if the bridge itself fails. The scorecard then returns the configured
maintenance exit code (default `4`), regardless of the scorecard verdict.

Server example with a neutral fallback:

```sh
PYTHON_BIN=./.venv/bin/python \
AUTORESEARCH_ENABLED=true \
AUTORESEARCH_WRITE_NEUTRAL_FALLBACK=true \
bash scripts/run_weekly_scorecard.sh
```

Server example with an external research-signal command (strict). Replace the
command and source path with those of your compatible producer:

```sh
PYTHON_BIN=./.venv/bin/python \
AUTORESEARCH_ENABLED=true \
AUTORESEARCH_REQUIRED=true \
AUTORESEARCH_WRITE_NEUTRAL_FALLBACK=false \
AUTORESEARCH_CMD="<your-autoresearch-command>" \
AUTORESEARCH_SOURCE_PATH=./data/research_signal_raw.json \
bash scripts/run_weekly_scorecard.sh
```

`AUTORESEARCH_CMD` templates for an independently implemented JSON producer
(not commands provided by karpathy/autoresearch):

```sh
# Python repo (entry script)
AUTORESEARCH_CMD="./external/autoresearch/.venv/bin/python main.py --output ./data/research_signal_raw.json"

# Python repo (module)
AUTORESEARCH_CMD="./external/autoresearch/.venv/bin/python -m autoresearch --output ./data/research_signal_raw.json"

# Node repo (npm script)
AUTORESEARCH_CMD="npm run research -- --output ./data/research_signal_raw.json"

# Node repo (direct node entry)
AUTORESEARCH_CMD="node index.js --output ./data/research_signal_raw.json"
```
