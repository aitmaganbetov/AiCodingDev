# syntax=docker/dockerfile:1.7
# Единый backend-образ (ADR-0001): api / workers / alembic / aicd CLI — разные команды.
# Базовые образы зафиксированы по digest (supply chain, NFR-SEC-05).
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build
# Необязательный CA корпоративного/egress-прокси: --secret id=ca_bundle,src=<file>; в образ не попадает.
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir uv==0.11.32
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-dev --no-install-project
COPY packages ./packages
COPY apps/api ./apps/api
COPY apps/cli ./apps/cli
COPY apps/worker ./apps/worker
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
# git нужен для проверки привязываемых репозиториев (P1-12). Источники apt — по HTTPS.
# AICD_INSTALL_GIT=0 — только для проверки конвейера релиза там, где deb.debian.org недоступен: такой образ
# не релизный (без git привязка репозиториев не работает), конвейер помечает его releasable=false в provenance.
ARG AICD_INSTALL_GIT=1
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ "$AICD_INSTALL_GIT" = "1" ]; then \
      sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
      && CA=""; if [ -f /run/secrets/ca_bundle ]; then CA="-o Acquire::https::CAInfo=/run/secrets/ca_bundle"; fi \
      && apt-get $CA update \
      && apt-get $CA install -y --no-install-recommends git \
      && rm -rf /var/lib/apt/lists/*; \
    fi \
 && groupadd --system --gid 10001 aicd \
 && useradd --system --uid 10001 --gid aicd --home-dir /nonexistent --shell /usr/sbin/nologin aicd \
 && mkdir -p /var/lib/aicd/artifacts /var/lib/aicd/index-export \
 && chown aicd:aicd /var/lib/aicd/artifacts /var/lib/aicd/index-export
WORKDIR /app
COPY --from=build /app /app
COPY infrastructure/migrations ./infrastructure/migrations
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER aicd:aicd
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live',timeout=2).status==200 else 1)"
CMD ["uvicorn", "aicd_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
