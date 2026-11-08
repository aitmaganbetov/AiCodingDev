-- Bootstrap ролей AiCodingDev (DATABASE_DESIGN §19.1) — уровень кластера, ОДИН раз.
-- Выполняется суперпользователем PostgreSQL до первой миграции:
--   psql -v migrator_password=... -v app_password=... -v worker_password=... \
--        -v maintenance_password=... -f bootstrap-roles.sql postgres
-- Пароли передаются переменными psql (из файлов секретов) и в файл не записываются.
-- Затем для каждой БД — bootstrap-database.sql.

\set ON_ERROR_STOP on

CREATE ROLE aicd_migrator    LOGIN PASSWORD :'migrator_password'    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE aicd_app         LOGIN PASSWORD :'app_password'         NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE aicd_worker      LOGIN PASSWORD :'worker_password'      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE aicd_maintenance LOGIN PASSWORD :'maintenance_password' NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
-- Владелец SECURITY DEFINER функций sys_* (§19.5). NOLOGIN: войти под ней нельзя.
CREATE ROLE aicd_sysfn NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
-- Миграции передают владение функциями sys_* роли aicd_sysfn (ALTER FUNCTION ... OWNER TO
-- требует членства). Мигратор используется только Alembic.
GRANT aicd_sysfn TO aicd_migrator;

ALTER ROLE aicd_migrator    SET search_path = aicd;
ALTER ROLE aicd_app         SET search_path = aicd;
ALTER ROLE aicd_worker      SET search_path = aicd;
ALTER ROLE aicd_maintenance SET search_path = aicd;
ALTER ROLE aicd_sysfn       SET search_path = aicd;
ALTER ROLE aicd_app         SET statement_timeout = '15s';
ALTER ROLE aicd_maintenance SET log_statement = 'all';
