# syntax=docker/dockerfile:1

FROM python:3.15-rc AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv

RUN python -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/pip" install --upgrade pip setuptools wheel

WORKDIR /app

COPY requirements.txt .
RUN "$VIRTUAL_ENV/bin/pip" install --no-cache-dir -r requirements.txt \
    && find "$VIRTUAL_ENV" -name "*.pyc" -delete \
    && find "$VIRTUAL_ENV" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

FROM python:3.15-rc-slim AS runtime

LABEL org.opencontainers.image.title="Telegram WebSocket Proxy" \
      org.opencontainers.image.description="MTProto proxy with WebSocket transport" \
      org.opencontainers.image.version="1.8.5" \
      org.opencontainers.image.source="https://github.com/LordArrin/tg-ws-proxy-docker"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PROXY_HOST=0.0.0.0 \
    PROXY_PORT=1443 \
    PROXY_DC_IPS="2:149.154.167.220 4:149.154.167.220" \
    PROXY_SECRET= \
    PROXY_BUF=4096 \
    PROXY_POOL_SIZE=2 \
    NO_CFPROXY= \
    CFPROXY_DOMAIN= \
    CFPROXY_WORKER_DOMAIN=

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home --home-dir /home/app app \
    && mkdir -p /home/app/.local /tmp/tg-proxy \
    && chown -R app:app /home/app /tmp/tg-proxy

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app proxy ./proxy
COPY --chown=app:app utils ./utils
COPY --chown=app:app LICENSE ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && find /app -name "*.pyc" -delete \
    && find /app -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

USER app

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]

CMD []
