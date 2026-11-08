# Kraken tick recording

`KrakenWebSocketV2` stores each complete tick in `kraken_ticks.db` before
calling its optional tick callback. If SQLite cannot commit a tick (including
because the filesystem or configured database limit is full), the client logs
the database path and error and raises `TickStorageError`. The listener stops
on that error rather than proceeding as if the tick had been recorded. The
callback is not invoked for that tick. Callers should supervise/restart the
service and inspect the storage path; an automatic retry or a durable
write-ahead queue is not provided.

SQLite connections used for initialization, writes, reads, and cleanup are
closed even when an operation fails. Read and cleanup errors are also surfaced
as `TickStorageError`; a failed read is not represented as an empty result.

## Disk capacity and warnings

The database has no hard size limit by default. Configure
`HAILO8_LOCAL_TICK_MAX_DB_BYTES` to a positive integer to cap SQLite's maximum
page count. SQLite then rejects writes that would exceed the page limit; the
client reports this as a storage error and does not delete older records to
make room. The limit covers the SQLite database pages, not other files in the
directory, filesystem metadata, or unrelated consumers of the volume. A
configured limit smaller than the existing database prevents further database
operations; it does not truncate or remove the database. `0` means unlimited.
The cap is applied to connections opened by this recorder, not to independent
processes that write directly to the same SQLite file.

`HAILO8_LOCAL_TICK_WARN_FREE_BYTES` sets a low-free-space warning threshold
(default: 1 GiB; set to `0` to disable). Free space and database/retention
health are checked at startup and at most once per minute while ticks are
recorded. These are warnings, not reservations: another process can consume
the remaining space after a check. Filesystem quotas, PVC capacity, and
operational alerts should also be configured outside this process. Actual
SQLite write failures remain fatal to the listener regardless of warnings.

## Retention and data deletion

`HAILO8_LOCAL_TICK_RETENTION_DAYS` defaults to 7 and must be positive. It is a
warning threshold only: expired rows are counted and warned about, never
deleted automatically. When the database is near its configured limit, the
client warns and continues until SQLite refuses a write; it does not perform
retention cleanup as a side effect. `cleanup_old_ticks()` is an explicit,
destructive operation and removes rows older than the configured period only
when a caller invokes it. Back up or export data before invoking it if those
ticks must be retained.

## Storage location and guarantees

`HAILO8_LOCAL_TICK_PATH` selects the directory (default: `/tmp/trading_ticks`,
which is usually ephemeral). A persistent mount is required if the database
must survive host/container restarts. The recorder commits one SQLite
transaction per tick and only reports a tick as accepted to its callback after
that commit succeeds.

This is not an end-to-end lossless feed guarantee. Network disconnections,
exchange-side omissions, process/host failure before a commit, and inability
to persist will leave gaps; there is no replay/reconnect cursor, in-memory
backlog, automatic retry, cross-process coordination, or monitoring export in
this recorder. Do not treat the SQLite file as an exchange-authoritative
record.
