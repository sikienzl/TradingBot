FROM python:3.12-slim

ARG TRADING_UID=1000
ARG TRADING_GID=1000

RUN groupadd --gid "${TRADING_GID}" trading \
    && useradd --uid "${TRADING_UID}" --gid trading --create-home trading
WORKDIR /app
COPY requirements-hailo-shadow.txt /app/
RUN pip install --no-cache-dir -r requirements-hailo-shadow.txt
COPY src /app/src
ENV PYTHONPATH=/app
USER trading
WORKDIR /data
CMD ["python3", "-m", "src.trading_bot"]
