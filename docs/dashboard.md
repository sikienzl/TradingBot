# Dashboard and PostgreSQL Analytics

This setup uses the **existing** PostgreSQL database, does not create a second
server, and does not modify the trading bot. The separate dashboard displays
up to 100 snapshots and 100 events; it has no order endpoints. Basic Auth and a
SELECT-only database role restrict read access. The display is not an exchange
status and does not prove that the bot is running. This local setup deliberately
omits PostgreSQL TLS and is therefore **not protected against eavesdropping or
tampering**, even on the local network.

## Temporary Operation on the CM5 Cluster

In addition to the general setup described below, a temporary dashboard test
was configured on `rasp-clu-01` in the `trading` namespace: deployment
`trading-dashboard-local-52247d45`, database `appdb`, schema
`trading_analytics`, and the existing roles `tradingbot_writer` and
`trade_dashboard_reader`. The writer requires database CREATE; the reader has
SELECT but not INSERT privileges on the analytics tables. This does not
necessarily exclude other public database privileges.

The dashboard pod uses host networking because the existing PostgreSQL
`pg_hba.conf` rejected connections through the cluster service. It binds only
to `127.0.0.1:8080` on Node 1. Local processes on Node 1 can also reach the
port, so protect it with Basic Auth and SSH access. Credentials are stored on
Node 1 only in `/mnt/nvme_data/trading_simulation_59325f0/dashboard-access.txt`
(mode 0600) and in the Kubernetes Secret. To access it from your computer:

```sh
rtk proxy ssh -N -o BatchMode=yes -o StrictHostKeyChecking=yes -L 127.0.0.1:8080:127.0.0.1:8080 raspi-cm5-node-1.kienzle
```

Then open `http://127.0.0.1:8080/` in your local browser. If needed, read the
credentials yourself over SSH from the protected file on Node 1; do not copy
passwords into chat, logs, or the repository. Press Ctrl+C to stop the SSH
process and end access.

The existing dry run was switched to the analytics writer once, with its
portfolio unchanged. The old instance is stopped and must not be started in
parallel. A portfolio and journal snapshot taken immediately before the
restart is on Node 1 at
`/mnt/nvme_data/trading_simulation_59325f0/pre-analytics-52247d45/`.
The dashboard manifest and image build files used are on Node 1 at
`/mnt/nvme_data/trading_simulation_59325f0/deployment-52247d45/`. Analytics
begins only after the switch, is best effort, and is not a recovery mechanism.
The dry run still has no automatic restart; images and runtime manifests cannot
be reproduced from a versioned release. This is not authorization for live
orders.

## Prerequisites and Security Boundaries

* The existing PostgreSQL overlay initializes `appdb`/`trading_analytics` only
  for a **new** data directory. For existing data, first inspect the schema
  and both tables, including their indexes; `scripts/init_postgres_analytics.sql`
  defines the existing schema. Plan schema changes, backups, and rollback
  separately. The SQL below changes ownership and revokes PUBLIC privileges
  on these objects; this may remove access for other users.
* The current StatefulSet does **not** configure TLS; the current ConfigMap
  default is `sslmode=disable`, and `trading_db` is the initial PostgreSQL
  superuser. Use separate roles instead of the superuser. Writer, dashboard,
  and administrative database connections using `sslmode=disable` transmit
  queries and data unencrypted, and the server is not cryptographically
  authenticated. Network-path participants can read traffic, tamper with
  connections, or impersonate the server. Use PostgreSQL SCRAM rather than
  plaintext password authentication; SCRAM does not replace transport
  encryption or server authentication.
* The local network alone is not a security boundary. Do not expose the
  database service or port 5432 externally. Restrict network access to
  required clients using a firewall, PostgreSQL `pg_hba.conf`, and, where
  actually enforced, Kubernetes NetworkPolicies. The operator must verify
  enforcement; this overlay does not create a NetworkPolicy. Do not use this
  plaintext configuration if other systems sharing the network or nodes are
  untrusted.
* Kubernetes Secrets are not encrypted automatically. Encryption at rest,
  restrictive RBAC for Secrets, pods, and `pods/portforward`, trusted nodes,
  separate credentials, rotation, and auditing are the operator's
  responsibility. Do not put Secrets, DSNs, or real passwords in the
  repository, shell history, logs, images, or command-line arguments. Manage
  Secret provisioning through the existing Secret Manager or secured files.
  Restart pods after rotation; environment variables are not updated
  automatically.

## Set Up Roles Once

Run this as an administrative user with role and object management privileges
**against the correct existing database** (for example, the DSN is in
`PGSERVICE`; set `sslmode=disable` there for this setup and store credentials
in a protected libpq password file). The operation intentionally fails if
either role name already exists; do not rerun it blindly. The connection
remains unencrypted and vulnerable to active attacks while setting passwords;
use it only over a suitably controlled network path.

```sh
rtk proxy psql -X -v ON_ERROR_STOP=1 --single-transaction -f k8s/overlays/trading-dashboard/analytics-roles.sql
rtk proxy psql -X
```

Then, in the interactive administrative psql session, run
`\password trading_analytics_writer` and `\password trading_dashboard_reader`.
Until passwords are set, neither login role can be used for password
authentication. Retrieve the passwords separately from the Secret Manager
and do not reuse the same password.

The table-owner role, Writer, intentionally receives more than INSERT
privileges: when connecting, the existing bot runs
`CREATE SCHEMA/TABLE/INDEX IF NOT EXISTS`. It therefore needs database CREATE,
schema CREATE, and ownership of the tables; it can modify or delete tables and
data, and create other schemas. This is **not an append-only security
boundary**. The reader role has only USAGE and SELECT on these two tables, with
`default_transaction_read_only` as an additional precaution, not a substitute
for permissions. The SQL revokes PUBLIC privileges only on the specified
objects; it does not change other roles or databases. Before deployment, also
inspect PUBLIC and explicit grants, role memberships, functions, views, and
TEMP privileges across the entire database, and revoke them as needed. Do not
grant privileged memberships or additional privileges to the reader. Schema
and table owners, as well as superusers, remain powerful. New tables and
subsequent grant changes require another review.

## Writer: Explicitly Configure the Existing Dry Run Safely

The existing `k8s/overlays/analytics-writer` already starts the bot; **do not
start a second instance alongside it**. It is not part of this dashboard
overlay: it uses `hostPath`, potentially broad Secrets, `trading_db`, and the
unencrypted ConfigMap. Before using it, the operator must specifically adjust
its deployment and configuration:

1. Set `DRY_RUN=true`, `ANALYTICS_DB_ENABLED=true`,
   `ANALYTICS_DB_SCHEMA=trading_analytics`, and `ANALYTICS_DB_URL` as a Secret
   containing a libpq DSN for `trading_analytics_writer`, `appdb`, the internal
   database host, and `sslmode=disable`. A configured URL overrides the
   individual database settings; in particular, `ANALYTICS_DB_SSLMODE` does
   **not** enforce TLS when a URL is set.
2. The bot loads `.env` with `override=True`: a file on the existing host mount
   can override `DRY_RUN`, the DSN, and other environment values. Remove it
   before startup or replace it with a controlled empty file. Also audit
   alternative `POSTGRES_*` values, runtime Secrets, deployment rollouts, and
   file permissions. Check the effective configuration without displaying
   Secrets. Do not provide exchange keys with trading permissions to the dry
   run.
3. A replica is not a distributed lock. Do not start parallel writers; also
   prevent rolling updates, manual processes, and old pods from running
   concurrently. Test that snapshots arrive in PostgreSQL and have
   `dry_run=true`. A reported snapshot proves neither correct isolation nor
   complete data.

This repository deliberately does not provide a purportedly safe writer
patch: without knowing the installation, it cannot safely override the host
file, existing Secrets, or network access. Do not deploy until these
prerequisites are met and the plaintext risk is knowingly accepted.

## Deploy the Dashboard

1. Build the image from the repository root and push it to the internal
   registry. Before applying, update the deployment to use the verified,
   immutable image digest. `latest` is only a local placeholder.

   ```sh
   rtk proxy docker build -f docker/trading-dashboard.Dockerfile -t trading-dashboard:latest .
   ```

2. Create a Secret externally in the `trading-bot` namespace named
   `trading-dashboard-credentials` with `DASHBOARD_USER`, `DASHBOARD_PASSWORD`,
   and `DASHBOARD_DATABASE_URL`. The DSN must use only
   `trading_dashboard_reader`, the internal database host, and explicitly
   `sslmode=disable`; percent-encode the URL password correctly if needed.
   Use strong, separate credentials for Basic Auth. The manifest fixes
   `DASHBOARD_DB_SCHEMA=trading_analytics` and does not require a CA volume.
3. After provisioning the Secret, deploy and access it only locally:

   ```sh
   rtk proxy kubectl apply -k k8s/overlays/trading-dashboard
   rtk proxy kubectl -n trading-bot rollout status deployment/trading-dashboard
   rtk proxy kubectl -n trading-bot port-forward --address 127.0.0.1 deployment/trading-dashboard 8080:8080
   ```

   On the same computer, open `http://127.0.0.1:8080/` and enter the
   Basic-Auth credentials. HTTP/Basic Auth is plaintext: never send credentials
   over the local network, do not bind port forwarding to `0.0.0.0`, and do not
   expose a Service, Ingress, or unprotected proxy. The application binds only
   to pod loopback; normal Service forwarding to the pod IP will not work.
   Restrict Kubernetes API access and the `pods/portforward` permission to
   authorized operators through RBAC; `pods/exec`, pod creation, and Secret
   access can also enable access. Secure the computer being used and its local
   user accounts. Port forwarding does not protect the separate plaintext
   connection from the dashboard to PostgreSQL.

4. Verify that missing or incorrect authentication returns 401 and correct
   authentication displays the dashboard; port forwarding is reachable only
   on localhost, and no Service or Ingress exists for the dashboard. Using the
   reader role, test SELECT on both tables, then test INSERT/UPDATE/DELETE/
   TRUNCATE outside an intentionally read-only transaction; all must fail due
   to database permissions. Do not include credentials in diagnostic output.
   A 503 indicates missing authentication configuration or a database error,
   not a healthy dashboard.

## Option: Introduce PostgreSQL TLS Later

The operator must enable PostgreSQL TLS, install a server certificate whose
SAN includes the DNS name in use, and verify the CA independently. Restrict
`pg_hba.conf` to `hostssl` with SCRAM and do not allow a matching plaintext
`host` fallback. For example, the certificate must contain exactly
`postgres-analytics.trading-bot.svc` when that is the host being used. Mount
the CA file as a separate read-only ConfigMap volume in both the dashboard and
writer, and set `PGSSLROOTCERT` to the corresponding file path; use only public
CA certificates, never private keys. Update both Secret DSNs to use
`sslmode=verify-full` and the certified hostname; update the administrative
connection as well. Update the Secrets and deployment, then restart the pods.
A CA ConfigMap is not required for the current plaintext configuration. Then
test a real TLS connection and verify that connections are rejected for a
wrong CA, wrong hostname, or plaintext; `sslmode=require` alone does not
sufficiently verify the server name. This TLS option is not preconfigured here
and does not replace RBAC or localhost-only port forwarding for the dashboard.

## Limitations and Tests

Analytics is best effort, not a durable transaction journal or PostgreSQL
portfolio recovery mechanism: writes, local state, and risk status are not
atomic; gaps or duplicates are possible, and snapshots may be stale. Database
error handling also does not catch all psycopg errors; uninterrupted bot
operation during a database outage is not guaranteed. The display does not
mix currencies or modes in one chart, but it does show historical LIVE and
DRY-RUN data; the last displayed mode is not a runtime-verified setting.
`DRY_RUN_REQUIRE_POSTGRES_RECOVERY=true` currently prevents startup instead of
implementing recovery. Further limitations are described in
`docs/postgres_analytics_schema.md`.

Local tests without a cluster:

```sh
rtk proxy .venv/bin/python -m pytest -q tests/test_trading_dashboard.py tests/test_trading_dashboard_deployment.py
```

Optional integration tests against a real PostgreSQL server require an
**isolated disposable database** and a superuser via
`TRADING_DASHBOARD_SETUP_TEST_DSN`; the setup test creates randomly named
roles and objects, then rolls back the transaction. The existing dashboard
integration tests also require `TRADING_SAFETY_TEST_DSN`. These tests do not
replace TLS or RBAC checks on the target system. They are skipped when these
DSNs are not set.
