-- Create a read-only role for local dev-read-only mode.
-- Run once against the prod database as neondb_owner:
--
--   psql "$DATABASE_URL_PROD" -f scripts/create_readonly_role.sql
--
-- After running, set DATABASE_URL_READONLY in .env.local to the connection
-- string for claven_readonly (see .env.local.example).

CREATE ROLE claven_readonly WITH LOGIN PASSWORD '<generate with: python3 -c "import secrets; print(secrets.token_hex(16))">';
GRANT USAGE ON SCHEMA public TO claven_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO claven_readonly;
-- Ensures future tables are also covered
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO claven_readonly;
-- Except Gmail credentials and users' correspondents: debugging never needs them.
REVOKE SELECT ON gmail_tokens, sent_recipients FROM claven_readonly;
