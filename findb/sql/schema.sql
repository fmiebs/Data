-- =============================================================================
-- Financial Data Lake – PostgreSQL Schema (Phase 1)
-- Idempotent: alle Objekte mit IF NOT EXISTS.
-- index_overview und index_constituents existieren bereits – werden nicht angefasst.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS master;
CREATE SCHEMA IF NOT EXISTS pub_equity;
CREATE SCHEMA IF NOT EXISTS priv_equity;
CREATE SCHEMA IF NOT EXISTS sec;

-- ----------------------------------------------------------------------------
-- master.sectors  –  GICS-Klassifizierung
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS master.sectors (
    sector_code   VARCHAR(20)  PRIMARY KEY,
    sector_name   VARCHAR(100) NOT NULL,
    gics_level    VARCHAR(30)
);

-- ----------------------------------------------------------------------------
-- master.securities  –  Zentrale Stammdatentabelle
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS master.securities (
    ric           VARCHAR(50)  PRIMARY KEY,
    isin          VARCHAR(20),
    cusip         VARCHAR(12),
    company_name  VARCHAR(200),
    sector_code   VARCHAR(20)  REFERENCES master.sectors(sector_code),
    currency      VARCHAR(5),
    exchange      VARCHAR(20),
    created_at    TIMESTAMPTZ  DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  DEFAULT NOW()
);

-- ----------------------------------------------------------------------------
-- pub_equity.index_overview       ← existiert bereits, wird nicht angefasst
-- pub_equity.index_constituents   ← existiert bereits, wird nicht angefasst
-- ----------------------------------------------------------------------------

-- ----------------------------------------------------------------------------
-- pub_equity.prices_daily
-- Tägliche OHLCV-Preise + Total Return Index
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pub_equity.prices_daily (
    ric               VARCHAR(50)  NOT NULL,
    trade_date        DATE         NOT NULL,
    open_price        NUMERIC(20,6),
    high_price        NUMERIC(20,6),
    low_price         NUMERIC(20,6),
    close_price       NUMERIC(20,6),
    total_return_idx  NUMERIC(20,6),
    volume            BIGINT,
    updated_at        TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (ric, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_prices_ric  ON pub_equity.prices_daily(ric);
CREATE INDEX IF NOT EXISTS idx_prices_date ON pub_equity.prices_daily(trade_date);

-- ----------------------------------------------------------------------------
-- pub_equity.fundamentals_annual
-- Jahresabschlussdaten (FY) – Beträge in Mio. Reporting Currency
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pub_equity.fundamentals_annual (
    ric              VARCHAR(50)  NOT NULL,
    fiscal_year      SMALLINT     NOT NULL,
    period_end_date  DATE,
    pe_ratio         NUMERIC(12,4),
    pb_ratio         NUMERIC(12,4),
    ev_ebitda        NUMERIC(12,4),
    revenue          NUMERIC(20,3),
    ebitda           NUMERIC(20,3),
    net_income       NUMERIC(20,3),
    total_assets     NUMERIC(20,3),
    total_debt       NUMERIC(20,3),
    dps              NUMERIC(12,6),
    payout_ratio     NUMERIC(12,4),
    reporting_ccy    VARCHAR(5),
    updated_at       TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (ric, fiscal_year)
);

CREATE INDEX IF NOT EXISTS idx_fundamentals_ric ON pub_equity.fundamentals_annual(ric);

-- ----------------------------------------------------------------------------
-- pub_equity.ibes_estimates
-- IBES Analystenkonsensus  –  ein Snapshot pro Ausführungsdatum
--
-- fiscal_year_offset:
--   1 = FY1 (nächstes GJ)  |  2 = FY2  |  0 = Long-Term / Target Price / Empfehlung
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pub_equity.ibes_estimates (
    ric                   VARCHAR(50)  NOT NULL,
    snap_date             DATE         NOT NULL,
    fiscal_year_offset    SMALLINT     NOT NULL,
    eps_mean              NUMERIC(12,4),
    eps_median            NUMERIC(12,4),
    eps_high              NUMERIC(12,4),
    eps_low               NUMERIC(12,4),
    eps_num_estimates     SMALLINT,
    eps_std_dev           NUMERIC(12,4),
    rev_mean              NUMERIC(20,3),
    rev_median            NUMERIC(20,3),
    rev_num_estimates     SMALLINT,
    ebitda_mean           NUMERIC(20,3),
    ebitda_num_estimates  SMALLINT,
    target_price_mean     NUMERIC(20,4),
    target_price_median   NUMERIC(20,4),
    target_price_high     NUMERIC(20,4),
    target_price_low      NUMERIC(20,4),
    target_price_num      SMALLINT,
    rec_mean              NUMERIC(6,3),
    rec_num_buy           SMALLINT,
    rec_num_hold          SMALLINT,
    rec_num_sell          SMALLINT,
    rec_total             SMALLINT,
    ltg_mean              NUMERIC(12,4),
    ltg_median            NUMERIC(12,4),
    ltg_num_estimates     SMALLINT,
    updated_at            TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (ric, snap_date, fiscal_year_offset)
);

CREATE INDEX IF NOT EXISTS idx_ibes_ric       ON pub_equity.ibes_estimates(ric);
CREATE INDEX IF NOT EXISTS idx_ibes_snap_date ON pub_equity.ibes_estimates(snap_date);
