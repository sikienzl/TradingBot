# Trading safety store (PostgreSQL)

This is a persistence building block, **not an order execution system**. It is
not connected to the bot or an exchange. It uses the existing psycopg 3
dependency. Provide a factory that creates a **new, idle, non-autocommit**
connection to a dedicated trusted schema, and call `initialize()` explicitly
before use. The factory must not set up a transaction; configure the schema in
connection options, for example `options="-c search_path=trading_safety"`.
Create that schema and grant permissions separately. Restrict its `search_path`
and write privileges to trusted principals. Initialization creates three v1
tables transactionally; it is not a migration or schema compatibility check.

## Public API

Import from `src.trading_safety`:

* `Scope(account: str, mode: Mode, venue: str)`; `Mode` is `LIVE`, `PAPER`
  or `SIMULATION`. Every key is scoped to all three fields.
* `OrderIntent(client_id, symbol, side: Side, quantity: Decimal,
  limit_price: Decimal | None = None)`; `Side` is `BUY` or `SELL`. A missing
  limit price means market. Quantities/prices are strictly positive finite
  Decimals. Persist a unique client ID before any network call.
* `Fill(execution_id, quantity: Decimal, price: Decimal)` identifies an
  execution uniquely within the scope; duplicate identical data is idempotent.
* `TradingSafetyStore(connect)` exposes:
  * `initialize() -> None`
  * `create_intent(scope, intent) -> Order`
  * `get_order(scope, client_id) -> Order`
  * `transition(scope, client_id, expected_version, status,
    exchange_id=None) -> Order`
  * `record_fill(scope, client_id, fill) -> Order`
  * `recovery_orders(scope) -> list[Order]`
  * `save_risk(scope, state: dict, expected_version: int) -> RiskSnapshot`
  * `load_risk(scope) -> RiskSnapshot`
* `Order` contains scope, intent, status, exchange ID, cumulative filled
  quantity and version. `RiskSnapshot` has version and JSON-object state.
  Risk version 0 creates; later updates require the current version. This is
  optimistic concurrency control, not a historical snapshot log.

Each method owns one transaction and closes its connection. Errors are explicit:
`DatabaseError` wraps psycopg errors, `Conflict` signals duplicate identifiers,
conflicting fill replay or stale versions, `NotFound` signals missing data, and
`InvalidTransition` signals a forbidden order state. Validation also raises
`ValueError`/`TypeError`. Failed commits or connection closes may have an
**ambiguous commit outcome**: read back by stable ID and reconcile; never
assume rollback or generate a new ID. Do not retry sends blindly.

## State and recovery contract

States are `INTENT -> SUBMITTING -> OPEN`, with `UNKNOWN` for uncertain
submission/order status, plus `PARTIALLY_FILLED`, `FILLED`, `CANCELED`,
`REJECTED`. Record `SUBMITTING` and wait for its commit **before sending**;
after any crash, a `SUBMITTING` order may already have reached the exchange.
The store does not submit anything or prevent a caller from bypassing this
protocol. `OPEN` requires an immutable exchange ID. Explicit transitions use
row locks and version checks. `FILLED` and `PARTIALLY_FILLED` result from
recorded executions, never a status assertion; an unknown partially filled
order reopens as partially filled. A late fill on a canceled order remains
canceled unless its quantity completes the order. Rejected orders with fills,
overfills, ID changes and invalid transitions fail.

`recovery_orders` returns all nonterminal intents, submissions, unknown and
open/partially filled orders. This is a work list, **not** proof that terminal
orders have no late fills. The application must also reconcile terminal orders
and exchange history, handle missing/truncated exchange history and halt when
it cannot establish a consistent state. An exchange may not guarantee that its
client IDs or execution IDs have the scope or lifetime assumed by this model;
verify those semantics and adapt externally before use. Unique constraints
apply per account/mode/venue, not across scopes.

## Remaining production safety work

There is no exchange reconciliation, send coordination/lease, trading halt,
risk enforcement, risk/order atomic transaction, encryption, audit log,
schema migration, retention policy or automated backup. The model omits fees,
time-in-force and exchange-specific order semantics; it is not sufficient to
reconstruct arbitrary orders or positions. Durable commits depend on PostgreSQL
configuration, storage and failover guarantees. Risk JSON has no domain schema.
Restrict direct table writes, since state-machine checks are application-level;
the database does not defend against an untrusted writer. Treat every failed
or ambiguous persistence operation as a reason to stop trading until reconciled.

## Tests

Run `.venv/bin/python -m pytest -q -p no:cacheprovider
tests/test_trading_safety_store.py`. Unit tests use isolated mocks and never
connect. Optional integration tests run only when `TRADING_SAFETY_TEST_DSN`
explicitly points to a dedicated disposable test database: they create a
uniquely named schema and drop only that schema afterwards. Never point it at
a production database. No exchange access is performed.
