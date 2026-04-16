# syntax=docker/dockerfile:1

FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv

RUN python -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/pip" install --upgrade pip setuptools wheel

WORKDIR /app
RUN "$VIRTUAL_ENV/bin/pip" install cryptography==46.0.7

FROM python:3.14-slim AS runtime

LABEL org.opencontainers.image.title="Telegram WebSocket Proxy" \
      org.opencontainers.image.description="MTProto proxy with WebSocket transport" \
      org.opencontainers.image.version="1.2" \
      org.opencontainers.image.source="https://github.com/LordArrin/tg-ws-proxy"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PROXY_HOST=0.0.0.0 \
    PROXY_PORT=1443 \
    PROXY_DC_IPS="2:149.154.167.220 4:149.154.167.220" \
    PROXY_SECRET= \
    PROXY_BUF=512 \
    PROXY_POOL_SIZE=8

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /home/app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app proxy ./proxy
COPY --chown=app:app LICENSE ./

USER app

EXPOSE 1443/tcp

ENTRYPOINT ["/usr/bin/tini", "--", "/bin/sh", "-c", "\
    set -eu; \
    set -- --host \"${PROXY_HOST}\" --port \"${PROXY_PORT}\" --buf-kb \"${PROXY_BUF}\" --pool-size \"${PROXY_POOL_SIZE}\"; \
    if [ -n \"${PROXY_SECRET:-}\" ]; then \
        set -- \"$@\" --secret \"${PROXY_SECRET}\"; \
    fi; \
    for dc in ${PROXY_DC_IPS}; do \
        set -- \"$@\" --dc-ip \"$dc\"; \
    done; \
    exec /opt/venv/bin/python -u proxy/tg_ws_proxy.py \"$@\" \
", "--"]

CMD []
