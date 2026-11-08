# K3s Deployment for Hailo-8 + Hybrid Trading Bot

## Architecture Overview

```text
┌─────────────────────────────────────────────────────┐
│         K3s Cluster (Nano: 2x Raspberry Pi CM5)    │
├──────────────────────────┬──────────────────────────┤
│                          │                          │
│  NODE 1 - Master         │  NODE 2 - Hailo-Worker   │
│  ├─ 1TB SSD              │  ├─ Hailo-8 M.2 Module   │
│  ├─ Trading-Bot Pod      │  ├─ WebSocket Listener   │
│  ├─ Cloud-Strategist     │  ├─ High-Freq Inference  │
│  │  (GPT-5 calls)        │  │  (Time-Series-TFM)   │
│  └─ Redis/RabbitMQ       │  └─ NVMe tick buffer    │
│                          │                          │
└──────────────────────────┴──────────────────────────┘
         ↓
    Kraken WebSocket V2
    (24/7 live ticks)
```

## Deployment-Strategie

### Phase 1: Vorbereitung (lokal)

```bash
# Branch already created: feature/hailo8-k3s-hybrid

# 1. Install Python dependencies
pip install -e ".[hybrid,hailo8,k3s]"

# 2. Build Docker images
docker build -f docker/hailo-worker.Dockerfile -t trading-hailo:latest .
docker build -f docker/cloud-strategist.Dockerfile -t trading-cloud:latest .

# 3. Push to K3s node registry (if not using Docker Hub)
# Or configure image pulls in manifests
```

### Phase 2: Build & Deploy

```bash
# Deploy the complete staging or production profile (choose one).
# These overlays reference shared resources outside their directories.
kustomize build --load-restrictor LoadRestrictionsNone k8s/overlays/staging/ | kubectl apply -f -
# Or the production profile, which also stays in dry-run/shadow mode by default:
kustomize build --load-restrictor LoadRestrictionsNone k8s/overlays/production/ | kubectl apply -f -

# Deploy Postgres analytics on the SSD1-backed master node
kustomize build --load-restrictor LoadRestrictionsNone k8s/overlays/postgres-analytics/ | kubectl apply -f -

# Verify pods are running
kubectl get pods -n trading-bot
kubectl logs -f <pod-name> -n trading-bot

# Full re-setup from this repo (K3s repair/join, .env migration, secret apply, deploy)
DB_PASSWORD='<set-postgres-password>' bash scripts/setup_rpi_k3s_cluster.sh
```

### Phase 3: Monitoring

The hybrid services expose Prometheus metrics; they do not deploy a Prometheus
server. Use a separately managed Prometheus server or query the metrics
endpoints directly. Grafana is not required by the hybrid rollout.

The optional dashboard has its own image (`docker/trading-dashboard.Dockerfile`)
and overlay (`k8s/overlays/trading-dashboard`). It is not deployed by the
hybrid rollout. Create the `trading-dashboard-credentials` Secret separately
with `DASHBOARD_USER`, `DASHBOARD_PASSWORD`, and `DASHBOARD_DATABASE_URL`;
use a dedicated PostgreSQL role with SELECT privileges only on the analytics
tables. Set `DASHBOARD_DB_SCHEMA` in that Secret if it is not `public`.
The image listens only on pod loopback, with no Kubernetes Service or ingress.
After deploying and importing the image on the master, access it locally with
`kubectl -n trading-bot port-forward deployment/trading-dashboard 8080:8080`.
Basic authentication requires TLS for remote access; do not expose it directly.
Dashboard snapshots are best-effort analytics, not a Kraken reconciliation.

**Live-order safety:** The trading bot currently blocks all live orders,
including stop-loss and other protective sells, until durable recovery and
Kraken reconciliation are implemented and verified. Manage existing positions
directly on Kraken. This block does not affect dry-run orders.

### Host Monitoring on the Master

If you want to install Prometheus and Node Exporter on the cluster master:

```bash
sudo bash /opt/trading_2/scripts/install_monitoring_host.sh /opt/trading_2
```

This installs and enables:

- `prometheus.service`
- `prometheus-node-exporter.service`
- `pnl-exporter.service`
- `scorecard-status.timer`
- `node-exporter-textfile.service`

The host installer does not install or remove Grafana. The hybrid rollout's
monitoring step requires Prometheus to be installed; use `--skip-monitoring`
when it is managed separately or not installed.

## Node Labels

The overlays are scheduled by stable node labels instead of hostnames:

- `trading-role=master-ssd` for the control-plane node with the SSD-backed Postgres volume
- `trading-role=hailo-worker` for the worker node with the Hailo module

The setup script applies these labels automatically.

### Persistent relay ticks

The market-data-relay overlay requests a dedicated 20 GiB PVC with a
`Retain` reclaim policy and schedules its single writer on `master-ssd`.
Before deployment, ensure the SSD is mounted at `/mnt/nvme_data` and that the
installed K3s local-path provisioner supports the `nodePath` parameter and
allows `/mnt/nvme_data/trading_db` for that node. Configure this separately
in Ansible; the StorageClass alone does not configure the provisioner or mount
the SSD. Verify the resulting PV's node affinity and actual host path, and
ensure its directory is writable by container UID 1000. Local-path PVC sizes
are requests, not necessarily enforced quotas; monitor free space and back up
the data separately. The manifests cannot detect a missing SSD mount: without
an Ansible/systemd mount dependency and verification, provisioning can write to
the OS disk instead. Do not deploy until that protection is in place. The
existing PostgreSQL StorageClass has the same provisioner prerequisite.

The relay writes SQLite under the PVC rather than `/tmp`, but persistence is
best effort: failed writes are logged and streaming continues, disconnects can
lose events, and the in-memory gRPC window is not restored after restart.
The retention setting is not automatically enforced by the relay. This is not
a complete or lossless historical dataset.

## Dateien in diesem Verzeichnis

```text
k8s/
├── README.md                          # This file
├── kustomization.yaml                 # Kustomize base config
├── configmap-hybrid.yaml              # ConfigMaps for .env.hailo8
├── secret-credentials.yaml            # Secrets (Git-ignored)
├── secret-credentials.example.yaml    # Placeholder secret manifest
│
├── base/                              # Kustomize base
│   ├── kustomization.yaml
│   ├── namespace.yaml
│   └── service-account.yaml
│
├── overlays/
│   ├── hailo-worker/
│   │   ├── kustomization.yaml
│   │   ├── daemonset-hailo.yaml      # Runs on Node 2 (Hailo-8)
│   │   └── pvc-nvme.yaml             # NVMe storage claim
│   │
│   ├── cloud-strategist/
│   │   ├── kustomization.yaml
│   │   ├── deployment-cloud.yaml     # GPT-5 calls on Node 1
│   │   └── service.yaml
│   │
│   ├── market-data-relay/
│   │   ├── kustomization.yaml
│   │   └── deployment-relay.yaml     # Streams Kraken windows from Pi 1 to Pi 2 over gRPC
│   │
│   ├── analytics-writer/
│   │   ├── kustomization.yaml
│   │   └── deployment-analytics-writer.yaml # Dry-run bot writing trades/snapshots into Postgres
│   │
│   ├── postgres-analytics/
│   │   ├── kustomization.yaml
│   │   ├── configmap-initdb.yaml     # Initializes analytics schema/tables
│   │   ├── service-postgres.yaml
│   │   └── statefulset-postgres.yaml # Master-only DB on SSD
│   │
│   └── monitoring/                   # Planned / optional cluster monitoring
│
└── development/                       # Local dev configs
    └── docker-compose.yaml           # For testing locally
```

## Important Environment Variables

See `.env.hailo8.example` for the full list:

- `HAILO8_ANOMALY_THRESHOLD`: Trigger point for GPT-5 calls (default: 85/100)
- `HAILO8_INFERENCE_INTERVAL_MS`: How often Hailo-8 updates (default: 100ms)
- `HAILO8_EDGE_GRPC_TARGET`: Cluster endpoint used by the master-node relay
- `CLOUD_STRATEGIST_GRPC_TARGET`: Cluster endpoint used by the Hailo worker for strategist decisions
- `GPT5_MAX_CALLS_PER_DAY`: Rate limit for cloud calls (default: 100)
- `KRAKEN_WEBSOCKET_V2_ENABLED`: Enable WebSocket instead of CCXT polling
- `ANALYTICS_DB_HOST`: K3s service name for the master-node Postgres sink

The shared base and production configurations default to `DRY_RUN=true` and
`GPT5_SHADOW_MODE=true`. To opt into live trading, deliberately set both values
to `false` in the production patch, inspect the rendered manifests, and replace
all placeholder credentials before applying. The runtime does not use the
Kubernetes API, so the shared service account has no Kubernetes permissions
and does not mount an API token.

## Analytics Storage Layout

- The master node NVMe path hosts the canonical Postgres analytics database via the `local-path-master-ssd1` StorageClass.
- The expected host path is `/mnt/nvme_data/trading_db` on the node labeled `trading-role=master-ssd`.
- The Hailo worker keeps `/mnt/nvme` as a local rolling tick buffer close to inference.
- The optional analytics writer appends trade and portfolio events into Postgres and does not sit on the runtime read path.

Before applying the Postgres overlay, ensure the target directory exists on the master node:

```bash
sudo mkdir -p /mnt/nvme_data/trading_db
sudo chown root:root /mnt/nvme_data/trading_db
```

## Analytics Verification

Step 1: Verify that the bot is configured to write analytics snapshots and trade events.

- The write path is enabled in [src/trading_bot.py](src/trading_bot.py) through `PostgresAnalyticsWriter`.
- Trade events are appended via `self.analytics_writer.write_trade(...)`.
- Portfolio snapshots are appended via `self.analytics_writer.write_snapshot(...)` every `ANALYTICS_DB_SNAPSHOT_EVERY` iterations.

Step 2: Verify that rows are actually landing in Postgres and that the DB is using the mounted NVMe data directory.

```bash
bash scripts/check_postgres_analytics.sh
```

This prints the active Postgres `data_directory` and the current row counts / latest timestamps for:

- `trading_analytics.trade_events`
- `trading_analytics.portfolio_snapshots`

To generate analytics rows inside the cluster, deploy the dedicated dry-run writer workload:

```bash
kustomize build --load-restrictor LoadRestrictionsNone k8s/overlays/analytics-writer/ | kubectl apply -f -
kubectl -n trading-bot rollout status deploy/trading-analytics-writer
```

## Existing Cluster Migration to NVMe Path

If the cluster already has a `postgres-analytics` PVC, changing the `StorageClass` alone will not move the existing data. Use the migration helper after preparing the NVMe data path on the node labeled `trading-role=master-ssd`:

```bash
sudo mkdir -p /mnt/nvme_data/trading_db
sudo chown root:root /mnt/nvme_data/trading_db

bash scripts/migrate_postgres_analytics_to_ssd1.sh
```

The script will:

- create a logical `pg_dump` backup
- scale down and recreate the StatefulSet/PVC on the NVMe-backed StorageClass
- restore the dump into the recreated database
- run `scripts/check_postgres_analytics.sh` at the end

## Secrets

Create the runtime secret from the example manifest and fill in real values before applying overlays that need credentials:

```bash
cp k8s/secret-credentials.example.yaml k8s/secret-credentials.yaml
kubectl apply -f k8s/secret-credentials.yaml
```

## Next Steps

1. **Kraken WebSocket V2 Integration** (`src/kraken_websocket_v2.py`)
2. **Time-Series Transformer Training** (`src/train_timeseries_transformer.py`)
3. **Hailo-8 ONNX Wrapper** (`src/hailo/inference.py`)
4. **Build Docker images**
5. **Extend K3s manifests**

## Debugging

```bash
# SSH in Hailo-Worker Pod
kubectl exec -it <hailo-pod> -n trading-bot -- bash

# Check Hailo-8 hardware
lspci | grep -i hailo

# Monitor real-time Hailo metrics
kubectl logs -f <hailo-pod> -n trading-bot | grep "anomaly_score"

# Check GPT-5 API calls
kubectl logs -f <cloud-pod> -n trading-bot | grep "gpt5_api"
```

## Resources for K3s Nano Cluster

**Node 1 (Master, 1TB SSD):**

- CPU: 4 cores (reserved: 1 core for K3s)
- RAM: ~2GB (reserved: 512MB for K3s)
- Available for pods: 3 cores, ~1.5GB RAM

**Node 2 (Worker, Hailo-8):**

- CPU: 4 cores (reserved: 1 core)
- RAM: ~2GB
- Hailo-8: 26 TOPS
- Available for pods: 3 cores, ~1.5GB RAM

Deployment should stay within this budget!
