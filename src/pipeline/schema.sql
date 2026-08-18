-- High-performance market ticks table for HFT ingestion pipeline
-- Optimized for time-series workloads with composite indexing

CREATE TABLE IF NOT EXISTS market_ticks (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    security_id INT NOT NULL,
    ltp NUMERIC(10, 2) NOT NULL,
    volume BIGINT NOT NULL,
    bid NUMERIC(10, 2),
    ask NUMERIC(10, 2),
    open_interest BIGINT
);

-- Composite index optimized for:
-- 1. Security-specific time-range queries (most common access pattern)
-- 2. DESC order for efficient latest-tick retrieval
CREATE INDEX IF NOT EXISTS idx_market_ticks_security_timestamp
    ON market_ticks (security_id, timestamp DESC);

-- Optional: Partition by time for very high volume (uncomment if needed)
-- CREATE TABLE market_ticks_partitioned (LIKE market_ticks INCLUDING ALL) PARTITION BY RANGE (timestamp);
