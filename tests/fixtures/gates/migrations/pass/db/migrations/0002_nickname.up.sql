-- добавление колонки (не деструктивно)
ALTER TABLE accounts ADD COLUMN nickname text;
CREATE INDEX ix_accounts_email ON accounts (email);
