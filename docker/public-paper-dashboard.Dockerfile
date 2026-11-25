FROM python:3.12-slim

ARG TRADING_UID=1000
ARG TRADING_GID=1000

RUN groupadd --gid "${TRADING_GID}" trading \
    && useradd --uid "${TRADING_UID}" --gid trading --create-home trading
WORKDIR /app
COPY requirements-dashboard.txt /app/
RUN pip install --no-cache-dir -r requirements-dashboard.txt
COPY src/public_paper_dashboard.py /app/src/public_paper_dashboard.py
COPY src/market_observations.py /app/src/market_observations.py
RUN touch /app/src/__init__.py
ENV PYTHONPATH=/app
USER trading
EXPOSE 8443
CMD ["python3", "-m", "src.public_paper_dashboard"]
