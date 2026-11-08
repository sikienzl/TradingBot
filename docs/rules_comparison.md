# Isolated Rule-Based Dry Run

The comparison run uses public Kraken market data, no API keys, and a separate
virtual portfolio. The original dry run continues unchanged. The comparison
strategy only replaces the missing ML probabilities in the uptrend fallback
with an explicit exception for rule-based signals without a tabular model. The
RSI filter, defensive market mode, additional risk rules, and live-order block
remain active. In defensive market mode, the fallback remains blocked, so a
buy is not guaranteed. This exception is not a production-validated strategy
and must not be considered a trading recommendation.

The comparison starts with 19.52 EUR in virtual capital and limits each
simulated purchase to a maximum of 5 EUR. Fees, slippage, and execution do not
reliably reflect exchange conditions. State is stored in a separate JSON file;
risk and process state are not fully restored. The comparison deliberately
does not write to the shared dashboard schema, which has no account or strategy
ID.

On Node 1, the portfolio, journal, and log are stored separately under
`/mnt/nvme_data/trading_rules_comparison_52247d45/`; the container is named
`trading-rules-comparison-52247d45`. To view logs:

```sh
rtk proxy ssh -o BatchMode=yes -o StrictHostKeyChecking=yes raspi-cm5-node-1.kienzle 'docker logs --tail 100 trading-rules-comparison-52247d45'
```

The container has no automatic restart policy. After a failure, only one
instance may run with the same data directory. A file lock protects against
cooperating instances running simultaneously on the same host. The script
refuses to start without an existing portfolio and creates one only with
`--initialize --initial-cash`; existing state is never overwritten. Before
restarting, manually inspect the risk state, JSON file, and journal: the file
lock is not distributed fencing or a recovery mechanism. Do not provide live
orders or private exchange keys.
