# Shadow hybrid data path

The relay independently receives Kraken WebSocket ticks and sends rolling
snapshots to the edge over JSON-encoded gRPC. The edge processes new ticks and
forwards anomalies to the strategist over gRPC. For a local shadow run set
`GPT5_ENABLED=true` and `GPT5_SHADOW_MODE=true`: the strategist simulates a
VETO and makes no external LLM request. These services do not place orders.
gRPC is insecure and unauthenticated; use only a trusted isolated network.

## Per-symbol state and concurrency

The worker creates a separate `AnomalyDetector` and window for every pair in
`CRYPTO_PAIRS` (default `BTC/USD,ETH/USD`) while sharing one model instance.
An unconfigured pair is rejected before snapshot processing; configure every
relay pair on the worker. `get_metrics()["detector_buffer_stats"]` is now
keyed by pair, and `detector_alerts` is the sum across detectors. These
process-local windows start empty on worker restart. The legacy preprocessing
retains its previous timestamp behavior; `tick-window-v1` checks chronological
timestamps independently per pair. Equal timestamps remain permitted.

The gRPC snapshot lock serializes snapshot processing and watermark updates.
An additional async tick lock serializes direct WebSocket callbacks and
snapshot tick processing, including shared-model inference and alert delivery;
slow delivery therefore blocks other pairs. Locks are intended for one asyncio
event loop per worker, not concurrent OS threads or multiple event loops;
external callers must marshal work onto the worker's loop. No hardware thread
safety or cross-process synchronization is implied.

## Trading bot connection

**No connection to a running bot has been established or verified.** No
cluster or live process was inspected. The repository's `src/trading_bot.py`
has a separate, optional AI-copilot Hailo gate reading a configured JSONL
file, but it does not consume these gRPC strategist decisions. No running
connection, shared filesystem, or consumer has been verified. Other bot
Hailo/coprocessor features are separate.

## Legacy JSONL fallback

The default `HAILO8_ALERT_FALLBACK_MODE=fail_closed` deliberately differs from
the historical fallback: failed or unavailable strategist delivery rejects the
snapshot and blocks its pair. This avoids implying that a local file write
means the strategist received the alert. For the historical append-on-failure
behavior, explicitly set `HAILO8_ALERT_FALLBACK_MODE=legacy_jsonl` and
`HAILO8_ALERT_FALLBACK_PATH` to an existing writable file path. The historical
location was `/tmp/hailo_alerts.jsonl`; an operator may configure that path
explicitly when appropriate. No parent directories are created. To feed the
optional bot gate, its `AI_COPILOT_HAILO_ALERTS_FILE` must refer to the same
file on a genuinely shared filesystem; enabling the fallback does not establish
or verify that connection.

Legacy mode appends the original alert payload as one JSON line when gRPC is
unavailable or fails. A successful local append allows a snapshot ACK but does
**not** confirm strategist delivery, bot consumption, or durable storage. A
timed-out gRPC call may already have reached the strategist, so fallback may
duplicate delivery across consumers. `alert_forward_failed` or
`alert_forward_unavailable` and `alert_fallback_written` expose this condition.
File-write failure is logged and counted as `alert_fallback_failed` and fails
closed. Configure `fail_closed` explicitly for shadow validation.

## Failure semantics and limits

* A successful snapshot ACK means processing completed, not that hardware
  inference ran. `inference_enabled=false` indicates pass-through; the relay
  records `flush_inference_disabled` and warns.
* Invalid ticks are rejected before processing; failures and rejections are
  logged and exposed in service event metrics. A transport failure may have
  occurred after server-side processing; the relay does not automatically retry.
  The WebSocket callback continues, and rolling windows may lose ticks during
  a prolonged outage. Sequence gaps are logged and counted, not recovered.
* Sequence watermarks are bounded, process-local and non-durable; restarts,
  evictions and new stream IDs can replay ticks. Legacy unsequenced snapshots
  are not deduplicated.
* If tick processing, alert delivery, or cancellation has uncertain side
  effects, the edge blocks that pair until restart and rejects later windows.
  A restart clears the block and watermarks; reconcile first. Alert delivery
  failures are not queued. Only the explicit legacy mode attempts a JSONL
  fallback; a successful append does not block the pair.
* `flush_failed`, `flush_rejected`, `snapshot_invalid_tick`,
  `snapshot_sequence_gap`, `snapshot_tick_failed`, `snapshot_cancelled`,
  `alert_forward_failed` and `alert_forward_unavailable` are event types in
  the respective `trading_hybrid_*_events_total` metrics. Consult logs for
  details. Metrics are process-local and do not provide durable audit history.

The local integration test uses synthetic ticks, stubbed detector output and
loopback gRPC. It does not validate Hailo hardware, a real model, exchange
connectivity, cluster networking, an external LLM, or bot execution.
