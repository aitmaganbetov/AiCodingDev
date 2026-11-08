-- Схема (фикстура). CREATE TABLE in_comment (x int);
/* CREATE TABLE in_block_comment (x int); */
CREATE TABLE users (
  id bigint PRIMARY KEY,
  note text DEFAULT 'CREATE TABLE in_string (x int);'
);

create table if not exists app.audit_log (id bigint);

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_users_note ON users (note);

CREATE OR REPLACE FUNCTION app.touch() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  -- CREATE TABLE in_body (x int);
  NEW.updated_at := now();
  RETURN NEW;
END $fn$;

CREATE TYPE mood AS ENUM ('sad', 'ok');
CREATE MATERIALIZED VIEW "Report View" AS SELECT 1;
CREATE TRIGGER trg_touch BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION app.touch();
INSERT INTO users VALUES (1, 'x');
