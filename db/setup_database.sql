-- ---------------------------------------------------------------------------
-- One-shot setup for a fresh PostgreSQL server (psql only).
--
--   psql -U postgres -f db/setup_database.sql
--
-- It creates the database if needed, connects to it and applies schema.sql.
-- Prefer `python db/init_db.py` if you are not sure how to run psql.
-- ---------------------------------------------------------------------------

SELECT 'CREATE DATABASE business_extractor'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'business_extractor')\gexec

\connect business_extractor

\ir schema.sql

\echo ''
\echo 'Database business_extractor is ready.'
