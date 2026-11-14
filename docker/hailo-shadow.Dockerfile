FROM python:3.12-slim

RUN groupadd --gid 1000 trading \
    && useradd --uid 1000 --gid trading --create-home trading
WORKDIR /app
COPY requirements-hailo-shadow.txt /app/
RUN pip install --no-cache-dir -r requirements-hailo-shadow.txt
COPY src /app/src
ENV PYTHONPATH=/app
USER trading
WORKDIR /data
CMD ["python3", "-m", "src.trading_bot"]
