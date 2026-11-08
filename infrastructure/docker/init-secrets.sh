#!/bin/sh
# Генерирует случайные секреты для профиля local в infrastructure/docker/secrets (каталог в .gitignore).
# Существующие файлы не перезаписываются.
# Каталог 0700 защищает секреты на хосте; сами файлы 0444, т.к. compose монтирует file-based
# secrets с правами хоста, а сервисы работают под непривилегированными uid (postgres, aicd).
set -eu
dir="$(cd "$(dirname "$0")" && pwd)/secrets"
mkdir -p "$dir"
chmod 700 "$dir"
gen() { [ -f "$dir/$1" ] || { umask 077; head -c 48 /dev/urandom | base64 | tr -d '/+=\n' > "$dir/$1"; }; }
# metrics_token — bearer-токен скрейпа /metrics (P8-06); grafana_admin_password — профиль observability (P8-08).
for name in pg_admin_password pg_migrator_password pg_app_password pg_worker_password pg_maintenance_password app_secret_key redis_password \
            metrics_token grafana_admin_password; do
  gen "$name"
done
[ -f "$dir/migrator_database_url" ] || { umask 077; printf 'postgresql://aicd_migrator:%s@postgres:5432/aicd' "$(cat "$dir/pg_migrator_password")" > "$dir/migrator_database_url"; }
[ -f "$dir/app_database_url" ] || { umask 077; printf 'postgresql://aicd_app:%s@postgres:5432/aicd' "$(cat "$dir/pg_app_password")" > "$dir/app_database_url"; }
[ -f "$dir/app_redis_url" ] || { umask 077; printf 'redis://:%s@redis:6379/0' "$(cat "$dir/redis_password")" > "$dir/app_redis_url"; }
chmod 0444 "$dir"/*
echo "Секреты созданы в $dir"
