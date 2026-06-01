-- DDL for reviews tables
-- raw.etsy_reviews: landing table for reviews.json
-- core.reviews:     canonical table linked to core.orders

CREATE TABLE IF NOT EXISTS raw.etsy_reviews (
    review_id   SERIAL PRIMARY KEY,
    order_id    BIGINT      NOT NULL UNIQUE,   -- Etsy order ID; one review per order
    reviewer    TEXT,
    reviewed_at DATE,
    star_rating SMALLINT,
    message     TEXT,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.reviews (
    review_id       SERIAL PRIMARY KEY,
    order_id        INTEGER     REFERENCES core.orders(order_id),
    source_order_id BIGINT,                    -- Etsy order ID, for reference
    reviewer        TEXT,
    reviewed_at     DATE,
    star_rating     SMALLINT    CHECK (star_rating BETWEEN 1 AND 5),
    message         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS core_reviews_source_order_id_idx
    ON core.reviews (source_order_id);
