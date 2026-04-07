-- =============================================================================
-- schema/ddl.sql
-- Gesamtschema research_db — nach Session-1-Refactoring
-- Ausfuehren via: schema/migrate.py  (sicher wiederholbar)
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Schemas
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS pub_config;
CREATE SCHEMA IF NOT EXISTS pub_equity;
CREATE SCHEMA IF NOT EXISTS pub_options;
CREATE SCHEMA IF NOT EXISTS pub_rates;
CREATE SCHEMA IF NOT EXISTS pub_futures;


-- ---------------------------------------------------------------------------
-- pub_config: Konfiguration und Steuerung
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pub_config.data_sources (
    id            SERIAL      PRIMARY KEY,
    domain        VARCHAR     NOT NULL,   -- 'equity', 'options', 'rates', 'futures'
    entity_type   VARCHAR     NOT NULL,   -- 'index', 'stock', 'option', 'future', 'rate'
    module        VARCHAR     NOT NULL,   -- Modulname ohne .py
    eikon_field   VARCHAR,                -- z.B. 'TR.CLOSEPRICE'; NULL fuer berechnete Felder
    eikon_params  JSONB,                  -- Zusatz-Parameter (Frq, CURN, ...)
    logical_name  VARCHAR     NOT NULL,   -- stabiler Code-Alias, z.B. 'stock_prices_daily'
    target_schema VARCHAR     NOT NULL,   -- z.B. 'pub_equity'
    target_table  VARCHAR     NOT NULL,   -- z.B. 'stock_prices_daily'
    target_column VARCHAR     NOT NULL,   -- z.B. 'close_price'
    source_code   CHAR(2)     NOT NULL DEFAULT 'EK',  -- 'EK' = Eikon, 'FM' = eigene Berechnung
    active        BOOLEAN     DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS pub_config.stock_indices (
    index_ric                      VARCHAR PRIMARY KEY,
    index_name                     VARCHAR,
    currency                       CHAR(3),
    fetch_index_prices             BOOLEAN DEFAULT TRUE,
    fetch_index_option_chain       BOOLEAN DEFAULT FALSE,
    fetch_constituents             BOOLEAN DEFAULT FALSE,
    fetch_constituent_options      BOOLEAN DEFAULT FALSE,
    fetch_constituent_prices       BOOLEAN DEFAULT FALSE,
    fetch_constituent_fundamentals BOOLEAN DEFAULT FALSE,
    fetch_constituent_dividends    BOOLEAN DEFAULT FALSE,
    active                         BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS pub_config.futures_underlyings (
    underlying_ric    VARCHAR PRIMARY KEY,
    underlying_name   VARCHAR,
    futures_chain_ric VARCHAR,
    fetch_chains      BOOLEAN DEFAULT TRUE,
    fetch_prices      BOOLEAN DEFAULT TRUE,
    active            BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS pub_config.rates (
    series_id   VARCHAR PRIMARY KEY,  -- FRED-Kennung, z.B. 'DGS3MO'
    tenor       VARCHAR  NOT NULL,    -- '3m', '1y', '10y', etc.
    tenor_days  INTEGER  NOT NULL,    -- Laufzeit in Tagen fuer Bootstrap
    rate_type   VARCHAR  NOT NULL,    -- 'treasury_cmt'
    source      VARCHAR  NOT NULL,    -- 'FRED'
    active      BOOLEAN  DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS pub_config.pipeline_runs (
    id            SERIAL      PRIMARY KEY,
    module        VARCHAR     NOT NULL,
    mode          VARCHAR     NOT NULL,    -- 'live' | 'backfill'
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    rows_written  INTEGER,
    status        VARCHAR     NOT NULL,    -- 'running' | 'success' | 'error'
    error_msg     TEXT,
    snapshot_date DATE
);


-- ---------------------------------------------------------------------------
-- pub_equity
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pub_equity.index_overview (
    index_ric    VARCHAR(50) PRIMARY KEY,
    index_name   VARCHAR(100)
    -- Control-Flags wurden nach pub_config.stock_indices verschoben
);

CREATE TABLE IF NOT EXISTS pub_equity.index_constituents (
    index_ric       VARCHAR(50)  NOT NULL,
    snapshot_date   DATE         NOT NULL,
    constituent_ric VARCHAR(50)  NOT NULL,
    source          CHAR(2)      DEFAULT 'EK',
    created_at      TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (index_ric, snapshot_date, constituent_ric)
);

CREATE TABLE IF NOT EXISTS pub_equity.stock_prices_daily (
    ric          VARCHAR(50)   NOT NULL,
    trade_date   DATE          NOT NULL,
    close_price  NUMERIC(20,6),
    open_price   NUMERIC(20,6),
    high_price   NUMERIC(20,6),
    low_price    NUMERIC(20,6),
    volume       NUMERIC(20,0),
    crncy        VARCHAR(3)    NOT NULL DEFAULT 'LOC',
    source       CHAR(2)       DEFAULT 'EK',
    created_at   TIMESTAMPTZ   DEFAULT now(),
    PRIMARY KEY (ric, trade_date)
);

CREATE TABLE IF NOT EXISTS pub_equity.index_prices_daily (
    index_ric    VARCHAR(50)   NOT NULL,
    trade_date   DATE          NOT NULL,
    close_price  NUMERIC(20,6),
    open_price   NUMERIC(20,6),
    high_price   NUMERIC(20,6),
    low_price    NUMERIC(20,6),
    crncy        VARCHAR(3)    NOT NULL DEFAULT 'LOC',
    source       CHAR(2)       DEFAULT 'EK',
    created_at   TIMESTAMPTZ   DEFAULT now(),
    PRIMARY KEY (index_ric, trade_date)
);

CREATE TABLE IF NOT EXISTS pub_equity.stock_fundamentals_quarterly (
    ric                  VARCHAR(50)  NOT NULL,
    period_end_date      DATE         NOT NULL,
    fiscal_year          INTEGER,
    fiscal_quarter       INTEGER,
    book_value_per_share NUMERIC(20,6),
    book_equity          NUMERIC(20,6),
    shares_outstanding   NUMERIC(20,6),
    gics_sector          VARCHAR(64),
    gics_industry_group  VARCHAR(64),
    gics_industry        VARCHAR(64),
    gics_sub_industry    VARCHAR(64),
    reporting_ccy        VARCHAR(3),
    gics_backfill        BOOLEAN,
    source               CHAR(2)      DEFAULT 'EK',
    created_at           TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (ric, period_end_date)
);

CREATE TABLE IF NOT EXISTS pub_equity.stock_dividends (
    ric          VARCHAR(50)  NOT NULL,
    ex_date      DATE         NOT NULL,
    div_type     VARCHAR(10)  NOT NULL,   -- 'regular' | 'special'
    pay_date     DATE,
    gross_amount NUMERIC(20,6),
    source       CHAR(2)      DEFAULT 'EK',
    created_at   TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (ric, ex_date, div_type)
);


-- ---------------------------------------------------------------------------
-- pub_options
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pub_options.options_chains (
    underlying_ric   VARCHAR     NOT NULL,
    option_ric       VARCHAR     NOT NULL,   -- Basisform: kein /, kein ^
    option_ric_hist  VARCHAR,                -- ^-Suffix-Form; NULL solange aktiv
    expiry_date      DATE        NOT NULL,
    strike           NUMERIC     NOT NULL,
    option_type      VARCHAR(4)  NOT NULL,   -- CALL | PUT
    expiry_type      CHAR(1),                -- W / M / L
    first_seen_date  DATE        NOT NULL,
    source           CHAR(2)     NOT NULL DEFAULT 'EK',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (underlying_ric, option_ric)
);

CREATE TABLE IF NOT EXISTS pub_options.options_prices (
    option_ric   VARCHAR      NOT NULL,
    price_date   DATE         NOT NULL,
    price_bid    NUMERIC(20,6),
    price_ask    NUMERIC(20,6),
    source       CHAR(2)      DEFAULT 'EK',
    created_at   TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (option_ric, price_date)
);


-- ---------------------------------------------------------------------------
-- pub_rates
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pub_rates.usd_rates (
    trade_date  DATE         NOT NULL,
    tenor       VARCHAR(5)   NOT NULL,   -- '1m', '3m', ..., '30y'
    rate_type   VARCHAR(20)  NOT NULL,   -- 'par' | 'zero'
    rate_value  NUMERIC(10,6),
    source      CHAR(2)      DEFAULT 'EK',  -- 'EK' fuer par (FRED), 'FM' fuer zero (bootstrapped)
    created_at  TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (trade_date, tenor, rate_type)
);


-- ---------------------------------------------------------------------------
-- pub_futures
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pub_futures.futures_chains (
    futures_ric      VARCHAR(50)  NOT NULL,
    snapshot_date    DATE         NOT NULL,
    underlying_ric   VARCHAR(50),
    expiry_date      DATE,
    contract_month   CHAR(7),              -- YYYY-MM
    expiry_type      CHAR(1),              -- Q | M
    source           CHAR(2)      DEFAULT 'EK',
    created_at       TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (futures_ric, snapshot_date)
);

CREATE TABLE IF NOT EXISTS pub_futures.futures_prices (
    futures_ric    VARCHAR(50)  NOT NULL,
    price_date     DATE         NOT NULL,
    price_bid      NUMERIC(20,6),
    price_ask      NUMERIC(20,6),
    price_settle   NUMERIC(20,6),
    volume         NUMERIC(20,0),
    open_interest  NUMERIC(20,0),
    source         CHAR(2)      DEFAULT 'EK',
    created_at     TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (futures_ric, price_date)
);


-- ---------------------------------------------------------------------------
-- Indizes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_options_prices_price_date
    ON pub_options.options_prices (price_date);

CREATE INDEX IF NOT EXISTS idx_options_chains_expiry
    ON pub_options.options_chains (underlying_ric, expiry_date);

CREATE INDEX IF NOT EXISTS idx_stock_prices_trade_date
    ON pub_equity.stock_prices_daily (trade_date);

CREATE INDEX IF NOT EXISTS idx_index_prices_trade_date
    ON pub_equity.index_prices_daily (trade_date);

CREATE INDEX IF NOT EXISTS idx_usd_rates_trade_date
    ON pub_rates.usd_rates (trade_date);
