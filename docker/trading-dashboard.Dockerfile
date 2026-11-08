FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir \
    fastapi==0.121.1 \
    uvicorn==0.38.0 \
    "psycopg[binary]==3.2.9"

COPY src/__init__.py src/__init__.py
COPY src/dashboard/ src/dashboard/

RUN useradd -m -u 1000 trading
USER trading

CMD ["python", "-m", "uvicorn", "src.dashboard.app:app", "--host", "127.0.0.1", "--port", "8080"]
