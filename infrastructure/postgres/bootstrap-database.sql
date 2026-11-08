-- Bootstrap схемы AiCodingDev в конкретной БД (DATABASE_DESIGN §19.1).
-- Выполняется суперпользователем в целевой БД после bootstrap-roles.sql:
--   psql -f bootstrap-database.sql <database>

\set ON_ERROR_STOP on

REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA aicd AUTHORIZATION aicd_migrator;
REVOKE ALL ON SCHEMA aicd FROM PUBLIC;
GRANT USAGE ON SCHEMA aicd TO aicd_app, aicd_worker, aicd_maintenance, aicd_sysfn;

-- Расширения (§1.7) — в схеме приложения, чтобы search_path = aicd их видел.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA aicd;
CREATE EXTENSION IF NOT EXISTS btree_gin WITH SCHEMA aicd;
