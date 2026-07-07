# Hailo-8 + K3s Hybrid Installation Guide

## Prerequisites

### Hardware
- ✅ Sipeed Nano Cluster (2x Raspberry Pi CM5)
- ✅ Node 1: 1TB SSD, 4GB RAM, K3s master
- ✅ Node 2: Hailo-8 M.2 AI Accelerator, 4GB RAM, K3s worker
- ✅ Network: Both nodes connected (192.168.x.x on same subnet)

### Software
- ✅ K3s v1.28+ installed on both nodes
- ✅ Python 3.12+ on both nodes
- ✅ `kubectl` access configured on Node 1
- ✅ Git access to repository

## Installation Steps

### Step 1: Clone & Checkout Branch

```bash
# On Node 1 (Master)
git clone https://github.com/sikienzl/TradingBot.git
cd TradingBot
git checkout feature/hailo8-k3s-hybrid
```

### Step 2: Prepare Python Environment

**On Node 1 (Master):**
```bash
# Create virtual environment
python3.12 -m venv venv_trading
source venv_trading/bin/activate

# Install hybrid dependencies
pip install --upgrade pip
pip install -e ".[hybrid,k3s]"
```

**On Node 2 (Hailo Worker):**
```bash
# Create virtual environment with extra packages for Hailo
python3.12 -m venv venv_hailo
source venv_hailo/bin/activate

# Install hailo-specific packages
pip install --upgrade pip
pip install -e ".[hailo8]"

# Verify Hailo-8 hardware access
lspci | grep -i hailo  # Should show: Hailo Devices

# Test Hailo runtime
python3 -c "import hailort; print(hailort.__version__)"
```

### Step 3: Configure Environment

**On Node 1:**
```bash
# Copy template configuration
cp .env.hailo8.example .env.hailo8

# Edit with your credentials
nano .env.hailo8
```

**Critical settings to update:**
```bash
# Kraken API
KRAKEN_API_KEY=your_key_here
KRAKEN_SECRET_KEY=your_secret_here
KRAKEN_WEBSOCKET_V2_ENABLED=true

# GPT-5 Integration
GPT5_API_KEY=your_gpt5_key_here
GPT5_ENABLED=true
GPT5_MAX_CALLS_PER_DAY=100
GPT5_MAX_SPEND_PER_MONTH_USD=500

# Hailo-8 Thresholds
HAILO8_ANOMALY_THRESHOLD=85  # Tune based on backtesting
HAILO8_INFERENCE_INTERVAL_MS=100

# K3s Configuration
K3S_CLUSTER_ENDPOINT=192.168.1.100:6443  # Node 1 IP
```

### Step 4: K3s Node Labeling

```bash
# Label Node 2 for Hailo workload affinity
kubectl label nodes node-2-hailo hailo-node=true

# Verify labeling
kubectl get nodes --show-labels | grep hailo
```

### Step 5: Create K3s Secrets

```bash
# Create namespace
kubectl create namespace trading-bot

# Create secret for API credentials (Git-ignored, not in repo)
kubectl create secret generic trading-bot-credentials \
  --from-literal=KRAKEN_API_KEY=your_key \
  --from-literal=KRAKEN_SECRET_KEY=your_secret \
  --from-literal=GPT5_API_KEY=your_gpt5_key \
  -n trading-bot

# Verify secret created
kubectl get secrets -n trading-bot
```

### Step 6: Build Docker Images

```bash
# Build Hailo worker image
docker build -f docker/hailo-worker.Dockerfile \
  -t trading-hailo:1.0.0 \
  -t trading-hailo:latest .

# Build Cloud strategist image
docker build -f docker/cloud-strategist.Dockerfile \
  -t trading-cloud:1.0.0 \
  -t trading-cloud:latest .

# Tag for K3s local registry (if using local storage)
# Or push to Docker Hub / private registry
docker tag trading-hailo:latest yourregistry/trading-hailo:latest
docker tag trading-cloud:latest yourregistry/trading-cloud:latest
docker push yourregistry/trading-hailo:latest
docker push yourregistry/trading-cloud:latest
```

### Step 7: Deploy to K3s

```bash
# Apply base K3s manifests
kubectl apply -k k8s/base/

# Wait for namespace & RBAC to settle
sleep 5

# Deploy Hailo worker (DaemonSet on Node 2)
kubectl apply -k k8s/overlays/hailo-worker/

# Deploy Cloud strategist (Deployment on Node 1)
kubectl apply -k k8s/overlays/cloud-strategist/

# Verify pods are running
kubectl get pods -n trading-bot
kubectl get daemonsets -n trading-bot
kubectl get deployments -n trading-bot
```

### Step 8: Verify Deployment

```bash
# Check pod status
kubectl get pods -n trading-bot -o wide

# View Hailo worker logs
kubectl logs -f daemonset/trading-hailo-worker -n trading-bot

# View Cloud strategist logs
kubectl logs -f deployment/trading-cloud-strategist -n trading-bot

# Check Hailo hardware access
kubectl exec -it <hailo-pod> -n trading-bot -- lspci | grep -i hailo

# Test WebSocket connection
kubectl exec -it <hailo-pod> -n trading-bot -- python3 -c \
  "import websockets; print('WebSocket OK')"
```

### Step 9: Setup Monitoring (Optional)

```bash
# Deploy Prometheus & Grafana
kubectl apply -k k8s/overlays/monitoring/

# Port-forward Prometheus
kubectl port-forward -n trading-bot svc/prometheus 9090:9090

# Port-forward Grafana
kubectl port-forward -n trading-bot svc/grafana 3000:3000

# Access:
# - Prometheus: http://localhost:9090
# - Grafana: http://localhost:3000 (admin/admin)
```

### Step 10: Verify Hailo-8 Inference Loop

```bash
# Watch metrics in real-time
kubectl logs -f daemonset/trading-hailo-worker -n trading-bot \
  | grep "anomaly_score"

# Expected output (every 100ms):
# [2026-07-07 10:45:23.456] anomaly_score=23.4, confidence=0.82, window_ticks=100
# [2026-07-07 10:45:23.556] anomaly_score=24.1, confidence=0.80, window_ticks=100
# ...
# [2026-07-07 10:45:28.234] anomaly_score=89.3 🚨 ALERT! Triggering GPT-5...
```

## Troubleshooting

### Hailo-8 Module Not Detected

```bash
# On Node 2, verify hardware
lspci -vv | grep Hailo

# If not visible, check device tree & M.2 slot
sudo dmesg | tail -20

# Reinstall Hailo driver
sudo apt update && sudo apt install hailo-hef-examples hailort
```

### WebSocket Connection Failures

```bash
# Test Kraken WebSocket V2 manually
python3 -c """
import asyncio
import websockets

async def test():
    async with websockets.connect('wss://ws.kraken.com/v2') as ws:
        print('Connected to Kraken')
        await ws.send('{\"method\": \"subscribe\", \"params\": {\"channel\": \"ticker\"}}')
        print(await ws.recv())

asyncio.run(test())
"""
```

### K3s Pod Not Starting

```bash
# Describe pod for events
kubectl describe pod <pod-name> -n trading-bot

# Check resource constraints
kubectl top nodes
kubectl top pods -n trading-bot

# Check logs
kubectl logs <pod-name> -n trading-bot --tail=50
```

### GPT-5 API Call Failures

```bash
# Check secret is mounted correctly
kubectl exec -it <cloud-pod> -n trading-bot -- env | grep GPT5

# Test API manually
python3 -c """
import os
from src.cloud.strategist_service import GPT5Client

client = GPT5Client(api_key=os.getenv('GPT5_API_KEY'))
client.test_connection()
"""
```

## Configuration Tuning

### Hailo-8 Anomaly Threshold

Lower = more sensitive (more GPT-5 calls, higher cost):
```bash
HAILO8_ANOMALY_THRESHOLD=70  # Very sensitive, ~10-20 calls/day
HAILO8_ANOMALY_THRESHOLD=85  # Default, ~5-10 calls/day
HAILO8_ANOMALY_THRESHOLD=95  # Conservative, ~1-3 calls/day
```

### Inference Interval Trade-off

Faster = more accurate but higher CPU/latency:
```bash
HAILO8_INFERENCE_INTERVAL_MS=50   # Very fast (20 fps), 2-4% CPU
HAILO8_INFERENCE_INTERVAL_MS=100  # Balanced (10 fps), ~2% CPU
HAILO8_INFERENCE_INTERVAL_MS=200  # Low-power (5 fps), ~1% CPU
```

### K3s Resource Limits

For constrained Nano cluster:
```yaml
# Hailo worker: reserves 2 cores, 1GB RAM (limit: 3 cores, 1.5GB)
# Cloud strategist: reserves 1 core, 512MB RAM (limit: 2 cores, 1GB)
# Total: ~3 cores available for infrastructure
```

## Uninstall / Reset

```bash
# Remove K3s deployments
kubectl delete -k k8s/overlays/cloud-strategist/ -n trading-bot
kubectl delete -k k8s/overlays/hailo-worker/ -n trading-bot
kubectl delete -k k8s/base/ -n trading-bot

# Clean up Python environments
rm -rf venv_trading venv_hailo

# Clean up .env
rm .env.hailo8
```

## Next Steps

1. **Kraken WebSocket V2 Integration** → Implement in `src/kraken_websocket_v2.py`
2. **Time-Series-Transformer Training** → `src/train_timeseries_transformer.py`
3. **Hailo ONNX Wrapper** → `src/hailo/inference.py`
4. **GPT-5 Chief Strategist** → `src/cloud/strategist_service.py`
5. **Integration Testing** → End-to-end scenario testing

---

**Support**: For issues, open GitHub issues or check `k8s/README.md` for K3s-specific documentation.
