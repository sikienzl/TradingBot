# Hailo-8 Edge Worker
# Node 2: High-frequency inference + WebSocket listener

FROM python:3.13-slim

WORKDIR /app

COPY . .

# Keep the edge image focused on the runtime path used by the Hailo worker.
RUN pip install --no-cache-dir \
    aiohttp==3.13.2 \
    grpcio==1.74.0 \
    numpy==2.3.4 \
    onnxruntime==1.23.2 \
    prometheus-client==0.22.1 \
    websockets==15.0.1

VOLUME ["/mnt/nvme", "/var/log/trading-bot"]

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import src.hailo.edge_filter_service; print('OK')" || exit 1

CMD ["python", "-m", "src.hailo.edge_filter_service"]
