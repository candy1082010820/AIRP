FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIRP_DB_PATH=/app/data/airp.sqlite3

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
# Optional build secrets support managed networks without persisting proxy settings.
RUN --mount=type=secret,id=https_proxy,env=HTTPS_PROXY \
    --mount=type=secret,id=http_proxy,env=HTTP_PROXY \
    --mount=type=secret,id=no_proxy,env=NO_PROXY \
    --mount=type=secret,id=pip_ca,target=/run/secrets/pip-ca.pem \
    if [ -f /run/secrets/pip-ca.pem ]; then export PIP_CERT=/run/secrets/pip-ca.pem SSL_CERT_FILE=/run/secrets/pip-ca.pem; fi \
    && python -m pip install --no-cache-dir uv==0.12.23 \
    && uv sync --frozen --no-dev --no-editable \
    && chmod -R a+rX /app/app \
    && groupadd --gid 10001 airp \
    && useradd --uid 10001 --gid airp --no-create-home airp \
    && mkdir -p /app/data \
    && chown airp:airp /app/data

USER airp
VOLUME ["/app/data"]
EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
