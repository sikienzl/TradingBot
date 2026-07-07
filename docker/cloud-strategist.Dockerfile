# GPT-5 Cloud Strategist
# Node 1: GPT-5 orchestration + macro validation

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project
COPY . .

# Install Python dependencies directly (avoid hatch-vcs git metadata requirement in image builds)
RUN pip install --no-cache-dir -r requirements.txt \
    aiohttp \
    websockets \
    kubernetes \
    prometheus-client \
    pyyaml

# Non-root user
RUN useradd -m -u 1000 trading && chown -R trading:trading /app
USER trading

# Create volumes for logs
VOLUME ["/var/log/trading-bot"]

# Health check
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import src.cloud.strategist_service; print('OK')" || exit 1

# Run cloud strategist service
CMD ["python", "-m", "src.cloud.strategist_service"]
