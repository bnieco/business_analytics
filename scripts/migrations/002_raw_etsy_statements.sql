-- 002_raw_etsy_statements.sql
-- Creates raw.etsy_statements for Etsy monthly statement CSV exports.
--
-- Statement CSVs (filename pattern: etsy_statement_YYYY_M.csv) contain
-- line-item activity for a single calendar month: sales, fees, marketing
-- (Etsy Ads / Offsite Ads commission), shipping labels, deposits, etc.
-- This is the cost-side data needed for ad ROI and net-revenue analysis.
--
-- Source columns are preserved verbatim (text) for fidelity, plus parsed
-- numeric copies for query convenience, plus provenance fields.
--
-- Idempotency: a UNIQUE index on (source_file, line_number) prevents
-- double-loading the same line from the same file. Re-running the loader
-- against the same CSV is a no-op (ON CONFLICT DO NOTHING).

CREATE TABLE IF NOT EXISTS raw.etsy_statements (
    id                  BIGSERIAL PRIMARY KEY,

    -- Provenance
    source_file         VARCHAR NOT NULL,
    line_number         INT     NOT NULL,
    statement_month     DATE    NOT NULL,  -- first-of-month, derived from filename

    -- Raw source columns (Etsy export)
    date_text           VARCHAR,           -- "March 31, 2026"
    type                VARCHAR,           -- Sale, Fee, Marketing, Shipping, Tax, Deposit, Payment
    title               VARCHAR,           -- "Etsy Ads", "Listing fee", "Transaction fee: ..."
    info                TEXT,              -- free-text description (often references listing/order)
    currency            VARCHAR(8),        -- "USD"
    amount_text         VARCHAR,           -- "$12.34" / "-$2.13" / "--"
    fees_taxes_text     VARCHAR,
    net_text            VARCHAR,
    tax_details         TEXT,

    -- Parsed/normalized fields (NULL if source was "--" or unparseable)
    txn_date            DATE,              -- parsed from date_text
    amount              NUMERIC(12, 2),
    fees_taxes          NUMERIC(12, 2),
    net                 NUMERIC(12, 2),

    loaded_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_etsy_statements_source_line
    ON raw.etsy_statements (source_file, line_number);

CREATE INDEX IF NOT EXISTS idx_etsy_statements_txn_date
    ON raw.etsy_statements (txn_date);

CREATE INDEX IF NOT EXISTS idx_etsy_statements_type
    ON raw.etsy_statements (type);

CREATE INDEX IF NOT EXISTS idx_etsy_statements_statement_month
    ON raw.etsy_statements (statement_month);

CREATE INDEX IF NOT EXISTS idx_etsy_statements_type_title
    ON raw.etsy_statements (type, title);

COMMENT ON TABLE raw.etsy_statements IS
    'Etsy monthly statement line items. One row per CSV line. Preserves source text + parsed numeric copies. Use type/title to filter (e.g., type=Marketing & title=Etsy Ads for ad spend).';
