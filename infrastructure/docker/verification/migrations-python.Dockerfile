# syntax=docker/dockerfile:1.7
# Образ MIGRATION gate для Alembic-проектов (OQ-43 (a), ключ образа `migrations-python`, aicd_verification.images):
# PostgreSQL 17 (initdb/pg_ctl/psql/pg_dump — драйвер MIGRATION_DRIVER) + Python 3.12 + alembic/SQLAlchemy/psycopg.
#
# apk/apt-зеркала недоступны в окружении сборки, поэтому PostgreSQL берётся из того же закреплённого образа, что и
# сервис postgres (alpine 3.24, musl), а Python — из официального python:3.12-alpine3.24 (та же версия alpine и musl):
# бинарники PostgreSQL и их разделяемые библиотеки (по ldd) копируются во второй образ. Python-зависимости —
# только с проверкой sha256 (`--require-hashes`, файл сгенерирован `uv pip compile --generate-hashes` из
# migrations-python.requirements.in, версии = uv.lock платформы). Зависимости самого проекта ставит шаг prepare
# (OQ-43 (b), QUALITY_GATES §4.3), не этот образ.
#
# Сборка:
#   docker build -f infrastructure/docker/verification/migrations-python.Dockerfile \
#     -t aicd/migrations-python:17-3.12 infrastructure/docker/verification
# Проверка: `uv run python -m scripts.release.check_migrations_image aicd/migrations-python:17-3.12`.
FROM postgres:17-alpine@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac AS pg
# Разделяемые библиотеки, нужные серверу и клиентским утилитам (JIT не копируется: без llvmjit.so PostgreSQL штатно работает без JIT).
RUN set -eu; mkdir -p /deps; \
    for f in /usr/local/bin/postgres /usr/local/bin/initdb /usr/local/bin/pg_ctl /usr/local/bin/psql \
             /usr/local/bin/pg_dump /usr/local/bin/createdb /usr/local/lib/libpq.so.5 \
             $(ls /usr/local/lib/postgresql/*.so | grep -v llvmjit); do \
      ldd "$f" | awk '/=>/ {print $3}' ; \
    done | sort -u | grep -v '^/usr/local/' | while read -r lib; do \
      mkdir -p "/deps$(dirname "$lib")"; cp -L "$lib" "/deps$lib"; \
    done

FROM python:3.12-alpine3.24@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7
COPY --from=pg /deps/ /
COPY --from=pg /usr/local/bin/postgres /usr/local/bin/initdb /usr/local/bin/pg_ctl /usr/local/bin/psql \
     /usr/local/bin/pg_dump /usr/local/bin/createdb /usr/local/bin/
COPY --from=pg /usr/local/lib/libpq.so.5 /usr/local/lib/
COPY --from=pg /usr/local/lib/postgresql /usr/local/lib/postgresql
COPY --from=pg /usr/local/share/postgresql /usr/local/share/postgresql
# Данные ICU (icu-data-full): initdb создаёт ICU-коллации ("unicode") — без .dat initdb падает.
COPY --from=pg /usr/share/icu /usr/share/icu
COPY migrations-python.requirements.txt /tmp/requirements.txt
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
 && rm -f /tmp/requirements.txt /usr/local/lib/postgresql/llvmjit*.so \
 && addgroup -g 70 -S postgres && adduser -u 70 -S -D -G postgres -H -h /var/lib/postgresql -s /bin/sh postgres
ENV LANG=en_US.utf8
USER 70:70
