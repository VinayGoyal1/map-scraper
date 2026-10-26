-- ---------------------------------------------------------------------------
-- Business extractor schema.
--
-- Apply this file either with:
--     python db/init_db.py            (recommended, also creates the database)
-- or inside an existing database with:
--     psql -U postgres -d business_extractor -f db/schema.sql
--
-- The file is idempotent: running it again is safe.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS businesses (
    id              BIGSERIAL PRIMARY KEY,

    -- core fields requested by the tool
    name            TEXT        NOT NULL,
    phone           TEXT,
    website         TEXT,
    has_website     BOOLEAN     NOT NULL DEFAULT FALSE,
    email           TEXT,
    emails_found    TEXT[]      NOT NULL DEFAULT '{}',

    -- extra context (useful, free to collect)
    address         TEXT,
    category        TEXT,
    rating          DOUBLE PRECISION,
    reviews_count   INTEGER,

    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    place_id        TEXT,

    -- links
    map_url         TEXT        NOT NULL,
    source_url      TEXT,

    -- full raw scrape (debugging / future fields)
    raw_data        JSONB       NOT NULL DEFAULT '{}'::jsonb,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Upgrade an older/partial `businesses` table in place. On a table created by
-- the statement above every clause below is a no-op, so this stays idempotent.
-- `id`, `name` and `map_url` are deliberately left out: they are the identity
-- of the row and cannot be added with a safe default to a table that has data.
ALTER TABLE businesses
    ADD COLUMN IF NOT EXISTS phone         TEXT,
    ADD COLUMN IF NOT EXISTS website       TEXT,
    ADD COLUMN IF NOT EXISTS has_website   BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS email         TEXT,
    ADD COLUMN IF NOT EXISTS emails_found  TEXT[]      NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS address       TEXT,
    ADD COLUMN IF NOT EXISTS category      TEXT,
    ADD COLUMN IF NOT EXISTS rating        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS reviews_count INTEGER,
    ADD COLUMN IF NOT EXISTS latitude      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS longitude     DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS place_id      TEXT,
    ADD COLUMN IF NOT EXISTS source_url    TEXT,
    ADD COLUMN IF NOT EXISTS raw_data      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- A place is unique by Google place id when we managed to read it,
-- and by its cleaned map link otherwise.
CREATE UNIQUE INDEX IF NOT EXISTS ux_businesses_place_id
    ON businesses (place_id)
    WHERE place_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_businesses_map_url
    ON businesses (map_url);

CREATE INDEX IF NOT EXISTS ix_businesses_name_lower
    ON businesses (LOWER(name));

CREATE INDEX IF NOT EXISTS ix_businesses_created_at
    ON businesses (created_at DESC);

CREATE INDEX IF NOT EXISTS ix_businesses_email
    ON businesses (LOWER(email));

-- Keep `updated_at` correct even when a row is changed outside the ORM
-- (psql, pgAdmin, another service). Without this the column would only ever
-- change when SQLAlchemy's `onupdate=func.now()` happened to be involved.
CREATE OR REPLACE FUNCTION businesses_set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_businesses_updated_at ON businesses;

CREATE TRIGGER trg_businesses_updated_at
    BEFORE UPDATE ON businesses
    FOR EACH ROW
    EXECUTE FUNCTION businesses_set_updated_at();
