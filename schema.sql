-- ============================================================================
-- PrixTN - schema de base de donnees (PostgreSQL / Supabase)
-- ============================================================================
-- Contrat de la base, versionne dans le depot (audit F-07). A executer dans
-- l'editeur SQL Supabase ou via psql sur une base vierge. Toutes les
-- instructions sont idempotentes (IF NOT EXISTS) : le script peut etre
-- rejoue sans danger.
--
-- NOTE pg_trgm : sur Supabase, `CREATE EXTENSION pg_trgm` s'installe dans le
-- schema "extensions" et les fonctions resolvent via le search_path par
-- defaut. Si l'API loggue "pg_trgm: False", executez cette seule ligne dans
-- l'editeur SQL Supabase puis redemarrez l'API :
--     CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------- produits
CREATE TABLE IF NOT EXISTS products (
    id         BIGSERIAL PRIMARY KEY,
    source     TEXT          NOT NULL,
    category   TEXT,
    title      TEXT          NOT NULL,
    sku        TEXT,
    price      NUMERIC(12,3) NOT NULL CHECK (price > 0),
    price_raw  TEXT,
    url        TEXT          NOT NULL,
    image      TEXT,
    in_stock   BOOLEAN       NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT products_source_url_key UNIQUE (source, url)
);

-- Index trigram : transforme les recherches "title ILIKE '%tok%'" et la
-- tolerance aux fautes ($1 <% title) en parcours d'index (audit F-15).
CREATE INDEX IF NOT EXISTS idx_products_title_trgm
    ON products USING gin (title gin_trgm_ops);

-- Index de tri par prix.
CREATE INDEX IF NOT EXISTS idx_products_price ON products (price);

-- ---------------------------------------------------------- historique prix
-- Alimente automatiquement par le declencheur ci-dessous a chaque changement
-- de prix (crawler nocturne). Base des futures courbes de prix et alertes.
CREATE TABLE IF NOT EXISTS price_history (
    id          BIGSERIAL PRIMARY KEY,
    product_id  BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price       NUMERIC(12,3) NOT NULL,
    captured_at TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_history_product
    ON price_history (product_id, captured_at DESC);

CREATE OR REPLACE FUNCTION log_price_change() RETURNS trigger AS $$
BEGIN
    IF NEW.price IS DISTINCT FROM OLD.price THEN
        INSERT INTO price_history (product_id, price)
        VALUES (NEW.id, NEW.price);
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_price_change ON products;
CREATE TRIGGER trg_price_change AFTER UPDATE ON products
FOR EACH ROW WHEN (OLD.price IS DISTINCT FROM NEW.price)
EXECUTE FUNCTION log_price_change();

-- --------------------------------------------------------- migration F-22
-- Aligne l'orthographe de la boutique Mytek (le crawler ecrivait "MyTek",
-- scrapers.py ecrit "Mytek" : deux groupes distincts pour une seule boutique).
UPDATE products SET source = 'Mytek' WHERE source = 'MyTek';
