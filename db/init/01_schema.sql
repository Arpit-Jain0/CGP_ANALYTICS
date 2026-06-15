-- =============================================================================
-- CPG Analytics Platform — canonical schema
-- Execution order: dimensions → fact → secondary feeds → audit
-- =============================================================================

-- ---------------------------------------------------------------------------
-- AUDIT / BATCH (created first; sales_transactions has a FK to it)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS load_batch (
    load_batch_id   SERIAL PRIMARY KEY,
    load_type       TEXT    NOT NULL,           -- HISTORICAL | INCREMENTAL
    source_file     TEXT,
    source_system   TEXT,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    rows_in         INTEGER DEFAULT 0,
    inserted        INTEGER DEFAULT 0,
    deduped         INTEGER DEFAULT 0,
    rejected        INTEGER DEFAULT 0,
    repaired        INTEGER DEFAULT 0,
    flagged         INTEGER DEFAULT 0,
    late_arriving   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS data_quality_log (
    log_id              SERIAL PRIMARY KEY,
    load_batch_id       INTEGER REFERENCES load_batch(load_batch_id),
    ingested_at         TIMESTAMP NOT NULL DEFAULT now(),
    load_type           TEXT,
    source_system       TEXT,
    source_file         TEXT,
    record_identifier   TEXT,
    issue_type          TEXT,           -- NULL_REQUIRED | TYPE_MISMATCH | DUPLICATE | etc.
    field_name          TEXT,
    raw_value           TEXT,
    action_taken        TEXT            -- REJECTED | REPAIRED | FLAGGED
);

CREATE INDEX IF NOT EXISTS idx_dq_log_batch  ON data_quality_log(load_batch_id);
CREATE INDEX IF NOT EXISTS idx_dq_log_issue  ON data_quality_log(issue_type);

-- ---------------------------------------------------------------------------
-- REFERENCE DIMENSIONS
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_region (
    region              TEXT PRIMARY KEY,
    population          INTEGER,
    median_income_band  TEXT,           -- LOW | MEDIUM | HIGH
    climate_zone        TEXT
);

CREATE TABLE IF NOT EXISTS dim_store (
    store_id    TEXT PRIMARY KEY,
    region      TEXT REFERENCES dim_region(region),
    city        TEXT,
    store_type  TEXT                    -- SUPERMARKET | CONVENIENCE | ONLINE | etc.
);

CREATE INDEX IF NOT EXISTS idx_store_region ON dim_store(region);

-- Type-2 slow-changing product dimension
-- One active row per SKU (is_current=TRUE, valid_to IS NULL).
-- Historical rows retain the version that was current during their validity window.
CREATE TABLE IF NOT EXISTS dim_product (
    product_key     SERIAL PRIMARY KEY,
    sku             TEXT    NOT NULL,
    category        TEXT,
    brand           TEXT,
    package_size    TEXT,
    list_price      NUMERIC(12, 4),
    launch_date     DATE,
    valid_from      DATE    NOT NULL,
    valid_to        DATE,               -- NULL means "current"
    is_current      BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT chk_product_validity CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS idx_product_sku_current ON dim_product(sku, is_current);
CREATE INDEX IF NOT EXISTS idx_product_sku         ON dim_product(sku);

CREATE TABLE IF NOT EXISTS seasonal_calendar (
    calendar_date   DATE PRIMARY KEY,
    season          TEXT,               -- SPRING | SUMMER | FALL | WINTER
    is_holiday      BOOLEAN NOT NULL DEFAULT FALSE,
    holiday_name    TEXT
);

-- ---------------------------------------------------------------------------
-- FACT TABLE
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_transactions (
    transaction_id      TEXT PRIMARY KEY,       -- natural dedup key from source
    transaction_ts      TIMESTAMP NOT NULL,
    store_id            TEXT REFERENCES dim_store(store_id),
    sku                 TEXT,                   -- resolve to dim_product via app logic
    quantity            NUMERIC(12, 4) NOT NULL,
    unit_price          NUMERIC(12, 4),
    revenue             NUMERIC(14, 4) NOT NULL,
    currency            TEXT NOT NULL,          -- normalised ISO-4217 code, e.g. USD
    source_system       TEXT NOT NULL,          -- POS | ONLINE
    load_batch_id       INTEGER REFERENCES load_batch(load_batch_id),
    is_late_arriving    BOOLEAN NOT NULL DEFAULT FALSE,
    ingested_at         TIMESTAMP NOT NULL DEFAULT now()
    -- NOTE: region is intentionally omitted; derive via store_id → dim_store.region
);

-- Typical query patterns: time-range scans, per-store aggregations, SKU lookups
CREATE INDEX IF NOT EXISTS idx_txn_ts        ON sales_transactions(transaction_ts);
CREATE INDEX IF NOT EXISTS idx_txn_store     ON sales_transactions(store_id);
CREATE INDEX IF NOT EXISTS idx_txn_sku       ON sales_transactions(sku);
CREATE INDEX IF NOT EXISTS idx_txn_batch     ON sales_transactions(load_batch_id);
CREATE INDEX IF NOT EXISTS idx_txn_ts_store  ON sales_transactions(transaction_ts, store_id);

-- ---------------------------------------------------------------------------
-- OPTIONAL SECONDARY FEEDS
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS promo_windows (
    promo_id        TEXT PRIMARY KEY,
    category        TEXT,
    region          TEXT,
    start_date      DATE,
    end_date        DATE,
    discount_pct    NUMERIC(5, 2),
    CONSTRAINT chk_promo_dates CHECK (end_date >= start_date)
);

CREATE INDEX IF NOT EXISTS idx_promo_cat_region ON promo_windows(category, region);
CREATE INDEX IF NOT EXISTS idx_promo_dates       ON promo_windows(start_date, end_date);

CREATE TABLE IF NOT EXISTS marketing_campaigns (
    campaign_id     TEXT PRIMARY KEY,
    category        TEXT,
    region          TEXT,
    channel         TEXT,
    start_date      DATE,
    end_date        DATE,
    exposure        NUMERIC(14, 2),     -- e.g. impressions / spend in consistent units
    CONSTRAINT chk_campaign_dates CHECK (end_date >= start_date)
);

CREATE INDEX IF NOT EXISTS idx_campaign_cat_region ON marketing_campaigns(category, region);

CREATE TABLE IF NOT EXISTS competitor_prices (
    obs_date            DATE    NOT NULL,
    category            TEXT    NOT NULL,
    region              TEXT    NOT NULL,
    competitor_price    NUMERIC(12, 4),
    PRIMARY KEY (obs_date, category, region)
);

CREATE INDEX IF NOT EXISTS idx_comp_price_cat ON competitor_prices(category, obs_date);
