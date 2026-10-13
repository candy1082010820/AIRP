FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIRP_DB_PATH=/app/data/airp.sqlite3

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir uv==0.12.23 \
    && uv sync --frozen --no-dev --no-editable \
    && chmod -R a+rX /app/app \
    && groupadd --gid 10001 airp \
    && useradd --uid 10001 --gid airp --no-create-home airp \
    && mkdir -p /app/data \
    && chown airp:airp /app/data

USER airp
EXPOSE 8000

CMD ["/bin/sh", "-c", "exec /app/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
