# K3s Deployment für Hailo-8 + Hybrid Trading Bot

## Architektur-Übersicht

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
# Apply base configuration
kubectl apply -k k8s/base/

# Deploy Hailo-Worker Daemonset (Node 2 only)
kubectl apply -f k8s/overlays/hailo-worker/

# Deploy Cloud-Strategist Deployment (Node 1+)
kubectl apply -f k8s/overlays/cloud-strategist/

# Deploy Postgres analytics on the SSD-backed master node
kubectl apply -k k8s/overlays/postgres-analytics/

# Verify pods are running
kubectl get pods -n trading-bot
kubectl logs -f <pod-name> -n trading-bot
```

### Phase 3: Monitoring

```bash
# Port-forward Prometheus
kubectl port-forward -n trading-bot svc/prometheus 9090:9090

# View Grafana dashboards
kubectl port-forward -n trading-bot svc/grafana 3000:3000
```

### Host Monitoring on the Master

If you want the cluster master to look like the single Raspberry Pi setup, install monitoring on the host instead of running Grafana inside K3s:

```bash
sudo bash /opt/trading_2/scripts/install_monitoring_host.sh /opt/trading_2
```

This installs and enables:

- `prometheus.service`
- `prometheus-node-exporter.service`
- `grafana-server.service`
- `pnl-exporter.service`
- `scorecard-status.timer`
- `node-exporter-textfile.service`

Grafana will be available on port 3000 and use the same provisioning files as the single-Pi host setup.

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

## Wichtige Umgebungsvariablen

Siehe `.env.hailo8.example` für vollständige Liste:

- `HAILO8_ANOMALY_THRESHOLD`: Trigger-Punkt für GPT-5 Calls (default: 85/100)
- `HAILO8_INFERENCE_INTERVAL_MS`: Wie oft Hailo-8 updated (default: 100ms)
- `GPT5_MAX_CALLS_PER_DAY`: Rate-Limit für Cloud-Calls (default: 100)
- `KRAKEN_WEBSOCKET_V2_ENABLED`: Aktiviere WebSocket statt CCXT polling
- `ANALYTICS_DB_HOST`: K3s service name for the master-node Postgres sink

## Analytics Storage Layout

- The master node SSD hosts the canonical Postgres analytics database.
- The Hailo worker keeps `/mnt/nvme` as a local rolling tick buffer close to inference.
- The optional analytics writer appends trade and portfolio events into Postgres and does not sit on the runtime read path.

## Secrets

Create the runtime secret from the example manifest and fill in real values before applying overlays that need credentials:

```bash
cp k8s/secret-credentials.example.yaml k8s/secret-credentials.yaml
kubectl apply -f k8s/secret-credentials.yaml
```

## Nächste Schritte

1. **Kraken WebSocket V2 Integration** (`src/kraken_websocket_v2.py`)
2. **Time-Series-Transformer Training** (`src/train_timeseries_transformer.py`)
3. **Hailo-8 ONNX Wrapper** (`src/hailo/inference.py`)
4. **Docker-Images bauen**
5. **K3s Manifests erweitern**

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

## Ressourcen für K3s Nano Cluster

**Node 1 (Master, 1TB SSD):**

- CPU: 4 cores (reserviert: 1 core für K3s)
- RAM: ~2GB (reserviert: 512MB für K3s)
- Available für Pods: 3 cores, ~1.5GB RAM

**Node 2 (Worker, Hailo-8):**

- CPU: 4 cores (reserviert: 1 core)
- RAM: ~2GB
- Hailo-8: 26 TOPS
- Available für Pods: 3 cores, ~1.5GB RAM

Deployment sollte in diesem Budget bleiben!
