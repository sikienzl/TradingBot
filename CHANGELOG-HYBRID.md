# Changelog: Hailo-8 + K3s Hybrid Trading Bot Setup

## Branch: `feature/hailo8-k3s-hybrid`

### Overview
Complete restructuring for Sipeed Nano cluster (2x Raspberry Pi CM5):
- **Node 1 (Master)**: 1TB SSD, K3s master, Cloud Strategist
- **Node 2 (Worker)**: Hailo-8 M.2 Module, K3s worker, Edge Inference

### Phase 1: Branch Setup ✅ COMPLETED

#### Changes Made:

1. **pyproject.toml - Enhanced for Hybrid Architecture**
   - `[hybrid]` extra: websockets, onnx, onnxruntime, hailort
   - `[k3s]` extra: kubernetes, prometheus-client, pyyaml
   - `[hailo8]` extra: Complete stack (torch, transformers, websockets, onnx, hailort)
   - `[all]` extra: Full installation for development
   - Installation: `pip install -e ".[hybrid,hailo8,k3s]"` or `.[all]`

2. **.env.hailo8.example - Configuration Template**
   - Hybrid architecture settings
   - Kraken WebSocket V2 configuration
   - Hailo-8 edge inference parameters (26 TOPS, 100ms loops)
   - GPT-5 Chief Strategist settings (rate limiting, budget)
   - K3s cluster configuration
   - Trading parameters (conservative for HFT regime)

3. **Directory Structure**
   ```
   src/
   ├── hybrid/           (NEW) Orchestration layer
   │   ├── __init__.py
   │   └── decision_gate.py
   ├── hailo/            (NEW) Edge inference
   │   └── __init__.py
   └── cloud/            (NEW) GPT-5 integration
       └── __init__.py
   
   k8s/                  (NEW) Kubernetes manifests
   ├── README.md
   ├── kustomization.yaml
   ├── base/
   │   ├── namespace.yaml
   │   ├── service-account.yaml
   │   └── configmap-hybrid.yaml
   └── overlays/
       ├── hailo-worker/
       │   ├── daemonset-hailo.yaml
       │   ├── pvc-nvme.yaml
       │   └── kustomization.yaml
       └── cloud-strategist/
           ├── deployment-cloud.yaml
           ├── service.yaml
           └── kustomization.yaml
   ```

4. **K3s Manifests Created**
   - DaemonSet for Hailo-8 worker (Node 2 only)
   - Deployment for Cloud Strategist (Node 1 preferred)
   - PersistentVolumeClaim for NVMe tick storage (500GB)
   - ConfigMap for hybrid configuration
   - ServiceAccount with RBAC

5. **Python Modules (Skeleton)**
   - `src/hybrid/decision_gate.py`: Orchestration logic
   - `src/hailo/__init__.py`: Edge inference module
   - `src/cloud/__init__.py`: Cloud integration module

### Next Phases (TODO)

#### Phase 2: Core Implementations
- [ ] `src/kraken_websocket_v2.py`: WebSocket V2 API integration
- [ ] `src/train_timeseries_transformer.py`: Time-Series-Transformer training
- [ ] `src/hailo/inference.py`: ONNX model inference wrapper
- [ ] `src/hailo/edge_filter_service.py`: Main edge service loop
- [ ] `src/cloud/strategist_service.py`: GPT-5 integration service

#### Phase 3: Docker Images
- [ ] `docker/hailo-worker.Dockerfile`
- [ ] `docker/cloud-strategist.Dockerfile`
- [ ] Build & push to registry

#### Phase 4: K3s Deployment
- [ ] Label nodes: `kubectl label nodes node-2 hailo-node=true`
- [ ] Create secrets for API keys
- [ ] Apply K3s manifests
- [ ] Verify pods running & healthy
- [ ] Monitor logs & metrics

#### Phase 5: Integration Testing
- [ ] End-to-end anomaly detection flow
- [ ] GPT-5 API call triggering
- [ ] Trade execution based on hybrid decision
- [ ] Cost tracking & metrics

### Installation Instructions

```bash
# Clone and checkout branch
git clone https://github.com/sikienzl/TradingBot.git
cd TradingBot
git checkout feature/hailo8-k3s-hybrid

# Install Python dependencies
pip install -e ".[hybrid,hailo8,k3s]"

# Or for minimal hybrid setup
pip install -e ".[hybrid]"

# Copy environment template
cp .env.hailo8.example .env.hailo8

# Edit configuration
nano .env.hailo8  # Configure Kraken, GPT-5, Hailo thresholds
```

### Configuration Overview

**Key Environment Variables:**
- `HYBRID_MODE=true`: Enable hybrid filtering
- `HAILO8_ANOMALY_THRESHOLD=85`: Trigger GPT-5 at score > 85 (0-100)
- `HAILO8_INFERENCE_INTERVAL_MS=100`: Check ticks every 100ms
- `GPT5_MAX_CALLS_PER_DAY=100`: Rate limiting to control costs
- `KRAKEN_WEBSOCKET_V2_ENABLED=true`: Use WebSocket instead of polling

### Expected Behavior

1. **Hailo-8 Worker (Node 2)** - Running 24/7:
   - Connect to Kraken WebSocket V2
   - Buffer ticks on NVMe (500GB, 7-day rolling)
   - Run Time-Series-Transformer ONNX model every 100ms
   - Calculate anomaly scores
   - Emit metrics to Prometheus

2. **Cloud Strategist (Node 1)** - On-demand:
   - Listen for anomaly alerts from Hailo-8
   - If score > 85: Call GPT-5 API
   - GPT-5 validates macro scenario
   - Return GO/VETO decision
   - Log all decisions & costs

3. **Trading Bot** - Main loop:
   - Receives hybrid decisions
   - Executes trades (or skips) based on GPT-5 veto
   - Reports PnL & trades to analytics

### Cost Reduction Target

- **Before (Current)**: GPT-5/LLM called for EVERY trade signal (~100-200x/day)
- **After (Hybrid)**: Called ONLY for anomalies (~1-5x/day)
- **Expected Savings**: 95%+ token cost reduction

### Metrics & Monitoring

Prometheus metrics endpoints:
- Hailo-Worker: `http://localhost:9201/metrics`
- Cloud-Strategist: `http://localhost:9202/metrics`
- Main Trading-Bot: `http://localhost:9200/metrics`

Grafana dashboards (planned):
- Anomaly detection score trends
- GPT-5 call frequency & costs
- Edge vs Cloud latency
- Trade execution rate
