CREATE TABLE accounts (
  id bigint PRIMARY KEY,
  email text NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (id bigint);
