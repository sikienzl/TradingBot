# GPT-5 Cloud Strategist
# Node 1: GPT-5 orchestration + macro validation

FROM python:3.12-slim

WORKDIR /app

COPY . .

# Keep the cloud image light enough for Raspberry Pi builds.
RUN pip install --no-cache-dir \
    aiohttp==3.13.2

RUN useradd -m -u 1000 trading && chown -R trading:trading /app
USER trading

VOLUME ["/var/log/trading-bot"]

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import src.cloud.strategist_service; print('OK')" || exit 1

CMD ["python", "-m", "src.cloud.strategist_service"]
