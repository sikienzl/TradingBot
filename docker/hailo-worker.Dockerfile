# Hailo-8 Edge Worker
# Node 2: High-frequency inference + WebSocket listener

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (minimal for edge)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -e ".[hailo8]"

# Create volumes for NVMe & logs
VOLUME ["/mnt/nvme", "/var/log/trading-bot"]

# Health check
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import src.hailo.edge_filter_service; print('OK')" || exit 1

# Run edge filter service
CMD ["python", "-m", "src.hailo.edge_filter_service"]
