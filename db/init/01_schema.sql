-- =============================================================================
-- CPG Analytics Platform — canonical schema
-- Layers:
--   raw.*      — data landed as-is from source Excel (all TEXT, no type coercion)
--   curated.*  — DQ-passed, cleaned, fully typed data ready for analytics
--   error.*    — rows rejected by the DQ gate with full audit metadata
--   public.*   — audit tables (load_batch, data_quality_log) + forecast results
-- =============================================================================

-- ---------------------------------------------------------------------------
-- SCHEMAS
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS curated;
CREATE SCHEMA IF NOT EXISTS error;

-- ---------------------------------------------------------------------------
-- PUBLIC — AUDIT / BATCH (created first; other tables FK to it)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS load_batch (
    load_batch_id   SERIAL PRIMARY KEY,
    load_type       TEXT    NOT NULL,           -- HISTORICAL | INCREMENTAL
    source_file     TEXT,
    source_system   TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
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
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    load_type           TEXT,
    source_system       TEXT,
    source_file         TEXT,
    record_identifier   TEXT,
    issue_type          TEXT,
    field_name          TEXT,
    raw_value           TEXT,
    action_taken        TEXT
);

CREATE INDEX IF NOT EXISTS idx_dq_log_batch ON data_quality_log(load_batch_id);
CREATE INDEX IF NOT EXISTS idx_dq_log_issue ON data_quality_log(issue_type);

-- ── Forecast results ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS forecast_results (
    forecast_id       SERIAL          PRIMARY KEY,
    run_date          DATE            NOT NULL,
    category          TEXT            NOT NULL,
    region            TEXT            NOT NULL,
    target_date       DATE            NOT NULL,
    predicted_revenue NUMERIC(14, 4)  NOT NULL,
    yhat_lower        NUMERIC(14, 4),
    yhat_upper        NUMERIC(14, 4),
    model_version     TEXT            NOT NULL DEFAULT '1.0',
    created_at        TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE (run_date, category, region, target_date)
);

CREATE INDEX IF NOT EXISTS idx_fc_run    ON forecast_results (run_date, category, region);
CREATE INDEX IF NOT EXISTS idx_fc_target ON forecast_results (category, region, target_date);


-- =============================================================================
-- RAW LAYER  (raw.*)
-- All business columns stored as TEXT — exactly as read from the source Excel
-- after basic header normalisation (lowercase + strip).  No type coercion,
-- no null substitution, no row removal.
-- Metadata columns are prefixed with _raw_.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- raw.pos_transactions
-- Source: pos_sales_history.xlsx (any sheet), incremental/*_pos.xlsx (Sales)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.pos_transactions (
    _raw_id         BIGSERIAL   PRIMARY KEY,
    _source_file    TEXT        NOT NULL,
    _sheet_name     TEXT        NOT NULL,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- source columns as TEXT
    transaction_id  TEXT,
    ts              TEXT,
    store_id        TEXT,
    sku             TEXT,
    qty             TEXT,
    unit_price      TEXT,
    amount          TEXT,
    currency        TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_pos_batch ON raw.pos_transactions(_load_batch_id);
CREATE INDEX IF NOT EXISTS idx_raw_pos_txnid ON raw.pos_transactions(transaction_id);

-- ---------------------------------------------------------------------------
-- raw.online_orders
-- Source: online_sales_history.xlsx, incremental/*_online.xlsx (Orders)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.online_orders (
    _raw_id         BIGSERIAL   PRIMARY KEY,
    _source_file    TEXT        NOT NULL,
    _sheet_name     TEXT        NOT NULL,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    order_id        TEXT,
    order_datetime  TEXT,
    location_id     TEXT,
    product_sku     TEXT,
    units           TEXT,
    price_per_unit  TEXT,
    currency        TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_online_batch   ON raw.online_orders(_load_batch_id);
CREATE INDEX IF NOT EXISTS idx_raw_online_orderid ON raw.online_orders(order_id);

-- ---------------------------------------------------------------------------
-- raw.dim_region
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.dim_region (
    _raw_id             BIGSERIAL   PRIMARY KEY,
    _source_file        TEXT        NOT NULL,
    _sheet_name         TEXT        NOT NULL,
    _load_batch_id      INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    region              TEXT,
    population          TEXT,
    median_income_band  TEXT,
    climate_zone        TEXT
);

-- ---------------------------------------------------------------------------
-- raw.dim_store
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.dim_store (
    _raw_id         BIGSERIAL   PRIMARY KEY,
    _source_file    TEXT        NOT NULL,
    _sheet_name     TEXT        NOT NULL,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    store_id        TEXT,
    region          TEXT,
    city            TEXT,
    store_type      TEXT
);

-- ---------------------------------------------------------------------------
-- raw.dim_product
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.dim_product (
    _raw_id         BIGSERIAL   PRIMARY KEY,
    _source_file    TEXT        NOT NULL,
    _sheet_name     TEXT        NOT NULL,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sku             TEXT,
    category        TEXT,
    brand           TEXT,
    package_size    TEXT,
    list_price      TEXT,
    launch_date     TEXT
);

-- ---------------------------------------------------------------------------
-- raw.seasonal_calendar
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.seasonal_calendar (
    _raw_id         BIGSERIAL   PRIMARY KEY,
    _source_file    TEXT        NOT NULL,
    _sheet_name     TEXT        NOT NULL,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    calendar_date   TEXT,
    season          TEXT,
    is_holiday      TEXT,
    holiday_name    TEXT
);

-- ---------------------------------------------------------------------------
-- raw.promo_windows
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.promo_windows (
    _raw_id         BIGSERIAL   PRIMARY KEY,
    _source_file    TEXT        NOT NULL,
    _sheet_name     TEXT        NOT NULL,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    promo_id        TEXT,
    category        TEXT,
    region          TEXT,
    start_date      TEXT,
    end_date        TEXT,
    discount_pct    TEXT
);

-- ---------------------------------------------------------------------------
-- raw.marketing_campaigns
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.marketing_campaigns (
    _raw_id         BIGSERIAL   PRIMARY KEY,
    _source_file    TEXT        NOT NULL,
    _sheet_name     TEXT        NOT NULL,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    campaign_id     TEXT,
    category        TEXT,
    region          TEXT,
    channel         TEXT,
    start_date      TEXT,
    end_date        TEXT,
    exposure        TEXT
);

-- ---------------------------------------------------------------------------
-- raw.competitor_prices
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.competitor_prices (
    _raw_id         BIGSERIAL   PRIMARY KEY,
    _source_file    TEXT        NOT NULL,
    _sheet_name     TEXT        NOT NULL,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    obs_date        TEXT,
    category        TEXT,
    region          TEXT,
    competitor_price TEXT
);

-- ---------------------------------------------------------------------------
-- raw.product_updates
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.product_updates (
    _raw_id         BIGSERIAL   PRIMARY KEY,
    _source_file    TEXT        NOT NULL,
    _sheet_name     TEXT        NOT NULL,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sku             TEXT,
    attribute       TEXT,
    old_value       TEXT,
    new_value       TEXT,
    effective_date  TEXT,
    change_reason   TEXT
);


-- =============================================================================
-- ERROR LAYER  (error.*)
-- Rows rejected by the pre-ingestion DQ gate.  Row content is stored as JSONB
-- so any sheet schema can be captured in a single generic table.
-- =============================================================================

CREATE TABLE IF NOT EXISTS error.dq_rejected_rows (
    _error_id       BIGSERIAL   PRIMARY KEY,
    _source_file    TEXT        NOT NULL,
    _sheet_name     TEXT        NOT NULL,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    dq_issue        TEXT        NOT NULL,   -- DUPLICATE_ROW | PK_DUPLICATE | DATATYPE_VIOLATION
    dq_detail       TEXT,
    dq_action       TEXT,
    row_data        JSONB       NOT NULL    -- full row content as key-value JSON
);

CREATE INDEX IF NOT EXISTS idx_err_batch  ON error.dq_rejected_rows(_load_batch_id);
CREATE INDEX IF NOT EXISTS idx_err_issue  ON error.dq_rejected_rows(dq_issue);
CREATE INDEX IF NOT EXISTS idx_err_file   ON error.dq_rejected_rows(_source_file);
CREATE INDEX IF NOT EXISTS idx_err_data   ON error.dq_rejected_rows USING gin(row_data);


-- =============================================================================
-- CURATED LAYER  (curated.*)
-- DQ-passed, cleaned, fully typed data.  Mirrors the downstream CSV schema.
-- Dimension tables are TRUNCATED + re-inserted on each historical load.
-- Fact tables use ON CONFLICT DO NOTHING for idempotent incremental appends.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- curated.sales_transactions
-- Combined POS + Online fact table (same schema as sales_transactions.csv)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS curated.sales_transactions (
    transaction_id      TEXT        PRIMARY KEY,
    transaction_ts      TIMESTAMP,
    store_id            TEXT,
    sku                 TEXT,
    quantity            NUMERIC(12, 4),
    unit_price          NUMERIC(12, 4),
    revenue             NUMERIC(14, 4),
    currency            TEXT,
    source_system       TEXT,
    source_file         TEXT,
    _load_batch_id      INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cur_txn_ts    ON curated.sales_transactions(transaction_ts);
CREATE INDEX IF NOT EXISTS idx_cur_txn_store ON curated.sales_transactions(store_id);
CREATE INDEX IF NOT EXISTS idx_cur_txn_sku   ON curated.sales_transactions(sku);
CREATE INDEX IF NOT EXISTS idx_cur_txn_batch ON curated.sales_transactions(_load_batch_id);

-- ---------------------------------------------------------------------------
-- curated.product_updates
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS curated.product_updates (
    _update_id      BIGSERIAL   PRIMARY KEY,
    sku             TEXT        NOT NULL,
    attribute       TEXT,
    old_value       TEXT,
    new_value       TEXT,
    effective_date  DATE,
    change_reason   TEXT,
    _load_batch_id  INTEGER     REFERENCES public.load_batch(load_batch_id),
    _ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cur_prod_upd_sku ON curated.product_updates(sku);


-- =============================================================================
-- PUBLIC ANALYTICS LAYER  (public.*)
-- Denormalised, analytics-ready tables written by the ingestion pipeline and
-- queried directly by the API + integration tests.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- dim_region
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_region (
    region  TEXT PRIMARY KEY
);

-- ---------------------------------------------------------------------------
-- dim_store
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_store (
    store_id    TEXT PRIMARY KEY,
    region      TEXT,
    city        TEXT,
    store_type  TEXT
);

-- ---------------------------------------------------------------------------
-- dim_product  (SCD Type-2 — full history kept, is_current marks active row)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_product (
    product_id  BIGSERIAL   PRIMARY KEY,
    sku         TEXT        NOT NULL,
    category    TEXT,
    brand       TEXT,
    valid_from  DATE,
    valid_to    DATE,
    is_current  BOOLEAN     NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_dim_product_sku ON dim_product(sku);

-- ---------------------------------------------------------------------------
-- sales_transactions  (analytics fact table — typed, deduplicated)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_transactions (
    transaction_id   TEXT        PRIMARY KEY,
    transaction_ts   TIMESTAMPTZ,
    store_id         TEXT,
    sku              TEXT,
    quantity         NUMERIC(12, 4),
    unit_price       NUMERIC(12, 4),
    revenue          NUMERIC(14, 4),
    currency         TEXT,
    source_system    TEXT,
    source_file      TEXT,
    is_late_arriving BOOLEAN     NOT NULL DEFAULT FALSE,
    _load_batch_id   INTEGER     REFERENCES load_batch(load_batch_id),
    _ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_txn_ts    ON sales_transactions(transaction_ts);
CREATE INDEX IF NOT EXISTS idx_txn_store ON sales_transactions(store_id);
CREATE INDEX IF NOT EXISTS idx_txn_sku   ON sales_transactions(sku);
