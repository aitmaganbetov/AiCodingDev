#!/bin/sh
# Выполняется образом postgres один раз при инициализации тома (docker-entrypoint-initdb.d).
# Пароли ролей читаются из Docker secrets и передаются psql переменными.
# Пустой или нечитаемый секрет — фатальная ошибка (никаких ролей без пароля).
set -eu

read_secret() {
  f="/run/secrets/$1"
  if [ ! -r "$f" ]; then echo "secret $1 is not readable" >&2; exit 1; fi
  v="$(cat "$f")"
  if [ "${#v}" -lt 16 ]; then echo "secret $1 is empty or too short" >&2; exit 1; fi
  printf '%s' "$v"
}

MIGRATOR="$(read_secret pg_migrator_password)"
APP="$(read_secret pg_app_password)"
WORKER="$(read_secret pg_worker_password)"
MAINT="$(read_secret pg_maintenance_password)"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v migrator_password="$MIGRATOR" -v app_password="$APP" \
  -v worker_password="$WORKER" -v maintenance_password="$MAINT" \
  -f /aicd-bootstrap/bootstrap-roles.sql
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -f /aicd-bootstrap/bootstrap-database.sql
